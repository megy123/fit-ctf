"""Persistent Attack & Defense entities.

New A&D collections use direct ObjectIds consistently.  This keeps compound indexes
and project scoping straightforward while legacy models continue using DBRef.
"""

from datetime import datetime, timezone
from enum import Enum

from bson import ObjectId
from pydantic import Field

from fit_ctf.models.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CompetitionMode(str, Enum):
    ATTACK_DEFENSE = "attack_defense"
    # Reserved extension points.  ConfigManager deliberately rejects these modes today.
    ATTACK_ONLY = "attack_only"
    DEFENSE_ONLY = "defense_only"


class ADProjectConfig(Base):
    project_id: ObjectId
    mode: CompetitionMode = CompetitionMode.ATTACK_DEFENSE
    state: str = "draft"
    tick_seconds: int = 120
    flag_ttl_ticks: int = 2
    checker_runs_per_tick: int = 1
    checker_concurrency: int = 8
    planting_concurrency: int = 8
    attack_points_milli: int = 10_000
    defense_points_milli: int = 5_000
    sla_points_milli: int = 5_000
    start_time: datetime | None = None
    end_time: datetime | None = None
    revision: int = 1
    scoring_policy: str = "additive_v1"
    scoring_policy_version: int = 1
    submit_rate_per_second: float = 10.0
    submit_burst: int = 30
    submit_batch_size: int = 50
    scheduler_lease_owner: str | None = None
    scheduler_lease_until: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ADServiceDefinition(Base):
    project_id: ObjectId
    key: str
    display_name: str
    scenario_name: str
    service_name: str
    enabled: bool = True
    supported_modes: list[str] = Field(default_factory=lambda: ["attack_defense"])
    owner_scope: str = "team"
    checker_adapter: str = "file"
    flag_adapter: str = "file"
    relative_flag_path: str = "runtime/flags/current.flag"
    scoring_weight_milli: int = 1000
    revision: int = 1


class ADServiceInstance(Base):
    project_id: ObjectId
    service_definition_id: ObjectId
    team_enrollment_id: ObjectId
    instance_key: str
    flag_path: str
    scope: str = "team_owned"
    cluster_kind: str = "team"
    runtime_service_name: str = ""
    deployment_revision: int = 1


class ADTick(Base):
    project_id: ObjectId
    number: int
    state: str = "scheduled"
    mode: CompetitionMode = CompetitionMode.ATTACK_DEFENSE
    scheduled_at: datetime
    started_at: datetime | None = None
    active_at: datetime | None = None
    closed_at: datetime | None = None
    config_revision: int = 1
    scoring_policy_version: int = 1
    expected_instance_count: int = 0
    planted_instance_count: int = 0
    checked_instance_count: int = 0
    error_summary: str | None = None
    lease_owner: str | None = None
    lease_until: datetime | None = None
    attempt: int = 1


class ADFlag(Base):
    project_id: ObjectId
    tick_id: ObjectId
    tick_number: int
    service_instance_id: ObjectId
    service_definition_id: ObjectId
    owner_team_enrollment_id: ObjectId
    slot: int = 0
    lookup_digest: str
    encrypted_value: str
    key_version: int = 1
    state: str = "created"
    valid_from: datetime
    valid_until: datetime
    planted_at: datetime | None = None
    planting_attempts: int = 0
    last_error: str | None = None


class ADFlagSubmission(Base):
    project_id: ObjectId
    attacker_team_enrollment_id: ObjectId
    flag_id: ObjectId
    service_instance_id: ObjectId
    victim_team_enrollment_id: ObjectId
    tick_number: int
    submitted_at: datetime = Field(default_factory=utcnow)
    accepted: bool = True
    reason: str = "ACCEPTED"
    source: str = "api"
    request_id: str = ""


class ADServiceCheckResult(Base):
    project_id: ObjectId
    tick_id: ObjectId
    tick_number: int
    service_instance_id: ObjectId
    service_definition_id: ObjectId
    owner_team_enrollment_id: ObjectId
    run_number: int = 0
    status: str
    functional_ok: bool
    flag_ok: bool | None
    latency_ms: int | None = None
    message_code: str
    message_detail: str = ""
    checked_at: datetime = Field(default_factory=utcnow)


class ADScoreSnapshot(Base):
    project_id: ObjectId
    tick_number: int
    team_enrollment_id: ObjectId
    mode: CompetitionMode = CompetitionMode.ATTACK_DEFENSE
    policy_name: str = "additive_v1"
    policy_version: int = 1
    tick_attack_milli: int = 0
    tick_defense_milli: int = 0
    tick_sla_milli: int = 0
    attack_total_milli: int = 0
    defense_total_milli: int = 0
    sla_total_milli: int = 0
    total_milli: int = 0
    rank: int = 0
    input_digest: str = ""
    computed_at: datetime = Field(default_factory=utcnow)
