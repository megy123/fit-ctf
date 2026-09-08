"""Attack & Defense use cases and the full A&D tick lifecycle."""

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from pymongo.errors import DuplicateKeyError

from fit_ctf.ad.adapters import FileFlagPlantingAdapter, FileServiceChecker
from fit_ctf.ad.crypto import FlagCrypto
from fit_ctf.ad.ports import FullAttackDefensePolicy
from fit_ctf.ad.scoring import AdditiveAttackDefenseScoringPolicy
from fit_ctf.components.data_parser.yaml_parser import YamlParser
from fit_ctf.models.ad.managers import ADConfigError, ADManagers
from fit_ctf.models.ad.models import ADScoreSnapshot, ADTick

if TYPE_CHECKING:
    from fit_ctf.ctf_base import CTFBase
    from fit_ctf.models.core.project import Project
    from fit_ctf.models.core.team_enrollment import TeamEnrollment


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SubmissionResult:
    accepted: bool
    code: str
    tick: int | None = None
    service: str | None = None


class TokenBucket:
    def __init__(self, rate: float = 10.0, burst: int = 30):
        self.rate = rate
        self.burst = float(burst)
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        with self._lock:
            now = time.monotonic()
            tokens, updated = self._buckets.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - updated) * self.rate)
            if tokens < 1:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1, now)
            return True


