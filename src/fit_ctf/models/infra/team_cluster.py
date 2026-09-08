"""Team-owned runtime clusters for Attack & Defense competitions."""

import pathlib
import shutil
from typing import TYPE_CHECKING, Any, Mapping, cast

from bson import DBRef

from fit_ctf.components.types import ErrorCode, HealthCheckDict
from fit_ctf.models.infra.cluster_mgr_mixin import BaseCluster, ClusterManagerMixin

if TYPE_CHECKING:
    from fit_ctf.models.core.project import Project
    from fit_ctf.models.core.team import Team
    from fit_ctf.models.core.team_enrollment import TeamEnrollment
    from fit_ctf.models.infra.project_cluster import ProjectClusterManager
    from fit_ctf.models.utils.repository import EntityRepository


class TeamClusterError(Exception):
    pass


class TeamCluster(BaseCluster):
    team_enrollment_id: DBRef


class TeamClusterManager(ClusterManagerMixin[TeamCluster]):
    def __init__(
        self,
        *args,
        repo: "EntityRepository",
        project_cluster_mgr: "ProjectClusterManager",
        **kw,
    ):
        super().__init__(*args, **kw)
        self._repo = repo
        self._project_cluster_mgr = project_cluster_mgr

    @staticmethod
    def create_base_team_cluster(
        project: "Project", team: "Team", enrollment: "TeamEnrollment"
    ) -> TeamCluster:
        return TeamCluster(
            name=f"{project.name}_{team.name}",
            team_enrollment_id=DBRef("team_enrollment", enrollment.id),
        )

    def get_team_project_enrollment(
        self, cluster: TeamCluster
    ) -> tuple["Team", "Project", "TeamEnrollment"]:
        enrollment = self._repo.get_team_enrollment_by_id(cluster.team_enrollment_id.id)
        team = self._repo.get_team_by_id(enrollment.team_id.id)
        project = self._repo.get_project_by_id(enrollment.project_id.id)
        return team, project, enrollment

    def get_cluster(self, enrollment_or_name: "TeamEnrollment | str") -> TeamCluster:
        if isinstance(enrollment_or_name, str):
            cluster = self.get_doc_by_filter(name=enrollment_or_name)
        else:
            cluster = self.get_doc_by_filter(**{"team_enrollment_id.$id": enrollment_or_name.id})
        if not cluster:
            raise TeamClusterError("Team cluster does not exist.")
        return cluster

    def get_network_map(
        self, cluster_or_pair: TeamCluster | tuple["Team", "Project"]
    ) -> dict[str, str]:
        if isinstance(cluster_or_pair, TeamCluster):
            team, project, _ = self.get_team_project_enrollment(cluster_or_pair)
        else:
            team, project = cluster_or_pair
        return {
            "shared": f"{project.name}_shared_net",
            "private": f"{project.name}_{team.name}_private_net",
        }

    def _scenario_global_and_destination(
        self, cluster: TeamCluster, scenario_name: str
    ) -> tuple[pathlib.Path, pathlib.Path]:
        team, project, _ = self.get_team_project_enrollment(cluster)
        return (
            self.paths.scenario_global / scenario_name,
            self.paths.enrolled_team_path(team, project) / scenario_name,
        )

    def _network_map_for_scenario_compile(self, cluster: TeamCluster) -> Mapping[str, str]:
        return cast(dict[str, str], self.get_network_map(cluster))

    def _volume_context_extras(
        self, cluster: TeamCluster, compile_destination: pathlib.Path
    ) -> Mapping[str, Any]:
        team, project, enrollment = self.get_team_project_enrollment(cluster)
        return {
            "team_scenario_dir": str(compile_destination.resolve()),
            "project_name": project.name,
            "team_name": team.name,
            "team_enrollment_id": str(enrollment.id),
        }

    def _compose_template_extras(self, cluster: TeamCluster) -> Mapping[str, Any]:
        team, project, enrollment = self.get_team_project_enrollment(cluster)
        return {
            "project_name": project.name,
            "team_name": team.name,
            "team_enrollment_id": str(enrollment.id),
        }

    def get_compose_files(self, cluster: TeamCluster) -> list[pathlib.Path]:
        team, project, _ = self.get_team_project_enrollment(cluster)
        root = self.paths.enrolled_team_path(team, project)
        return [root / name / "scenario_compose.yaml" for name in cluster.scenario_names]

    def create_cluster(self, cluster: TeamCluster) -> TeamCluster:
        if self.get_doc_by_filter(**{"team_enrollment_id.$id": cluster.team_enrollment_id.id}):
            raise TeamClusterError("Team cluster already exists.")
        self.insert_doc(cluster)
        return cluster

    async def start_cluster(self, cluster: TeamCluster, *, verbose: bool = False) -> ErrorCode:
        _, project, _ = self.get_team_project_enrollment(cluster)
        project_cluster = self._project_cluster_mgr.get_cluster(project)
        if not await self._project_cluster_mgr.cluster_is_running(project_cluster):
            await self._project_cluster_mgr.start_cluster(project_cluster, verbose=verbose)
        return await self.c_client.compose_up(
            project.name, self.get_compose_files(cluster), to_stdout=verbose
        )

    async def stop_cluster(self, cluster: TeamCluster, *, verbose: bool = False) -> ErrorCode:
        _, project, _ = self.get_team_project_enrollment(cluster)
        code, _ = await self.c_client.compose_down(
            project.name, self.get_compose_files(cluster), to_stdout=verbose
        )
        return code

    async def build_cluster_images(
        self, cluster: TeamCluster, *, verbose: bool = False
    ) -> ErrorCode:
        _, project, _ = self.get_team_project_enrollment(cluster)
        return await self.c_client.compose_build(
            project.name, self.get_compose_files(cluster), to_stdout=verbose
        )

    async def cluster_is_running(self, cluster: TeamCluster) -> bool:
        return bool(await self.c_client.compose_ps(self.get_compose_files(cluster)))

    async def cluster_health_check(self, cluster: TeamCluster) -> list[HealthCheckDict]:
        return await self.c_client.compose_states(self.get_compose_files(cluster))

    async def delete_cluster(self, cluster: TeamCluster) -> None:
        try:
            await self.stop_cluster(cluster)
        except FileNotFoundError:
            pass
        team, project, _ = self.get_team_project_enrollment(cluster)
        self.remove_doc_by_id(cluster.id)
        path = self.paths.enrolled_team_path(team, project)
        if path.exists():
            shutil.rmtree(path)
