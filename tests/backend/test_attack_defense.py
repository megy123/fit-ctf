from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from fit_ctf.models.ad.managers import ADConfigError
from fit_ctf.models.core.team import InvalidTeamNameError, UserAlreadyInTeam
from tests import FixtureData


def _competition(app, name: str = "ad_project", team_count: int = 2):
    project = app.prj_mgr.init_project(name=name, max_nof_users=20)
    teams = [app.team_mgr.create_team(f"{name}_team_{index}") for index in range(team_count)]
    enrolled = [app.team_enroll_mgr.enroll(team, project) for team in teams]
    app.ad.initialize_project(project, tick_seconds=10)
    definition = app.ad.add_service_definition(
        project,
        key="notes",
        scenario_name="vulnerable_notes",
        service_name="web",
    )
    app.ad.start_project(project)
    return project, teams, enrolled, definition


def test_team_membership_and_enrollment(empty_data: FixtureData):
    app, _ = empty_data
    project = app.prj_mgr.init_project(name="team_project", max_nof_users=10)
    user, _ = app.user_mgr.create_new_user("alice", "test-password")
    team = app.team_mgr.create_team("red_team", "Red Team")

    app.team_mgr.add_user(team, user, captain=True)
    with pytest.raises(UserAlreadyInTeam):
        app.team_mgr.add_user(team, user)
    with pytest.raises(InvalidTeamNameError):
        app.team_mgr.create_team("Bad-Team")

    enrollment, token = app.team_enroll_mgr.enroll(team, project)
    assert app.team_enroll_mgr.authenticate_token(project, token).id == enrollment.id
    assert app.paths.enrolled_team_path(team, project).is_dir()
    assert app.team_cluster_mgr.get_cluster(enrollment).name == "team_project_red_team"

    rotated = app.team_enroll_mgr.rotate_token(enrollment)
    assert rotated != token
    assert app.team_enroll_mgr.authenticate_token(project, token) is None
    assert app.team_enroll_mgr.authenticate_token(project, rotated) is not None


def test_full_attack_defense_lifecycle_scales_to_three_teams(empty_data: FixtureData):
    app, _ = empty_data
    project, _, enrolled, definition = _competition(app, team_count=3)
    enrollments = [item[0] for item in enrolled]

    tick = app.ad.open_tick(project)
    assert tick.number == 0
    assert tick.planted_instance_count == 3
    assert len(app.ad.managers.instances.get_docs(project_id=project.id)) == 3

    victim_flag = app.ad.managers.flags.get_doc_by_filter(
        project_id=project.id,
        tick_number=0,
        owner_team_enrollment_id=enrollments[1].id,
    )
    value = app.ad.crypto.decrypt(victim_flag.encrypted_value)

    assert app.ad.submit(project, enrollments[1], value).code == "OWN_FLAG"
    assert (
        app.ad.submit(project, enrollments[0], value, now=victim_flag.valid_until).code
        == "UNKNOWN_OR_EXPIRED"
    )
    assert app.ad.submit(project, enrollments[0], value).code == "ACCEPTED"
    assert app.ad.submit(project, enrollments[0], value).code == "DUPLICATE"
    assert app.ad.submit(project, enrollments[2], "FITCTF{not-a-real-flag}").code == (
        "UNKNOWN_OR_EXPIRED"
    )

    closed = app.ad.close_tick(project)
    assert closed.state == "closed"
    assert closed.checked_instance_count == 3
    assert app.ad.close_tick(project).id == closed.id

    scoreboard = app.ad.scoreboard(project)
    assert len(scoreboard) == 3
    assert scoreboard[0]["team"] == "ad_project_team_0"
    assert scoreboard[0]["attack"] == 10
    assert scoreboard[0]["defense"] == 5
    assert scoreboard[0]["sla"] == 5
    assert scoreboard[0]["total"] == 20
    assert {row["total"] for row in scoreboard[1:]} == {10}
    assert definition.key == "notes"


