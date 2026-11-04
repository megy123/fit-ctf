"""Flag catalogue setup and binding checker instances to teams and topology nodes."""

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

from fit_ctf.ad.checkers import CheckerClient, FlagCatalogue, resolve_entrypoint
from fit_ctf.models.ad.managers import ADConfigError


class FlagRuntimeMixin:
    def uncovered_team_services(self, project, definitions):
        topology = self.app.ctf_db["ad_topology"].find_one({"project_id": project.id}) or {}
        covered = {(d.scenario_name, d.node_name, d.service_name) for d in definitions}
        required = set()
        for key, node in topology.get("plan", {}).get("nodes", {}).items():
            parts = key.split("/")
            if len(parts) == 5 and parts[0] == "team":
                required.update((parts[2], parts[4], service) for service in node["services"])
        return required - covered

    def configure_flags(self, project, filename):
        path = Path(filename).resolve()
        catalogue = FlagCatalogue.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        config = self._config(project)
        if config.state in {"running", "paused", "finished"} or self.managers.ticks.latest(
            project.id
        ):
            raise ADConfigError(
                "Flag definitions and tick settings must be set before the first tick"
            )
        entries = {
            key: resolve_entrypoint(flag.checker.entrypoint, path.parent)
            for key, flag in catalogue.flags.items()
        }
        # Validate all entrypoints before replacing any of this project's definitions.
        for key, flag in catalogue.flags.items():
            if not flag.node or not flag.cluster or not flag.service:
                raise ADConfigError(f"Flag {key} must name its cluster, node and service")
        self.managers.definitions.remove_docs_by_filter(project_id=project.id)
        self.managers.instances.remove_docs_by_filter(project_id=project.id)
        for key, flag in catalogue.flags.items():
            definition = self.managers.definitions.create_definition(
                project.id, key, flag.cluster, flag.service
            )
            definition.checker_entrypoint = entries[key]
            definition.checker_params = flag.checker.params
            definition.checker_adapter = "python"
            definition.flag_adapter = "checker"
            definition.node_name = flag.node
            definition.flag_ticks = flag.ticks
            definition.scoring_weight_milli = round(flag.scoring_weight * 1000)
            self.managers.definitions.update_doc(definition)
        config.tick_seconds = catalogue.ticks.seconds
        config.tick_count = catalogue.ticks.count
        config.flag_ttl_ticks = catalogue.ticks.flag_ttl_ticks
        config.checker_timeout_seconds = catalogue.ticks.checker_timeout_seconds
        config.checker_concurrency = catalogue.ticks.concurrency
        config.planting_concurrency = catalogue.ticks.concurrency
        config.state = "draft"
        config.revision += 1
        self.managers.config.update_doc(config)
        self.sync_instances(project)
        self.configure_scoreboard(project, catalogue.scoreboard)
        return catalogue

    def checker_for(self, project, instance, tick_number):
        definition = self.managers.definitions.get_doc_by_id(instance.service_definition_id)
        enrollment = self.app.team_enroll_mgr.get_doc_by_id(instance.team_enrollment_id)
        team = self.app.team_mgr.get_doc_by_id(enrollment.team_id.id)
        cluster_dir = self.app.paths.project_teams(project) / team.name / definition.scenario_name
        context = {
            "project": project.name,
            "team": team.name,
            "cluster": definition.scenario_name,
            "node": definition.node_name,
            "service": definition.service_name,
            "flag_key": definition.key,
            "tick": tick_number,
            "project_dir": str(self.app.paths.project_path(project).resolve()),
            "cluster_dir": str(cluster_dir.resolve()),
        }
        topology = self.app.ctf_db["ad_topology"].find_one({"project_id": project.id}) or {}
        if definition.node_name and topology.get("plan"):
            node_key = f"team/{team.name}/{definition.scenario_name}/node/{definition.node_name}"
            node = topology["plan"]["nodes"].get(node_key)
            if not node or definition.service_name not in node["services"]:
                raise ADConfigError(
                    f"Flag {definition.key}: topology target does not exist: {node_key}"
                )
            context["address"] = next(iter(node["interfaces"].values()))
            context["addresses"] = node["interfaces"]
        env = Environment(undefined=StrictUndefined)

        def render(value):
            if isinstance(value, str) and ("{{" in value or "{%" in value):
                return env.from_string(value).render(**context)
            if isinstance(value, dict):
                return {k: render(v) for k, v in value.items()}
            if isinstance(value, list):
                return [render(v) for v in value]
            return value

        entrypoint = definition.checker_entrypoint
        if entrypoint:
            context["params"] = render(definition.checker_params)
        else:
            from fit_ctf.components.container_client.mock_client import MockClient

            if not isinstance(self.app.c_client, MockClient):
                raise ADConfigError(
                    f"Flag {definition.key} requires an author checker; "
                    "load a version-2 flag catalogue"
                )
            # Preserve the synthetic file storage used by existing MockClient tests.
            # Real deployments must explicitly supply a checker for each flag.
            entrypoint = f"{Path(__file__).with_name('file_checker.py')}:create_checker"
            context["params"] = {"path": instance.flag_path}
        return CheckerClient(entrypoint, context, self._config(project).checker_timeout_seconds)

    def probe_instance(self, project, instance, tick_number, expected=None, operation="check"):
        started = time.monotonic()
        try:
            result = self.checker_for(project, instance, tick_number).call(operation, expected)
        except Exception as exc:
            result = {"status": "checker_error", "code": type(exc).__name__}
        self.record_service_health(project, instance, tick_number, result["status"], result["code"])
        return result["status"], result["code"], int((time.monotonic() - started) * 1000)

    def unavailable_team_service(self, project, attacker, tick):
        definitions = {
            d.id: d
            for d in self.managers.definitions.get_docs(
                project_id=project.id, active=True, enabled=True
            )
        }
        if self.uncovered_team_services(project, definitions.values()):
            return "UNCHECKED_TEAM_SERVICES"
        instances = self.managers.instances.get_docs(
            project_id=project.id, team_enrollment_id=attacker.id, active=True
        )
        if not definitions or {i.service_definition_id for i in instances}.intersection(
            definitions
        ) != set(definitions):
            return "NO_TEAM_SERVICES"

        def check(instance):
            if instance.service_definition_id not in definitions:
                return None
            latest = self.managers.flags.collection.find_one(
                {
                    "project_id": project.id,
                    "service_instance_id": instance.id,
                    "active": True,
                },
                sort=[("tick_number", -1)],
            )
            operation, expected = "health", None
            if latest:
                if latest["state"] != "planted":
                    return definitions[instance.service_definition_id].key
                operation, expected = "check", self.crypto.decrypt(latest["encrypted_value"])
            checked_tick = latest["tick_number"] if latest else tick.number
            status, _, _ = self.probe_instance(project, instance, checked_tick, expected, operation)
            if status != "up":
                return definitions[instance.service_definition_id].key
            return None

        with ThreadPoolExecutor(max_workers=self._config(project).checker_concurrency) as pool:
            return next((result for result in pool.map(check, instances) if result), None)
