import json
import threading
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import urlopen

import click
import mongomock
import pytest
import yaml
from click.testing import CliRunner
from rich.console import Console

from fit_ctf.ad.http_api import create_http_server
from fit_ctf.ad.scoreboard_config import Coefficients, ScoreboardSettings
from fit_ctf.ad.scoreboard_html import render_scoreboard_html
from fit_ctf.ad.scoreboard_terminal import scoreboard_renderable
from fit_ctf.components.container_client.mock_client import MockClient
from fit_ctf.ctf_base import CTFBase
from fit_ctf.models.ad.managers import ADConfigError
from fit_ctf_cli.cli.ad import ad
from fit_ctf_rendezvous.rendezvous_core import RendezvousCore


@pytest.fixture
def competition(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DEST", str(tmp_path))
    paths = {name: tmp_path / name for name in ("projects", "users", "modules", "scenarios")}
    for path in paths.values():
        path.mkdir()
    app = CTFBase(
        {"db_name": "scoreboard_test", "db_password": "secret"},
        paths,
        mongomock.MongoClient(tz_aware=True),
        MockClient,
    )
    project = app.prj_mgr.init_project("score_match", max_nof_users=8)
    app.ad.initialize_project(project, tick_seconds=120, tick_count=3)
    app.team_enroll_mgr.list_for_project = lambda p, active=True: [
        e
        for e in app.team_enroll_mgr.get_docs(active=active)
        if e.project_id.id == p.id and e.state == "active"
    ]
    teams = [app.team_mgr.create_team(name) for name in ("red", "blue", "green")]
    enrolled = [app.team_enroll_mgr.enroll(team, project)[0] for team in teams]
    # Two flag locations on the same service; display must group them into one service.
    for key in ("public", "private"):
        app.ad.add_service_definition(project, key, "web", "api", f"flags/{key}")
    values, failures, operations = {}, {}, []

    def checker_for(project, instance, tick):
        def call(operation, expected=None):
            operations.append((instance.id, operation))
            if operation == "validate":
                return {"status": "up", "code": "VALID"}
            if instance.id in failures:
                return {"status": failures[instance.id], "code": "TEST_FAILURE"}
            if operation == "plant":
                values[instance.id] = expected
            ok = operation == "health" or values.get(instance.id) == expected
            return {"status": "up" if ok else "corrupt", "code": "OK" if ok else "FLAG_MISMATCH"}

        return SimpleNamespace(call=call)

    monkeypatch.setattr(app.ad, "checker_for", checker_for)
    return app, project, enrolled, failures, operations


def configure(app, project, *, ma=2, mb=0.5, mc=3, html=False):
    return app.ad.configure_scoreboard(
        project, ScoreboardSettings(coefficients=Coefficients(ma=ma, mb=mb, mc=mc), html=html)
    )


def test_weighted_totals_zero_rows_sla_and_completed_snapshot(competition):
    app, project, teams, failures, operations = competition
    configure(app, project)
    assert len(app.ad.scoreboard(project)) == 3
    assert all(row["total"] == 0 for row in app.ad.scoreboard(project))
    app.ad.start_project(project)
    tick = app.ad.open_tick(project)
    flag = next(
        f
        for f in app.ad.managers.flags.get_docs(project_id=project.id)
        if f.owner_team_enrollment_id == teams[1].id
    )
    assert app.ad.submit(project, teams[0], app.ad.crypto.decrypt(flag.encrypted_value)).accepted
    failing = next(
        i
        for i in app.ad.managers.instances.get_docs(project_id=project.id)
        if i.team_enrollment_id == teams[1].id
    )
    failures[failing.id] = "down"
    app.ad.close_tick(project)
    rows = {r["team"]: r for r in app.ad.scoreboard(project)}
    assert (rows["red"]["attack"], rows["red"]["defense"], rows["red"]["sla"]) == (10, 10, 5)
    assert rows["red"]["weighted"] == {"attack": 20, "defense": 5, "sla": 15}
    assert rows["red"]["total"] == 40
    assert rows["blue"]["sla"] == 0 and rows["blue"]["total"] == 2.5
    assert rows["green"]["total"] == 20
    # Replaying scoring does not discard cumulative history or double count.
    app.ad._score_tick(project, app.ad.managers.ticks.latest(project.id))
    assert app.ad.scoreboard(project)[0]["total"] == 40
    app.ad.open_tick(project, now=tick.active_at + timedelta(seconds=120))
    assert app.ad.scoreboard(project)[0]["tick"] == 0
    assert app.ad.scoreboard(project)[0]["total"] == 40
    app.ad.close_tick(project)
    closed = app.ad.managers.ticks.latest(project.id)
    app.ad._score_tick(project, closed)
    assert app.ad.scoreboard(project)[0]["total"] == 60
    before = len(operations)
    app.ad.scoreboard_state(project)
    assert len(operations) == before  # Viewing never probes services.


@pytest.mark.parametrize("failure", ["down", "corrupt", "checker_error"])
def test_failed_get_flag_never_earns_sla(competition, failure):
    app, project, teams, failures, _ = competition
    app.ad.start_project(project)
    app.ad.open_tick(project)
    instance = next(
        i
        for i in app.ad.managers.instances.get_docs(project_id=project.id)
        if i.team_enrollment_id == teams[0].id
    )
    failures[instance.id] = failure
    app.ad.close_tick(project)
    rows = {r["team_id"]: r for r in app.ad.scoreboard(project)}
    assert rows[str(teams[0].id)]["sla"] == 0
    assert rows[str(teams[1].id)]["sla"] == 5


def test_missing_result_no_sla_and_partial_scores_not_published(competition):
    app, project, teams, _, _ = competition
    app.ad.start_project(project)
    tick = app.ad.open_tick(project)
    app.ad.close_tick(project)
    app.ad.managers.checks.collection.delete_one({"owner_team_enrollment_id": teams[0].id})
    app.ad._score_tick(project, tick)
    assert (
        next(r for r in app.ad.scoreboard(project) if r["team_id"] == str(teams[0].id))["sla"] == 0
    )
    app.ad.managers.ticks.collection.update_one({"_id": tick.id}, {"$set": {"state": "scoring"}})
    assert all(r["total"] == 0 for r in app.ad.scoreboard(project))


def test_previous_tick_flag_scores_in_submission_tick(competition):
    app, project, teams, _, _ = competition
    app.ad.start_project(project)
    first = app.ad.open_tick(project)
    flag = next(
        f
        for f in app.ad.managers.flags.get_docs(project_id=project.id)
        if f.owner_team_enrollment_id == teams[1].id
    )
    app.ad.advance(project, now=first.active_at + timedelta(seconds=120))
    result = app.ad.submit(
        project,
        teams[0],
        app.ad.crypto.decrypt(flag.encrypted_value),
        now=first.active_at + timedelta(seconds=121),
    )
    assert result.accepted and result.tick == 1
    assert all(r["attack"] == 0 for r in app.ad.scoreboard(project))
    app.ad.close_tick(project)
    assert (
        next(r for r in app.ad.scoreboard(project) if r["team_id"] == str(teams[0].id))["attack"]
        == 10
    )


def test_author_yaml_rounding_and_zero_coefficients(competition, tmp_path):
    app, project, teams, _, _ = competition
    checker_file = tmp_path / "checker.py"
    checker_file.write_text("def create_checker(context): pass\n")
    spec = {
        "version": 2,
        "ticks": {"seconds": 30, "count": 2},
        "scoreboard": {"coefficients": {"ma": 0.00005, "mb": 0, "mc": 0}, "html": True},
        "flags": {
            "secret": {
                "cluster": "web",
                "node": "app",
                "service": "api",
                "checker": {"entrypoint": "checker.py:create_checker"},
            }
        },
    }
    file = tmp_path / "competition.yaml"
    file.write_text(yaml.safe_dump(spec))
    app.ad.configure_flags(project, file)
    config = app.ad._config(project)
    assert (config.ma, config.mb, config.mc) == (0.00005, 0, 0)
    assert config.scoreboard_html_enabled and config.tick_seconds == 30
    app.ad.start_project(project)
    app.ad.open_tick(project)
    flag = next(
        f
        for f in app.ad.managers.flags.get_docs(project_id=project.id)
        if f.owner_team_enrollment_id == teams[1].id
    )
    app.ad.submit(project, teams[0], app.ad.crypto.decrypt(flag.encrypted_value))
    app.ad.close_tick(project)
    rows = app.ad.scoreboard(project)
    assert rows[0]["total"] == 0.001  # 10 * 0.00005 = 0.0005, half-up to a milli-point
    assert all(r["total"] == 0 for r in rows[1:])
    assert rows[0]["sla"] == 5  # Zero coefficient affects total, not raw components.


def test_worker_health_aggregation_staleness_and_tick_timers(competition):
    app, project, teams, failures, operations = competition
    app.ad.start_project(project)
    tick = app.ad.open_tick(project)
    now = tick.active_at + timedelta(seconds=1)
    state = app.ad.scoreboard_state(project, now=now)
    assert state["tick"]["seconds_remaining"] == pytest.approx(119, abs=0.002)
    assert state["tick"]["match_seconds_remaining"] == pytest.approx(359, abs=0.002)
    assert len(state["services"]) == 3  # six flag locations, three services
    assert all(s["status"] == "up" and s["flag_locations"] == 2 for s in state["services"])
    instance = next(
        i
        for i in app.ad.managers.instances.get_docs(project_id=project.id)
        if i.team_enrollment_id == teams[0].id
    )
    failures[instance.id] = "checker_error"
    app.ad.run_due_projects(now=now)
    assert any(s["status"] == "checker_error" for s in app.ad.scoreboard_state(project)["services"])
    calls = len(operations)
    app.ad.run_due_projects(now=now + timedelta(seconds=1))
    assert len(operations) == calls  # monitor respects its interval
    stale = app.ad.scoreboard_state(project, now=now + timedelta(seconds=60))
    assert all(s["status"] == "stale" and s["active"] is None for s in stale["services"])
    app.ad.pause_project(project)
    paused = app.ad.scoreboard_state(project)
    assert paused["tick"]["deadline"] is None
    assert paused["tick"]["match_seconds_remaining"] == 240
    app.ad.resume_project(project)
    app.ad.open_tick(project)
    app.ad.finish_project(project)
    assert app.ad.scoreboard_state(project)["tick"]["match_seconds_remaining"] == 0


def test_settings_immutable_scoring_html_runtime_toggle_and_http(competition):
    app, project, _, _, _ = competition
    path = configure(app, project, html=True)
    assert path.is_file() and "initial-state" in path.read_text()
    app.ad.start_project(project)
    with pytest.raises(ADConfigError):
        configure(app, project, ma=7)
    server = create_http_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}/api/v1/projects/{project.name}/ad"
    try:
        with urlopen(base + "/scoreboard-state") as response:
            state = json.load(response)
        assert state["coefficients"] == {"ma": 2, "mb": 0.5, "mc": 3}
        with urlopen(base + "/scoreboard.html") as response:
            assert response.headers.get_content_type() == "text/html"
            assert b"Team services" in response.read()
        configure(app, project, html=False)
        with pytest.raises(HTTPError) as exc:
            urlopen(base + "/scoreboard.html")
        assert exc.value.code == 404
        with urlopen(base + "/scoreboard") as response:
            assert len(json.load(response)) == 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_safe_html_terminal_and_cli(competition):
    app, project, _, _, _ = competition
    team = app.team_mgr.get_team("red")
    team.display_name = '</script><img src=x onerror="alert(1)">[bold]'
    app.team_mgr.update_doc(team)
    state = app.ad.scoreboard_state(project)
    html = render_scoreboard_html(state)
    assert "</script><img" not in html
    assert "\\u003c/script\\u003e" in html
    assert "token_hash" not in html and "encrypted_value" not in html
    stream = StringIO()
    Console(file=stream, width=150).print(scoreboard_renderable(state))
    assert "[bold]" in stream.getvalue()  # Rich markup in names is literal.
    assert "Team services" in stream.getvalue() and "Current tick" in stream.getvalue()
    root = click.Group()
    root.add_command(ad)
    result = CliRunner().invoke(
        root,
        [
            "ad",
            "scoreboard-config",
            "-pn",
            project.name,
            "--ma",
            "2",
            "--mb",
            "0.5",
            "--mc",
            "3",
            "--html",
        ],
        obj={"ctf_app": app},
    )
    assert result.exit_code == 0, result.output
    result = CliRunner().invoke(
        root, ["ad", "scoreboard", "-pn", project.name], obj={"ctf_app": app}
    )
    assert result.exit_code == 0, result.output
    assert "Team services" in result.output