def test_submission_request_id_is_idempotent(empty_data: FixtureData):
    app, _ = empty_data
    project, _, enrolled, _ = _competition(app, "idempotent_ad")
    attacker, victim = (item[0] for item in enrolled)
    app.ad.open_tick(project)
    flag = app.ad.managers.flags.get_doc_by_filter(
        project_id=project.id, owner_team_enrollment_id=victim.id
    )
    value = app.ad.crypto.decrypt(flag.encrypted_value)

    first = app.ad.submit(project, attacker, value, request_id="request-1")
    repeated = app.ad.submit(project, attacker, "different body", request_id="request-1")
    assert first.code == repeated.code == "ACCEPTED"
    assert len(app.ad.managers.submissions.get_docs(project_id=project.id)) == 1


def test_concurrent_submission_is_scored_only_once(empty_data: FixtureData):
    app, _ = empty_data
    project, _, enrolled, _ = _competition(app, "concurrent_ad")
    attacker, victim = (item[0] for item in enrolled)
    app.ad.open_tick(project)
    flag = app.ad.managers.flags.get_doc_by_filter(
        project_id=project.id, owner_team_enrollment_id=victim.id
    )
    value = app.ad.crypto.decrypt(flag.encrypted_value)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda request_id: app.ad.submit(
                    project, attacker, value, request_id=request_id
                ),
                ["concurrent-1", "concurrent-2"],
            )
        )
    assert {result.code for result in results} == {"ACCEPTED", "DUPLICATE"}
    assert len(app.ad.managers.submissions.get_docs(project_id=project.id)) == 1


def test_tick_worker_and_project_isolation(empty_data: FixtureData):
    app, _ = empty_data
    project_a, _, enrolled_a, _ = _competition(app, "project_a")
    project_b, _, enrolled_b, _ = _competition(app, "project_b")

    opened = app.ad.run_due_projects()
    assert {tick.project_id for tick in opened} == {project_a.id, project_b.id}

    flag_a = app.ad.managers.flags.get_doc_by_filter(project_id=project_a.id, tick_number=0)
    value_a = app.ad.crypto.decrypt(flag_a.encrypted_value)
    assert app.ad.submit(project_b, enrolled_b[0][0], value_a).code == "UNKNOWN_OR_EXPIRED"
    assert app.ad.submit(project_a, enrolled_b[0][0], value_a).code == "UNAUTHORIZED_TEAM"

    tick_a = app.ad.managers.ticks.active_tick(project_a.id)
    due = tick_a.active_at + timedelta(seconds=10)
    advanced = app.ad.run_due_projects(now=due)
    assert any(tick.project_id == project_a.id and tick.number == 1 for tick in advanced)
    assert len(app.ad.managers.scores.get_docs(project_id=project_a.id, tick_number=0)) == 2


def test_start_requires_full_attack_defense_prerequisites(empty_data: FixtureData):
    app, _ = empty_data
    project = app.prj_mgr.init_project(name="incomplete_ad", max_nof_users=5)
    app.ad.initialize_project(project, tick_seconds=10)
    with pytest.raises(ADConfigError):
        app.ad.start_project(project)


def test_imports_declarative_ad_service_catalog(empty_data: FixtureData):
    app, _ = empty_data
    project = app.prj_mgr.init_project(name="catalog_ad", max_nof_users=5)
    scenario = app.paths.scenario_global / "catalog_scenario"
    scenario.mkdir()
    (scenario / "ad.yaml").write_text(
        """\
version: 1
services:
  notes:
    display_name: Vulnerable Notes
    compose_service: web
    supported_modes: [attack_defense, attack_only]
    checker:
      adapter: file
    flag:
      adapter: file
      directory_mount: flags
      filename: current.flag
    scoring_weight: 1.5
""",
        encoding="utf-8",
    )

    definitions = app.ad.import_service_catalog(project, "catalog_scenario")
    assert len(definitions) == 1
    assert definitions[0].relative_flag_path == "runtime/flags/current.flag"
    assert definitions[0].scoring_weight_milli == 1500


def test_scheduler_lease_is_project_scoped_and_atomic(empty_data: FixtureData):
    app, _ = empty_data
    project = app.prj_mgr.init_project(name="lease_ad", max_nof_users=5)
    config = app.ad.initialize_project(project, tick_seconds=10)
    now = config.created_at

    assert app.ad.managers.config.acquire_scheduler_lease(config.id, "worker-a", now)
    assert app.ad.managers.config.acquire_scheduler_lease(config.id, "worker-b", now) is None
    app.ad.managers.config.release_scheduler_lease(config.id, "worker-a")
    assert app.ad.managers.config.acquire_scheduler_lease(config.id, "worker-b", now)
