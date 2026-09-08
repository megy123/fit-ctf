"""Project-scoped team enrollment for Attack & Defense competitions."""

import hashlib
import secrets
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from bson import DBRef
from pydantic import Field

from fit_ctf.models.base import Base, BaseManager

if TYPE_CHECKING:
    from fit_ctf.models.core.project import Project
    from fit_ctf.models.core.team import Team
    from fit_ctf.models.infra.team_cluster import TeamClusterManager
    from fit_ctf.models.utils.repository import EntityRepository


class TeamEnrollmentError(Exception):
    pass


class TeamAlreadyEnrolledError(TeamEnrollmentError):
    pass


class TeamNotEnrolledError(TeamEnrollmentError):
    pass


class TeamEnrollment(Base):
    project_id: DBRef
    team_id: DBRef
    state: str = "active"
    enrolled_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    member_ids_snapshot: list = Field(default_factory=list)
    token_version: int = 1
    token_hash: str
    token_prefix: str


def hash_team_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TeamEnrollmentManager(BaseManager[TeamEnrollment]):
    def __init__(
        self,
        *args,
        repo: "EntityRepository",
        team_cluster_mgr: "TeamClusterManager",
        **kw,
    ):
        super().__init__(*args, **kw)
        self._repo = repo
        self._team_cluster_mgr = team_cluster_mgr

    @staticmethod
    def issue_token() -> str:
        return f"fitctf_team_{secrets.token_urlsafe(32)}"

    def enroll(self, team: "Team", project: "Project") -> tuple[TeamEnrollment, str]:
        existing = self.get_doc_by_filter(
            **{"team_id.$id": team.id, "project_id.$id": project.id, "active": True}
        )
        if existing:
            raise TeamAlreadyEnrolledError(
                f"Team `{team.name}` is already enrolled in `{project.name}`."
            )
        member_ids = set(team.user_ids)
        for other in self.list_for_project(project):
            overlap = member_ids.intersection(other.member_ids_snapshot)
            if overlap:
                raise TeamEnrollmentError(
                    "A user may belong to only one active competition team per project."
                )
        token = self.issue_token()
        enrollment = self.create_and_insert_doc(
            team_id=DBRef("team", team.id),
            project_id=DBRef("project", project.id),
            member_ids_snapshot=list(team.user_ids),
            token_hash=hash_team_token(token),
            token_prefix=token[:16],
        )
        self.paths.enrolled_team_path(team, project).mkdir(parents=True, exist_ok=True)
        self._team_cluster_mgr.create_cluster(
            self._team_cluster_mgr.create_base_team_cluster(project, team, enrollment)
        )
        network = self._team_cluster_mgr.get_network_map((team, project))["private"]
        self.c_client.create_networks(project.name, [network])
        return enrollment, token

    def get_enrollment(
        self, team: "Team", project: "Project", active: bool | None = True
    ) -> TeamEnrollment:
        query = {"team_id.$id": team.id, "project_id.$id": project.id}
        if active is not None:
            query["active"] = active
        enrollment = self.get_doc_by_filter(**query)
        if not enrollment:
            raise TeamNotEnrolledError(f"Team `{team.name}` is not enrolled in `{project.name}`.")
        return enrollment

    def list_for_project(self, project: "Project", active: bool = True) -> list[TeamEnrollment]:
        return self.get_docs(**{"project_id.$id": project.id, "active": active})

    def rotate_token(self, enrollment: TeamEnrollment) -> str:
        token = self.issue_token()
        enrollment.token_version += 1
        enrollment.token_hash = hash_team_token(token)
        enrollment.token_prefix = token[:16]
        self.update_doc(enrollment)
        return token

    def authenticate_token(self, project: "Project", token: str) -> TeamEnrollment | None:
        digest = hash_team_token(token)
        raw = self.collection.find_one(
            {"project_id.$id": project.id, "token_hash": digest, "active": True, "state": "active"}
        )
        return self.model_cls(**raw) if raw else None