def test_team_members_can_select_project_without_individual_enrollment(competition):
    app, project, teams, _, _ = competition
    user = app.user_mgr.create_new_user("alice", "Password123!")[0]
    enrollment = teams[0]
    enrollment.member_ids_snapshot = [user.id]
    app.team_enroll_mgr.update_doc(enrollment)
    core = RendezvousCore(app)
    core.active_user = user
    core.selected_project = project
    assert project.id in {p.id for p in core.get_active_projects()}
    assert len(core.get_ad_scoreboard()["teams"]) == 3
    other = app.user_mgr.create_new_user("outsider", "Password123!")[0]
    core.active_user = other
    with pytest.raises(Exception, match="not enrolled"):
        core.get_ad_scoreboard()


@pytest.mark.asyncio
async def test_player_console_auto_opens_and_updates(competition):
    from textual.widgets import Static

    from fit_ctf_rendezvous.rendezvous_app import RendezvousApp
    from fit_ctf_rendezvous.screens.app_screen.app_screen import AppScreen

    app, project, teams, _, _ = competition
    user = app.user_mgr.create_new_user("player", "Password123!")[0]
    teams[0].member_ids_snapshot = [user.id]
    app.team_enroll_mgr.update_doc(teams[0])
    tui = RendezvousApp(app)
    async with tui.run_test(size=(140, 45)) as pilot:
        tui.core_mgr.active_user = user
        tui.on_login_submit()
        await pilot.pause()
        screen = tui.screen
        assert isinstance(screen, AppScreen) and screen.page_state == "scoreboard"
        page = screen.query_one("#scoreboard-page")
        await page.refresh_scoreboard()
        assert len(page._state["teams"]) == 3
        assert page._state["project"] == project.name
        assert page.query_one(Static).render() is not None
        app.ad.start_project(project)
        app.ad.open_tick(project)
        page._received = 0
        await page.refresh_scoreboard()
        assert page._state["tick"]["state"] == "active"
        assert all(s["status"] == "up" for s in page._state["services"])
        # Navigation away and back must recreate the refresh timer correctly.
        await screen._async_page_select("home")
        await screen._async_page_select("scoreboard")
        await pilot.pause()
        assert screen.query_one("#scoreboard-page").is_mounted
