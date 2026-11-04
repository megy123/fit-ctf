"""Persistence managers and indexes for Attack & Defense entities."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, ReturnDocument

from fit_ctf.models.ad.models import (
    ADFlag,
    ADFlagSubmission,
    ADProjectConfig,
    ADScoreSnapshot,
    ADServiceCheckResult,
    ADServiceDefinition,
    ADServiceInstance,
    ADTick,
    CompetitionMode,
)
from fit_ctf.models.base import BaseManager


class ADConfigError(Exception):
    pass


class ADProjectConfigManager(BaseManager[ADProjectConfig]):
    def initialize(
        self, project_id: ObjectId, *, tick_seconds: int = 120, tick_count: int | None = None
    ) -> ADProjectConfig:
        if tick_seconds < 1 or (tick_count is not None and tick_count < 1):
            raise ADConfigError("Tick duration and count must be positive.")
        if self.get_doc_by_filter(project_id=project_id, active=True):
            raise ADConfigError("Attack & Defense is already configured for this project.")
        return self.create_and_insert_doc(
            project_id=project_id,
            mode=CompetitionMode.ATTACK_DEFENSE,
            network_topology="isolated_router_v1",
            tick_seconds=tick_seconds,
            tick_count=tick_count,
        )

    def get_for_project(self, project_id: ObjectId) -> ADProjectConfig:
        config = self.get_doc_by_filter(project_id=project_id, active=True)
        if not config:
            raise ADConfigError("Attack & Defense is not configured for this project.")
        return config

    def acquire_scheduler_lease(
        self,
        config_id: ObjectId,
        owner: str,
        now: datetime,
        lease_seconds: int = 30,
    ) -> ADProjectConfig | None:
        raw = self.collection.find_one_and_update(
            {
                "_id": config_id,
                "$or": [
                    {"scheduler_lease_until": {"$lte": now}},
                    {"scheduler_lease_until": None},
                    {"scheduler_lease_owner": owner},
                ],
            },
            {
                "$set": {
                    "scheduler_lease_owner": owner,
                    "scheduler_lease_until": now + timedelta(seconds=lease_seconds),
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        return self.model_cls(**raw) if raw else None

    def release_scheduler_lease(self, config_id: ObjectId, owner: str) -> None:
        self.collection.update_one(
            {"_id": config_id, "scheduler_lease_owner": owner},
            {"$set": {"scheduler_lease_owner": None, "scheduler_lease_until": None}},
        )


class ADServiceDefinitionManager(BaseManager[ADServiceDefinition]):
    def create_definition(
        self,
        project_id: ObjectId,
        key: str,
        scenario_name: str,
        service_name: str,
        relative_flag_path: str = "runtime/flags/current.flag",
        display_name: str = "",
    ) -> ADServiceDefinition:
        rel = PurePosixPath(relative_flag_path)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("Flag path must be a safe relative path.")
        if self.get_doc_by_filter(project_id=project_id, key=key, active=True):
            raise ValueError(f"A&D service `{key}` already exists.")
        return self.create_and_insert_doc(
            project_id=project_id,
            key=key,
            display_name=display_name or key,
            scenario_name=scenario_name,
            service_name=service_name,
            relative_flag_path=str(rel),
        )


class ADServiceInstanceManager(BaseManager[ADServiceInstance]):
    def sync_team_instances(
        self,
        project_id: ObjectId,
        project_name: str,
        definitions: list[ADServiceDefinition],
        enrollments: list,
        team_by_id: dict[ObjectId, object],
        project_teams_path: Path,
    ) -> list[ADServiceInstance]:
        instances: list[ADServiceInstance] = []
        for enrollment in enrollments:
            team = team_by_id[enrollment.team_id.id]
            for definition in definitions:
                key = f"{team.name}/{definition.key}"
                existing = self.get_doc_by_filter(
                    project_id=project_id, instance_key=key, active=True
                )
                if existing:
                    instances.append(existing)
                    continue
                flag_path = (
                    project_teams_path
                    / team.name
                    / definition.scenario_name
                    / definition.relative_flag_path
                )
                instances.append(
                    self.create_and_insert_doc(
                        project_id=project_id,
                        service_definition_id=definition.id,
                        team_enrollment_id=enrollment.id,
                        instance_key=key,
                        flag_path=str(flag_path),
                        runtime_service_name=definition.service_name,
                    )
                )
        return instances


class ADTickManager(BaseManager[ADTick]):
    def latest(self, project_id: ObjectId) -> ADTick | None:
        raw = self.collection.find_one(
            {"project_id": project_id, "active": True}, sort=[("number", DESCENDING)]
        )
        return self.model_cls(**raw) if raw else None

    def active_tick(self, project_id: ObjectId) -> ADTick | None:
        raw = self.collection.find_one(
            {
                "project_id": project_id,
                "active": True,
                "state": {"$in": ["planting", "active", "checking", "scoring"]},
            },
            sort=[("number", DESCENDING)],
        )
        return self.model_cls(**raw) if raw else None


class ADFlagManager(BaseManager[ADFlag]):
    pass


class ADSubmissionManager(BaseManager[ADFlagSubmission]):
    pass


class ADCheckResultManager(BaseManager[ADServiceCheckResult]):
    pass


class ADScoreSnapshotManager(BaseManager[ADScoreSnapshot]):
    def latest_for_project(self, project_id: ObjectId) -> list[ADScoreSnapshot]:
        latest = self.collection.find_one(
            {"project_id": project_id, "active": True}, sort=[("tick_number", DESCENDING)]
        )
        if not latest:
            return []
        return self.get_docs(project_id=project_id, tick_number=latest["tick_number"], active=True)


@dataclass
class ADManagers:
    config: ADProjectConfigManager
    definitions: ADServiceDefinitionManager
    instances: ADServiceInstanceManager
    ticks: ADTickManager
    flags: ADFlagManager
    submissions: ADSubmissionManager
    checks: ADCheckResultManager
    scores: ADScoreSnapshotManager


def ensure_ad_indexes(db) -> None:
    db["team"].create_index("name", unique=True)
    db["team_enrollment"].create_index(
        [("project_id.$id", ASCENDING), ("team_id.$id", ASCENDING)], unique=True
    )
    db["team_cluster"].create_index("team_enrollment_id.$id", unique=True)
    db["team_cluster"].create_index("name", unique=True)
    db["ad_project_config"].create_index("project_id", unique=True)
    db["ad_service_definition"].create_index(
        [("project_id", ASCENDING), ("key", ASCENDING)], unique=True
    )
    db["ad_service_instance"].create_index(
        [("project_id", ASCENDING), ("instance_key", ASCENDING)], unique=True
    )
    db["ad_tick"].create_index([("project_id", ASCENDING), ("number", ASCENDING)], unique=True)
    db["ad_tick"].create_index(
        [("project_id", ASCENDING), ("state", ASCENDING), ("lease_until", ASCENDING)]
    )
    db["ad_flag"].create_index(
        [("project_id", ASCENDING), ("lookup_digest", ASCENDING)], unique=True
    )
    db["ad_flag"].create_index(
        [
            ("project_id", ASCENDING),
            ("service_instance_id", ASCENDING),
            ("tick_number", ASCENDING),
            ("slot", ASCENDING),
        ],
        unique=True,
    )
    db["ad_flag_submission"].create_index(
        [
            ("project_id", ASCENDING),
            ("attacker_team_enrollment_id", ASCENDING),
            ("flag_id", ASCENDING),
        ],
        unique=True,
    )
    db["ad_flag_submission"].create_index(
        [
            ("project_id", ASCENDING),
            ("attacker_team_enrollment_id", ASCENDING),
            ("request_id", ASCENDING),
        ],
        unique=True,
        partialFilterExpression={"request_id": {"$type": "string", "$gt": ""}},
    )
    db["ad_service_check_result"].create_index(
        [
            ("project_id", ASCENDING),
            ("service_instance_id", ASCENDING),
            ("tick_number", ASCENDING),
            ("run_number", ASCENDING),
        ],
        unique=True,
    )
    db["ad_score_snapshot"].create_index(
        [
            ("project_id", ASCENDING),
            ("team_enrollment_id", ASCENDING),
            ("tick_number", ASCENDING),
            ("policy_version", ASCENDING),
        ],
        unique=True,
    )