class AttackDefenseService:
    """Project-scoped full Attack & Defense orchestration.

    Mode behavior is behind a policy object so attack-only and defense-only can be
    introduced later without changing persistence entities or the tick engine.
    """

    def __init__(self, app: "CTFBase", managers: ADManagers, flag_secret: str):
        self.app = app
        self.managers = managers
        self.policy = FullAttackDefensePolicy()
        self.scoring_policy = AdditiveAttackDefenseScoringPolicy()
        self.crypto = FlagCrypto(flag_secret)
        self.planter = FileFlagPlantingAdapter()
        self.checker = FileServiceChecker()
        self.team_rate_limiter = TokenBucket(rate=10, burst=30)
        self.ip_rate_limiter = TokenBucket(rate=25, burst=60)
        self.project_rate_limiter = TokenBucket(rate=100, burst=300)
        self.worker_id = str(uuid.uuid4())

    def initialize_project(self, project: "Project", *, tick_seconds: int = 120):
        return self.managers.config.initialize(project.id, tick_seconds=tick_seconds)

    def _config(self, project: "Project"):
        return self.managers.config.get_for_project(project.id)

    def start_project(self, project: "Project"):
        config = self._config(project)
        if config.state == "finished":
            raise ADConfigError("A finished A&D project cannot be restarted.")
        if config.state == "running":
            return config
        self.preflight(project)
        config = self._config(project)
        config.state = "running"
        config.start_time = config.start_time or utcnow()
        config.updated_at = utcnow()
        self.managers.config.update_doc(config)
        return config

    def preflight(self, project: "Project"):
        config = self._config(project)
        enrollments = self.app.team_enroll_mgr.list_for_project(project)
        definitions = self.managers.definitions.get_docs(
            project_id=project.id, active=True, enabled=True
        )
        if len(enrollments) < 2:
            raise ADConfigError("Full Attack & Defense requires at least two active teams.")
        if not definitions:
            raise ADConfigError("At least one A&D service definition is required.")
        if any("attack_defense" not in item.supported_modes for item in definitions):
            raise ADConfigError("Every enabled service must support attack_defense.")
        self.sync_instances(project)
        config.state = "ready"
        config.updated_at = utcnow()
        self.managers.config.update_doc(config)
        return config

    def finish_project(self, project: "Project"):
        config = self._config(project)
        if config.state == "running" and self.managers.ticks.active_tick(project.id):
            self.close_tick(project)
        config.state = "finished"
        config.end_time = utcnow()
        config.updated_at = utcnow()
        self.managers.config.update_doc(config)
        self.managers.flags.collection.update_many(
            {"project_id": project.id, "state": {"$ne": "expired"}},
            {"$set": {"state": "expired", "valid_until": config.end_time}},
        )
        return config

    def pause_project(self, project: "Project"):
        config = self._config(project)
        if config.state != "running":
            raise ADConfigError("Only a running A&D project can be paused.")
        if self.managers.ticks.active_tick(project.id):
            self.close_tick(project)
        config.state = "paused"
        config.updated_at = utcnow()
        self.managers.config.update_doc(config)
        return config

    def resume_project(self, project: "Project"):
        config = self._config(project)
        if config.state != "paused":
            raise ADConfigError("Only a paused A&D project can be resumed.")
        config.state = "running"
        config.updated_at = utcnow()
        self.managers.config.update_doc(config)
        return config

    def add_service_definition(
        self,
        project: "Project",
        key: str,
        scenario_name: str,
        service_name: str,
        relative_flag_path: str = "runtime/flags/current.flag",
    ):
        definition = self.managers.definitions.create_definition(
            project.id,
            key,
            scenario_name,
            service_name,
            relative_flag_path,
        )
        self.sync_instances(project)
        return definition

    def import_service_catalog(self, project: "Project", scenario_name: str):
        """Import versioned A&D service metadata from a scenario's ad.yaml."""
        path = self.app.paths.scenario_global / scenario_name / "ad.yaml"
        raw = YamlParser.load_data_file(path, "ad")
        created = []
        for key, spec in raw["services"].items():
            supported_modes = spec.get("supported_modes", ["attack_defense"])
            if "attack_defense" not in supported_modes:
                continue
            checker = spec.get("checker", {})
            flag = spec["flag"]
            if checker.get("adapter", "file") != "file" or flag.get("adapter") != "file":
                raise ADConfigError("The first A&D implementation supports file adapters only.")
            relative_path = flag.get("relative_path")
            if not relative_path:
                relative_path = str(
                    Path("runtime")
                    / flag.get("directory_mount", "flags")
                    / flag.get("filename", "current.flag")
                )
            definition = self.managers.definitions.create_definition(
                project.id,
                key,
                scenario_name,
                spec["compose_service"],
                relative_path,
                spec.get("display_name", key),
            )
            definition.supported_modes = supported_modes
            definition.scoring_weight_milli = round(float(spec.get("scoring_weight", 1)) * 1000)
            self.managers.definitions.update_doc(definition)
            created.append(definition)
        if not created:
            raise ADConfigError("ad.yaml contains no service supporting attack_defense.")
        self.sync_instances(project)
        return created

    def sync_instances(self, project: "Project"):
        definitions = self.managers.definitions.get_docs(
            project_id=project.id, active=True, enabled=True
        )
        enrollments = self.app.team_enroll_mgr.list_for_project(project)
        teams = {
            enrollment.team_id.id: self.app.team_mgr.get_doc_by_id(enrollment.team_id.id)
            for enrollment in enrollments
        }
        if any(team is None for team in teams.values()):
            raise ADConfigError("A team enrollment references a missing team.")
        return self.managers.instances.sync_team_instances(
            project.id,
            project.name,
            definitions,
            enrollments,
            teams,
            self.app.paths.project_teams(project),
        )

    def open_tick(self, project: "Project", *, now: datetime | None = None) -> ADTick:
        now = now or utcnow()
        config = self._config(project)
        if config.state != "running":
            raise ADConfigError("A&D project is not running.")
        if self.managers.ticks.active_tick(project.id):
            raise ADConfigError("The project already has an active tick.")
        instances = self.sync_instances(project)
        latest = self.managers.ticks.latest(project.id)
        tick = self.managers.ticks.create_and_insert_doc(
            project_id=project.id,
            number=0 if latest is None else latest.number + 1,
            state="planting",
            scheduled_at=now,
            started_at=now,
            config_revision=config.revision,
            scoring_policy_version=config.scoring_policy_version,
            expected_instance_count=len(instances),
        )
        planted = 0
        planting_jobs = []
        for instance in instances:
            value = self.crypto.generate()
            flag = self.managers.flags.create_and_insert_doc(
                project_id=project.id,
                tick_id=tick.id,
                tick_number=tick.number,
                service_instance_id=instance.id,
                service_definition_id=instance.service_definition_id,
                owner_team_enrollment_id=instance.team_enrollment_id,
                lookup_digest=self.crypto.digest(value),
                encrypted_value=self.crypto.encrypt(value),
                valid_from=now,
                valid_until=now + timedelta(seconds=config.tick_seconds * config.flag_ttl_ticks),
            )
            flag.planting_attempts += 1
            planting_jobs.append((flag, Path(instance.flag_path), value))

        def plant_one(job):
            flag, path, value = job
            try:
                self.planter.plant(path, value)
                return flag, None
            except Exception as exc:  # adapter boundary: persist a redacted failure type
                return flag, type(exc).__name__

        with ThreadPoolExecutor(max_workers=config.planting_concurrency) as executor:
            planting_results = executor.map(plant_one, planting_jobs)
            for flag, error in planting_results:
                if error is None:
                    flag.state = "planted"
                    flag.planted_at = utcnow()
                    planted += 1
                else:
                    flag.state = "plant_failed"
                    flag.last_error = error
                self.managers.flags.update_doc(flag)
        tick.state = "active"
        tick.active_at = utcnow()
        tick.planted_instance_count = planted
        self.managers.ticks.update_doc(tick)
        return tick

    def close_tick(self, project: "Project", *, now: datetime | None = None) -> ADTick:
        now = now or utcnow()
        self._config(project)
        tick = self.managers.ticks.active_tick(project.id)
        if not tick:
            latest = self.managers.ticks.latest(project.id)
            if latest and latest.state == "closed":
                return latest
            raise ADConfigError("The project has no active tick.")
        tick.state = "checking"
        self.managers.ticks.update_doc(tick)
        flags = self.managers.flags.get_docs(
            project_id=project.id, tick_number=tick.number, active=True
        )
        instance_by_id = {
            item.id: item
            for item in self.managers.instances.get_docs(project_id=project.id, active=True)
        }
        checked = 0
        check_jobs = []
        for flag in flags:
            instance = instance_by_id[flag.service_instance_id]
            existing = self.managers.checks.get_doc_by_filter(
                project_id=project.id,
                service_instance_id=instance.id,
                tick_number=tick.number,
                run_number=0,
            )
            if not existing:
                expected = self.crypto.decrypt(flag.encrypted_value)
                check_jobs.append((flag, instance, Path(instance.flag_path), expected))
            checked += 1

        def check_one(job):
            flag, instance, path, expected = job
            try:
                status, code, latency = self.checker.check(path, expected)
            except Exception as exc:  # checker bugs are neutral infrastructure failures
                status, code, latency = "checker_error", type(exc).__name__, None
            return flag, instance, status, code, latency

        config = self._config(project)
        with ThreadPoolExecutor(max_workers=config.checker_concurrency) as executor:
            for flag, instance, status, code, latency in executor.map(check_one, check_jobs):
                self.managers.checks.create_and_insert_doc(
                    project_id=project.id,
                    tick_id=tick.id,
                    tick_number=tick.number,
                    service_instance_id=instance.id,
                    service_definition_id=instance.service_definition_id,
                    owner_team_enrollment_id=instance.team_enrollment_id,
                    status=status,
                    functional_ok=status == "up",
                    flag_ok=status == "up",
                    latency_ms=latency,
                    message_code=code,
                )
        tick.state = "scoring"
        tick.checked_instance_count = checked
        self.managers.ticks.update_doc(tick)
        self._score_tick(project, tick)
        tick.state = "closed"
        tick.closed_at = now
        self.managers.ticks.update_doc(tick)
        return tick

    def advance(self, project: "Project", *, now: datetime | None = None) -> ADTick:
        now = now or utcnow()
        active = self.managers.ticks.active_tick(project.id)
        if active:
            self.close_tick(project, now=now)
        return self.open_tick(project, now=now)

    def run_due_projects(self, *, now: datetime | None = None) -> list[ADTick]:
        """Advance every running project independently when its tick is due."""
        now = now or utcnow()
        advanced: list[ADTick] = []
        configs = self.managers.config.get_docs(state="running", active=True)
        for config in configs:
            leased = self.managers.config.acquire_scheduler_lease(
                config.id, self.worker_id, now, lease_seconds=max(config.tick_seconds, 30)
            )
            if not leased:
                continue
            try:
                project = self.app.prj_mgr.get_doc_by_id(config.project_id)
                if not project:
                    continue
                active = self.managers.ticks.active_tick(project.id)
                if active is None:
                    advanced.append(self.open_tick(project, now=now))
                    continue
                boundary = (
                    active.active_at or active.started_at or active.scheduled_at
                ) + timedelta(seconds=config.tick_seconds)
                if now >= boundary:
                    advanced.append(self.advance(project, now=now))
            finally:
                self.managers.config.release_scheduler_lease(config.id, self.worker_id)
        return advanced

    def submit(
        self,
        project: "Project",
        attacker: "TeamEnrollment",
        value: str,
        *,
        source: str = "api",
        source_ip: str | None = None,
        request_id: str | None = None,
        now: datetime | None = None,
    ) -> SubmissionResult:
        now = now or utcnow()
        config = self._config(project)
        if attacker.project_id.id != project.id or attacker.state != "active":
            return SubmissionResult(False, "UNAUTHORIZED_TEAM")
        if config.state != "running" or not self.policy.attack_enabled:
            return SubmissionResult(False, "PROJECT_NOT_RUNNING")
        if request_id:
            previous_request = self.managers.submissions.get_doc_by_filter(
                project_id=project.id,
                attacker_team_enrollment_id=attacker.id,
                request_id=request_id,
            )
            if previous_request:
                previous_flag = self.managers.flags.get_doc_by_id(previous_request.flag_id)
                previous_definition = (
                    self.managers.definitions.get_doc_by_id(previous_flag.service_definition_id)
                    if previous_flag
                    else None
                )
                return SubmissionResult(
                    True,
                    "ACCEPTED",
                    previous_request.tick_number,
                    previous_definition.key if previous_definition else None,
                )
        allowed = self.team_rate_limiter.allow(str(attacker.id))
        allowed = self.project_rate_limiter.allow(str(project.id)) and allowed
        if source_ip:
            allowed = self.ip_rate_limiter.allow(source_ip) and allowed
        if not allowed:
            return SubmissionResult(False, "RATE_LIMITED")
        if len(value) > 256 or not value.startswith("FITCTF{") or not value.endswith("}"):
            return SubmissionResult(False, "INVALID_FORMAT")
        digest = self.crypto.digest(value.strip())
        flag = self.managers.flags.get_doc_by_filter(
            project_id=project.id, lookup_digest=digest, active=True
        )
        if not flag or flag.state != "planted":
            return SubmissionResult(False, "UNKNOWN_OR_EXPIRED")
        if not (flag.valid_from <= now < flag.valid_until):
            return SubmissionResult(False, "UNKNOWN_OR_EXPIRED")
        if flag.owner_team_enrollment_id == attacker.id:
            return SubmissionResult(False, "OWN_FLAG", tick=flag.tick_number)
        definition = self.managers.definitions.get_doc_by_id(flag.service_definition_id)
        try:
            self.managers.submissions.create_and_insert_doc(
                project_id=project.id,
                attacker_team_enrollment_id=attacker.id,
                flag_id=flag.id,
                service_instance_id=flag.service_instance_id,
                victim_team_enrollment_id=flag.owner_team_enrollment_id,
                tick_number=flag.tick_number,
                source=source,
                request_id=request_id or str(uuid.uuid4()),
            )
        except DuplicateKeyError:
            if request_id:
                previous_request = self.managers.submissions.get_doc_by_filter(
                    project_id=project.id,
                    attacker_team_enrollment_id=attacker.id,
                    request_id=request_id,
                )
                if previous_request:
                    return SubmissionResult(
                        True,
                        "ACCEPTED",
                        previous_request.tick_number,
                        definition.key if definition else None,
                    )
            return SubmissionResult(
                False,
                "DUPLICATE",
                tick=flag.tick_number,
                service=definition.key if definition else None,
            )
        return SubmissionResult(
            True,
            "ACCEPTED",
            tick=flag.tick_number,
            service=definition.key if definition else None,
        )

    def _score_tick(self, project: "Project", tick: ADTick) -> list[ADScoreSnapshot]:
        config = self._config(project)
        enrollments = self.app.team_enroll_mgr.list_for_project(project)
        submissions = self.managers.submissions.get_docs(
            project_id=project.id, tick_number=tick.number, accepted=True, active=True
        )
        checks = self.managers.checks.get_docs(
            project_id=project.id, tick_number=tick.number, active=True
        )
        definitions = {
            item.id: item
            for item in self.managers.definitions.get_docs(project_id=project.id, active=True)
        }
        instance_definition_ids = {
            item.id: item.service_definition_id
            for item in self.managers.instances.get_docs(project_id=project.id, active=True)
        }
        previous = {
            snap.team_enrollment_id: snap
            for snap in self.managers.scores.latest_for_project(project.id)
            if snap.tick_number < tick.number
        }
        created: list[ADScoreSnapshot] = []
        for enrollment in enrollments:
            delta = self.scoring_policy.compute_team(
                config=config,
                team_enrollment_id=enrollment.id,
                submissions=submissions,
                checks=checks,
                definitions=definitions,
                instance_definition_ids=instance_definition_ids,
            )
            prev = previous.get(enrollment.id)
            attack_total = delta.attack_milli + (prev.attack_total_milli if prev else 0)
            defense_total = delta.defense_milli + (prev.defense_total_milli if prev else 0)
            sla_total = delta.sla_milli + (prev.sla_total_milli if prev else 0)
            snapshot = ADScoreSnapshot(
                project_id=project.id,
                tick_number=tick.number,
                team_enrollment_id=enrollment.id,
                policy_name=self.scoring_policy.name,
                policy_version=self.scoring_policy.version,
                tick_attack_milli=delta.attack_milli,
                tick_defense_milli=delta.defense_milli,
                tick_sla_milli=delta.sla_milli,
                attack_total_milli=attack_total,
                defense_total_milli=defense_total,
                sla_total_milli=sla_total,
                total_milli=attack_total + defense_total + sla_total,
                input_digest=delta.input_digest,
            )
            existing = self.managers.scores.get_doc_by_filter(
                project_id=project.id,
                team_enrollment_id=enrollment.id,
                tick_number=tick.number,
                policy_version=snapshot.policy_version,
            )
            if existing:
                snapshot.id = existing.id
                self.managers.scores.update_doc(snapshot)
            else:
                self.managers.scores.insert_doc(snapshot)
            created.append(snapshot)
        ranked = sorted(created, key=lambda item: (-item.total_milli, str(item.team_enrollment_id)))
        for rank, snapshot in enumerate(ranked, 1):
            snapshot.rank = rank
            self.managers.scores.update_doc(snapshot)
        return ranked

    def scoreboard(self, project: "Project") -> list[dict]:
        snapshots = sorted(
            self.managers.scores.latest_for_project(project.id), key=lambda item: item.rank
        )
        result = []
        for snapshot in snapshots:
            enrollment = self.app.team_enroll_mgr.get_doc_by_id(snapshot.team_enrollment_id)
            team = self.app.team_mgr.get_doc_by_id(enrollment.team_id.id) if enrollment else None
            result.append(
                {
                    "rank": snapshot.rank,
                    "team": team.display_name if team else "<missing>",
                    "tick": snapshot.tick_number,
                    "attack": snapshot.attack_total_milli / 1000,
                    "defense": snapshot.defense_total_milli / 1000,
                    "sla": snapshot.sla_total_milli / 1000,
                    "total": snapshot.total_milli / 1000,
                }
            )
        return result
