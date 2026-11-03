import os
from typing import TYPE_CHECKING

import pymongo
from pymongo.database import Database

import fit_ctf.components.container_client.container_client_interface as c_client_interface
from fit_ctf.components.logger.default_logger import DefaultLogger
from fit_ctf.components.logger.logger_interface import LoggerInterface
from fit_ctf.components.types import EnvInfo, PathDict
from fit_ctf.path_mgmt import PathManagement

if TYPE_CHECKING:
    import fit_ctf.models.core as core
    import fit_ctf.models.infra as infra


class CTFBase:
    def __init__(
        self,
        env_info: EnvInfo,
        paths: PathDict,
        mongo_client: pymongo.MongoClient,
        _c_client: type["c_client_interface.ContainerClientInterface"],
        logger_cls: type[LoggerInterface] = DefaultLogger,
    ) -> None:
        self._env_info = env_info
        self._mongo_client = mongo_client
        self._ctf_db: Database | None = None
        self._path_mgmt = PathManagement(paths)
        self._logger = logger_cls(self)
        self._c_client = _c_client(self)

        # Initialize managers eagerly to avoid circular dependency issues
        self._init_managers()

    def _init_managers(self):
        """Initialize all managers in linear order (no circular dependencies)."""
        from fit_ctf.models.ad.managers import (
            ADCheckResultManager,
            ADFlagManager,
            ADManagers,
            ADProjectConfigManager,
            ADScoreSnapshotManager,
            ADServiceDefinitionManager,
            ADServiceInstanceManager,
            ADSubmissionManager,
            ADTickManager,
            ensure_ad_indexes,
        )
        from fit_ctf.models.ad.models import (
            ADFlag,
            ADFlagSubmission,
            ADProjectConfig,
            ADScoreSnapshot,
            ADServiceCheckResult,
            ADServiceDefinition,
            ADServiceInstance,
            ADTick,
        )
        from fit_ctf.models.core.enrollment import Enrollment, EnrollmentManager
        from fit_ctf.models.core.project import Project, ProjectManager
        from fit_ctf.models.core.team import Team, TeamManager
        from fit_ctf.models.core.team_enrollment import TeamEnrollment, TeamEnrollmentManager
        from fit_ctf.models.core.user import User, UserManager
        from fit_ctf.models.infra import (
            ModuleManager,
            ProjectCluster,
            ProjectClusterManager,
            ScenarioManager,
            TeamCluster,
            TeamClusterManager,
            UserCluster,
            UserClusterManager,
        )
        from fit_ctf.models.utils.repository import EntityRepository

        # Create shared repository first
        self._repo = EntityRepository(db=self.ctf_db)

        # Leaf managers (no manager dependencies)
        self._scenario_mgr: "infra.ScenarioManager" = ScenarioManager(paths=self._path_mgmt)

        self._module_mgr: "infra.ModuleManager" = ModuleManager(
            c_client=self._c_client,
            paths=self._path_mgmt,
        )

        self._project_cluster_mgr: "infra.ProjectClusterManager" = ProjectClusterManager(
            db=self.ctf_db,
            coll=self.ctf_db["project_cluster"],
            model_cls=ProjectCluster,
            repo=self._repo,
            c_client=self._c_client,
            paths=self._path_mgmt,
            logger=self._logger,
        )

        self._team_cluster_mgr: "infra.TeamClusterManager" = TeamClusterManager(
            db=self.ctf_db,
            coll=self.ctf_db["team_cluster"],
            model_cls=TeamCluster,
            repo=self._repo,
            project_cluster_mgr=self._project_cluster_mgr,
            c_client=self._c_client,
            paths=self._path_mgmt,
            logger=self._logger,
        )

        # Mid-level managers
        self._user_cluster_mgr: "infra.UserClusterManager" = UserClusterManager(
            db=self.ctf_db,
            coll=self.ctf_db["user_cluster"],
            model_cls=UserCluster,
            repo=self._repo,
            project_cluster_mgr=self._project_cluster_mgr,
            c_client=self._c_client,
            paths=self._path_mgmt,
            logger=self._logger,
        )

        self._prj_mgr: "core.ProjectManager" = ProjectManager(
            db=self.ctf_db,
            coll=self.ctf_db["project"],
            model_cls=Project,
            repo=self._repo,
            project_cluster_mgr=self._project_cluster_mgr,
            user_cluster_mgr=self._user_cluster_mgr,
            c_client=self._c_client,
            paths=self._path_mgmt,
            logger=self._logger,
        )

        self._user_mgr: "core.UserManager" = UserManager(
            db=self.ctf_db,
            coll=self.ctf_db["user"],
            model_cls=User,
            repo=self._repo,
            user_cluster_mgr=self._user_cluster_mgr,
            c_client=self._c_client,
            paths=self._path_mgmt,
            logger=self._logger,
        )

        self._team_mgr: "core.TeamManager" = TeamManager(
            db=self.ctf_db,
            coll=self.ctf_db["team"],
            model_cls=Team,
            c_client=self._c_client,
            paths=self._path_mgmt,
            logger=self._logger,
        )

        self._team_enroll_mgr: "core.TeamEnrollmentManager" = TeamEnrollmentManager(
            db=self.ctf_db,
            coll=self.ctf_db["team_enrollment"],
            model_cls=TeamEnrollment,
            repo=self._repo,
            team_cluster_mgr=self._team_cluster_mgr,
            c_client=self._c_client,
            paths=self._path_mgmt,
            logger=self._logger,
        )

        # Top-level manager
        self._enroll_mgr: "core.EnrollmentManager" = EnrollmentManager(
            db=self.ctf_db,
            coll=self.ctf_db["enrollment"],
            model_cls=Enrollment,
            repo=self._repo,
            user_cluster_mgr=self._user_cluster_mgr,
            project_cluster_mgr=self._project_cluster_mgr,
            c_client=self._c_client,
            paths=self._path_mgmt,
            logger=self._logger,
        )

        def manager(cls, collection, model):
            return cls(
                db=self.ctf_db,
                coll=self.ctf_db[collection],
                model_cls=model,
                c_client=self._c_client,
                paths=self._path_mgmt,
                logger=self._logger,
            )

        ensure_ad_indexes(self.ctf_db)
        self._ad_managers = ADManagers(
            config=manager(ADProjectConfigManager, "ad_project_config", ADProjectConfig),
            definitions=manager(
                ADServiceDefinitionManager, "ad_service_definition", ADServiceDefinition
            ),
            instances=manager(ADServiceInstanceManager, "ad_service_instance", ADServiceInstance),
            ticks=manager(ADTickManager, "ad_tick", ADTick),
            flags=manager(ADFlagManager, "ad_flag", ADFlag),
            submissions=manager(ADSubmissionManager, "ad_flag_submission", ADFlagSubmission),
            checks=manager(ADCheckResultManager, "ad_service_check_result", ADServiceCheckResult),
            scores=manager(ADScoreSnapshotManager, "ad_score_snapshot", ADScoreSnapshot),
        )
        from fit_ctf.ad.service import AttackDefenseService

        flag_secret = os.getenv("AD_FLAG_SECRET", self._env_info["db_password"])
        self._ad = AttackDefenseService(self, self._ad_managers, flag_secret)

        # NO two-phase wiring - clean linear initialization!

    @property
    def mongo_client(self) -> pymongo.MongoClient:
        return self._mongo_client

    @property
    def ctf_db(self) -> Database:
        if self._ctf_db is None:
            self._ctf_db = self.mongo_client[self._env_info["db_name"]]
        return self._ctf_db

    @property
    def prj_mgr(self) -> "core.ProjectManager":
        """Returns a project manager.

        :return: A project manager initialized in CTFApp.
        :rtype: ProjectManager
        """
        return self._prj_mgr

    @property
    def user_mgr(self) -> "core.UserManager":
        """Returns a user manager.

        :return: A user manager initialized in CTFApp.
        :rtype: UserManager
        """
        return self._user_mgr

    @property
    def team_mgr(self) -> "core.TeamManager":
        return self._team_mgr

    @property
    def team_enroll_mgr(self) -> "core.TeamEnrollmentManager":
        return self._team_enroll_mgr

    @property
    def enroll_mgr(self) -> "core.EnrollmentManager":
        """Returns an enrollment manager.

        :return: An enrollment manager initialized in CTFApp.
        :rtype: EnrollmentManager
        """
        return self._enroll_mgr

    @property
    def module_mgr(self) -> "infra.ModuleManager":
        """Returns a module manager.

        :return: A module manager initialized in CTFApp.
        :rtype: ModuleManager
        """
        return self._module_mgr

    @property
    def user_cluster_mgr(self) -> "infra.UserClusterManager":
        return self._user_cluster_mgr

    @property
    def team_cluster_mgr(self) -> "infra.TeamClusterManager":
        return self._team_cluster_mgr

    @property
    def project_cluster_mgr(self) -> "infra.ProjectClusterManager":
        return self._project_cluster_mgr

    @property
    def scenario_mgr(self) -> "infra.ScenarioManager":
        return self._scenario_mgr

    @property
    def c_client(self) -> "c_client_interface.ContainerClientInterface":
        return self._c_client

    @property
    def logger(self) -> LoggerInterface:
        return self._logger

    @property
    def paths(self) -> PathManagement:
        return self._path_mgmt

    @property
    def ad(self):
        return self._ad

    @property
    def topology(self):
        from fit_ctf.ad.topology import TopologyManager

        if not hasattr(self, "_topology"):
            self._topology = TopologyManager(self)
        return self._topology

    @property
    def audit(self):
        from fit_ctf.ad.audit import AuditManager

        if not hasattr(self, "_audit"):
            self._audit = AuditManager(self)
        return self._audit
