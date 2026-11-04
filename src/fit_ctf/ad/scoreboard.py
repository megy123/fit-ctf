"""Shared read model for terminal and optional web scoreboards."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from fit_ctf.ad.scoreboard_config import ScoreboardSettings
from fit_ctf.ad.scoring import weighted_components
from fit_ctf.models.ad.managers import ADConfigError


class ScoreboardMixin:
    def configure_scoreboard(self, project, settings: ScoreboardSettings):
        config = self._config(project)
        coefficients = settings.coefficients
        changed = (config.ma, config.mb, config.mc) != (
            coefficients.ma,
            coefficients.mb,
            coefficients.mc,
        )
        if changed and (
            config.state in {"running", "paused", "finished"}
            or self.managers.ticks.latest(project.id)
        ):
            raise ADConfigError("Scoring coefficients cannot change after the competition starts.")
        self.managers.config.collection.update_one(
            {"_id": config.id},
            {
                "$set": {
                    "ma": coefficients.ma,
                    "mb": coefficients.mb,
                    "mc": coefficients.mc,
                    "scoreboard_html_enabled": settings.html,
                    "scoreboard_refresh_seconds": settings.refresh_seconds,
                    "service_health_interval_seconds": settings.service_check_seconds,
                }
            },
        )
        if settings.html:
            return self.write_scoreboard_html(project)
        (self.app.paths.project_path(project) / "scoreboard" / "index.html").unlink(missing_ok=True)
        return None

    def record_service_health(self, project, instance, tick_number, status, code):
        self.app.ctf_db["ad_service_health"].update_one(
            {"project_id": project.id, "service_instance_id": instance.id},
            {
                "$set": {
                    "status": status,
                    "code": code,
                    "tick": tick_number,
                    "checked_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )

    def refresh_service_health(self, project, *, now=None):
        """Worker-only probes; scoreboard readers never execute author code."""
        now = now or datetime.now(timezone.utc)
        config = self._config(project)
        if config.state != "running":
            return
        if (
            config.service_health_checked_at
            and (now - config.service_health_checked_at).total_seconds()
            < config.service_health_interval_seconds
        ):
            return
        tick = self.managers.ticks.latest(project.id)
        if not tick or tick.state != "active":
            return
        enabled = {
            d.id
            for d in self.managers.definitions.get_docs(
                project_id=project.id, active=True, enabled=True
            )
        }
        instances = [
            i
            for i in self.managers.instances.get_docs(project_id=project.id, active=True)
            if i.service_definition_id in enabled
        ]

        def probe(instance):
            latest = self.managers.flags.collection.find_one(
                {"project_id": project.id, "service_instance_id": instance.id, "active": True},
                sort=[("tick_number", -1)],
            )
            operation, expected, number = "health", None, tick.number
            if latest:
                operation, number = "check", latest["tick_number"]
                expected = self.crypto.decrypt(latest["encrypted_value"])
            self.probe_instance(project, instance, number, expected, operation)

        with ThreadPoolExecutor(max_workers=config.checker_concurrency) as executor:
            list(executor.map(probe, instances))
        self.managers.config.collection.update_one(
            {"_id": config.id}, {"$set": {"service_health_checked_at": now}}
        )

    def scoreboard(self, project):
        """Keep the original list API, including zero rows before the first close."""
        config = self._config(project)
        closed = self.managers.ticks.collection.find_one(
            {"project_id": project.id, "state": "closed", "active": True},
            sort=[("number", -1)],
        )
        snapshots = (
            {}
            if closed is None
            else {
                s.team_enrollment_id: s
                for s in self.managers.scores.get_docs(
                    project_id=project.id, tick_number=closed["number"], active=True
                )
            }
        )
        rows = []
        for enrollment in self.app.team_enroll_mgr.list_for_project(project):
            team = self.app.team_mgr.get_doc_by_id(enrollment.team_id.id)
            snap = snapshots.get(enrollment.id)
            raw = (
                (snap.attack_total_milli, snap.defense_total_milli, snap.sla_total_milli)
                if snap
                else (0, 0, 0)
            )
            weighted = weighted_components(config, *raw)
            rows.append(
                {
                    "team_id": str(enrollment.id),
                    "team": team.display_name if team else "<missing>",
                    "tick": snap.tick_number if snap else None,
                    **dict(zip(("attack", "defense", "sla"), (n / 1000 for n in raw))),
                    "weighted": dict(
                        zip(("attack", "defense", "sla"), (n / 1000 for n in weighted))
                    ),
                    "total": (snap.total_milli if snap else 0) / 1000,
                }
            )
        rows.sort(key=lambda row: (-row["total"], row["team_id"]))
        return [{"rank": rank, **row} for rank, row in enumerate(rows, 1)]

    def scoreboard_state(self, project, *, now=None):
        now = now or datetime.now(timezone.utc)
        config = self._config(project)
        latest = self.managers.ticks.latest(project.id)
        completed = self.managers.ticks.collection.count_documents(
            {"project_id": project.id, "state": "closed", "active": True}
        )
        deadline = None
        if latest and latest.state == "active" and config.state == "running":
            deadline = (latest.active_at or latest.started_at or latest.scheduled_at) + timedelta(
                seconds=config.tick_seconds
            )
        remaining = max(0, (deadline - now).total_seconds()) if deadline else None
        ticks_remaining = (
            max(0, config.tick_count - completed) if config.tick_count is not None else None
        )
        match_remaining = None
        if config.state == "finished":
            match_remaining = 0
        elif ticks_remaining is not None:
            match_remaining = ticks_remaining * config.tick_seconds
            if latest and latest.state != "closed":
                match_remaining = max(0, ticks_remaining - 1) * config.tick_seconds
                if latest.state == "active":
                    match_remaining += remaining if remaining is not None else config.tick_seconds
                elif latest.state in {"planting", "scheduled"}:
                    match_remaining += config.tick_seconds
        rows = self.scoreboard(project)
        names = {row["team_id"]: row["team"] for row in rows}
        definitions = {
            d.id: d
            for d in self.managers.definitions.get_docs(
                project_id=project.id, active=True, enabled=True
            )
        }
        health = {
            h["service_instance_id"]: h
            for h in self.app.ctf_db["ad_service_health"].find({"project_id": project.id})
        }
        groups = {}
        for instance in self.managers.instances.get_docs(project_id=project.id, active=True):
            definition = definitions.get(instance.service_definition_id)
            team_id = str(instance.team_enrollment_id)
            if not definition or team_id not in names:
                continue
            key = (team_id, definition.scenario_name, definition.node_name, definition.service_name)
            groups.setdefault(key, []).append(health.get(instance.id))
        services = []
        severity = {"up": 0, "unknown": 1, "stale": 2, "checker_error": 3, "corrupt": 4, "down": 5}
        for (team_id, cluster, node, service), records in sorted(groups.items()):
            statuses = []
            times = []
            for record in records:
                if not record:
                    statuses.append("unknown")
                    continue
                checked = record["checked_at"]
                times.append(checked)
                age = (now - checked).total_seconds()
                # A previous check remains historical after pause/finish, never falsely live.
                statuses.append(
                    "stale"
                    if config.state != "running"
                    or age
                    > (config.service_health_interval_seconds * 2 + config.checker_timeout_seconds)
                    else record["status"]
                )
            status = max(statuses, key=lambda s: severity.get(s, 3))
            services.append(
                {
                    "team_id": team_id,
                    "team": names[team_id],
                    "cluster": cluster,
                    "node": node,
                    "service": service,
                    "status": status,
                    "active": True
                    if status == "up"
                    else False
                    if status in {"down", "corrupt"}
                    else None,
                    "checked_at": min(times).isoformat() if times else None,
                    "flag_locations": len(records),
                }
            )
        return {
            "project": project.name,
            "state": config.state,
            "generated_at": now.isoformat(),
            "refresh_seconds": config.scoreboard_refresh_seconds,
            "coefficients": {"ma": config.ma, "mb": config.mb, "mc": config.mc},
            "teams": rows,
            "services": services,
            "tick": {
                "number": latest.number if latest else None,
                "count": config.tick_count,
                "state": latest.state if latest else "not_started",
                "completed": completed,
                "remaining": ticks_remaining,
                "seconds": config.tick_seconds,
                "deadline": deadline.isoformat() if deadline else None,
                "seconds_remaining": remaining,
                "match_seconds_remaining": match_remaining,
                "scored_through": max(
                    (r["tick"] for r in rows if r["tick"] is not None), default=None
                ),
            },
        }

    def write_scoreboard_html(self, project):
        from fit_ctf.ad.scoreboard_html import render_scoreboard_html

        path = self.app.paths.project_path(project) / "scoreboard" / "index.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_scoreboard_html(self.scoreboard_state(project)), encoding="utf-8")
        return path
