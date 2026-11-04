import importlib.util
import json
import threading
from datetime import timedelta
from pathlib import Path

import click
import mongomock
import pytest
import yaml
from click.testing import CliRunner

from fit_ctf.ad.checkers import CheckerClient, FlagCatalogue
from fit_ctf.components.container_client.mock_client import MockClient
from fit_ctf.ctf_base import CTFBase
from fit_ctf.models.ad.managers import ADConfigError
from fit_ctf_cli.cli.ad import ad

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "docs/attack-defense/mock_checkers"


@pytest.fixture
def competition(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DEST", str(tmp_path))
    paths = {name: tmp_path / name for name in ("projects", "users", "modules", "scenarios")}
    for path in paths.values():
        path.mkdir()
    app = CTFBase(
        {"db_name": "checker_test", "db_password": "secret"},
        paths,
        mongomock.MongoClient(tz_aware=True),
        MockClient,
    )
    project = app.prj_mgr.init_project("checker_match", max_nof_users=4)
    app.ad.initialize_project(project)
    teams = [app.team_mgr.create_team(name) for name in ("red", "blue")]
    enrolled = [app.team_enroll_mgr.enroll(team, project)[0] for team in teams]
    # Work around mongomock's unsupported DBRef.$id query without changing runtime code.
    app.team_enroll_mgr.list_for_project = lambda p, active=True: [
        e for e in app.team_enroll_mgr.get_docs(active=active) if e.project_id.id == p.id
    ]
    spec = importlib.util.spec_from_file_location("demo_service", EXAMPLES / "demo_service.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    servers = []
    for team in teams:
        servers.extend(demo.create_servers(str(tmp_path / f"{team.name}.sqlite"), 0, 0))
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for thread in threads:
        thread.start()
    catalogue = yaml.safe_load((EXAMPLES / "competition.yaml").read_text())
    catalogue["ticks"].update(seconds=2, count=3, checker_timeout_seconds=2)
    for flag in catalogue["flags"].values():
        flag["checker"]["entrypoint"] = str(EXAMPLES / flag["checker"]["entrypoint"])
        params = flag["checker"]["params"]
        if "base_url" in params:
            params["base_url"] = (
                "http://127.0.0.1:{% if team == 'red' %}"
                + str(servers[0].server_port)
                + "{% else %}"
                + str(servers[2].server_port)
                + "{% endif %}"
            )
        else:
            params["port"] = (
                "{% if team == 'red' %}"
                + str(servers[1].server_address[1])
                + "{% else %}"
                + str(servers[3].server_address[1])
                + "{% endif %}"
            )
    catalogue["flags"]["queue_secret"]["ticks"] = [0, 2]
    file = tmp_path / "competition.yaml"
    file.write_text(yaml.safe_dump(catalogue))
    app.ad.configure_flags(project, file)
    try:
        yield app, project, enrolled, servers, file
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=3)


def flags(app, project, number):
    return app.ad.managers.flags.get_docs(project_id=project.id, tick_number=number)


def test_live_http_sqlite_tcp_plant_check_rotation_and_tick_limit(competition):
    app, project, teams, _, _ = competition
    app.ad.start_project(project)
    tick = app.ad.open_tick(project)
    first = flags(app, project, 0)
    assert len(first) == tick.planted_instance_count == 6
    assert len({app.ad.crypto.decrypt(f.encrypted_value) for f in first}) == 6
    assert all(f.state == "planted" for f in first)
    assert all(
        not Path(i.flag_path).exists()
        for i in app.ad.managers.instances.get_docs(project_id=project.id)
    )
    victim = next(f for f in first if f.owner_team_enrollment_id == teams[1].id)
    assert app.ad.submit(project, teams[0], app.ad.crypto.decrypt(victim.encrypted_value)).accepted
    second = app.ad.advance(project, now=tick.active_at + timedelta(seconds=2))
    assert second.number == 1 and len(flags(app, project, 1)) == 4  # TCP flag not scheduled
    assert (
        app.ad.submit(
            project,
            teams[0],
            app.ad.crypto.decrypt(victim.encrypted_value),
            now=victim.valid_until + timedelta(seconds=1),
        ).code
        == "UNKNOWN_OR_EXPIRED"
    )
    third = app.ad.advance(project, now=second.active_at + timedelta(seconds=2))
    assert third.number == 2 and len(flags(app, project, 2)) == 6
    last = app.ad.advance(project, now=third.active_at + timedelta(seconds=2))
    assert last.number == 2 and last.state == "closed"
    assert app.ad._config(project).state == "finished"
    assert len(app.ad.managers.ticks.get_docs(project_id=project.id)) == 3
    checks = app.ad.managers.checks.get_docs(project_id=project.id)
    assert len(checks) == 16 and all(c.status == "up" for c in checks)


def test_down_attacker_cannot_submit_and_recovers(competition):
    app, project, teams, servers, _ = competition
    app.ad.start_project(project)
    tick = app.ad.open_tick(project)
    victim = next(f for f in flags(app, project, 0) if f.owner_team_enrollment_id == teams[1].id)
    value = app.ad.crypto.decrypt(victim.encrypted_value)
    # Pause the Red HTTP listener; Blue is on a separate running service.
    servers[0].shutdown()
    result = app.ad.submit(project, teams[0], value, now=tick.active_at)
    assert not result.accepted and result.code == "TEAM_SERVICE_UNAVAILABLE"
    threading.Thread(target=servers[0].serve_forever, daemon=True).start()
    # Freeze logical time to separate service recovery from flag expiration.
    assert app.ad.submit(project, teams[0], value, now=tick.active_at).accepted


def test_corrupted_own_flag_blocks_even_replayed_request(competition):
    app, project, teams, _, _ = competition
    app.ad.start_project(project)
    tick = app.ad.open_tick(project)
    own = next(f for f in flags(app, project, 0) if f.owner_team_enrollment_id == teams[0].id)
    victim = next(f for f in flags(app, project, 0) if f.owner_team_enrollment_id == teams[1].id)
    stolen = app.ad.crypto.decrypt(victim.encrypted_value)
    assert app.ad.submit(project, teams[0], stolen, request_id="repeat").accepted
    instance = app.ad.managers.instances.get_doc_by_id(own.service_instance_id)
    checker = app.ad.checker_for(project, instance, tick.number)
    assert checker.call("plant", "CORRUPTED")["status"] == "up"
    assert (
        app.ad.submit(project, teams[0], stolen, request_id="repeat").code
        == "TEAM_SERVICE_UNAVAILABLE"
    )
    checker.call("plant", app.ad.crypto.decrypt(own.encrypted_value))
    assert app.ad.submit(project, teams[0], stolen, request_id="repeat").accepted


def test_worker_stops_after_authored_count(competition):
    app, project, _, _, _ = competition
    app.ad.start_project(project)
    first = app.ad.run_due_projects()[0]
    second = app.ad.run_due_projects(now=first.active_at + timedelta(seconds=2))[0]
    third = app.ad.run_due_projects(now=second.active_at + timedelta(seconds=2))[0]
    closed = app.ad.run_due_projects(now=third.active_at + timedelta(seconds=2))[0]
    assert closed.state == "closed"
    assert app.ad.run_due_projects(now=third.active_at + timedelta(seconds=20)) == []


def test_cli_load_and_refuse_changes_after_start(competition):
    app, project, _, _, file = competition
    root = click.Group()
    root.add_command(ad)
    result = CliRunner().invoke(
        root, ["ad", "flags-load", "-pn", project.name, "-f", str(file)], obj={"ctf_app": app}
    )
    assert result.exit_code == 0, result.output
    assert "3 flag definitions" in result.output
    app.ad.start_project(project)
    with pytest.raises(ADConfigError):
        app.ad.configure_flags(project, file)


@pytest.mark.parametrize(
    "body,code",
    [
        ("import time\ndef create_checker(c): time.sleep(3)", "CHECKER_TIMEOUT"),
        ("def create_checker(c): raise RuntimeError('sensitive flag')", "RuntimeError"),
        ("def create_checker(c): return object()", "AttributeError"),
    ],
)
def test_checker_failures_are_bounded_and_redacted(tmp_path, body, code):
    path = tmp_path / "broken.py"
    path.write_text(body)
    result = CheckerClient(f"{path}:create_checker", {}, 0.7).call("health")
    assert result["status"] == "checker_error" and result["code"] == code
    assert "sensitive" not in json.dumps(result)


def test_schedule_validation_requires_checker_and_bounded_ticks():
    raw = yaml.safe_load((EXAMPLES / "competition.yaml").read_text())
    raw["flags"]["notes_public"]["ticks"] = [999]
    with pytest.raises(ValueError):
        FlagCatalogue.model_validate(raw)
    raw["flags"]["notes_public"].pop("ticks")
    raw["flags"]["notes_public"].pop("checker")
    with pytest.raises(ValueError):
        FlagCatalogue.model_validate(raw)


@pytest.mark.parametrize(
    "check,put,get,operation,status,code",
    [
        ("'yes'", "None", "'flag'", "check", "checker_error", "TypeError"),
        ("True", "flag", "'flag'", "plant", "checker_error", "TypeError"),
        ("True", "None", "123", "check", "checker_error", "TypeError"),
        ("True", "None", "'wrong'", "plant", "corrupt", "FLAG_MISMATCH"),
    ],
)
def test_checker_contract_and_readback(tmp_path, check, put, get, operation, status, code):
    file = tmp_path / "checker.py"
    file.write_text(
        f"class Checker:\n"
        f" def check_service(self): return {check}\n"
        f" def put_flag(self, flag): return {put}\n"
        f" def get_flag(self): return {get}\n"
        "def create_checker(context): return Checker()\n"
    )
    result = CheckerClient(f"{file}:create_checker", {}, 2).call(operation, "flag")
    assert result == {"status": status, "code": code}


def test_checker_target_is_bound_to_compiled_topology(competition):
    app, project, teams, _, _ = competition
    instance = app.ad.managers.instances.get_doc_by_filter(
        team_enrollment_id=teams[0].id,
        service_definition_id=app.ad.managers.definitions.get_doc_by_filter(key="notes_public").id,
    )
    node_key = "team/red/web/node/application"
    app.ctf_db["ad_topology"].insert_one(
        {
            "project_id": project.id,
            "plan": {
                "nodes": {node_key: {"interfaces": {"lan": "10.192.0.10"}, "services": {"api": {}}}}
            },
        }
    )
    definition = app.ad.managers.definitions.get_doc_by_id(instance.service_definition_id)
    definition.checker_params["base_url"] = "http://{{ address }}:8000"
    app.ad.managers.definitions.update_doc(definition)
    client = app.ad.checker_for(project, instance, 2)
    assert client.context["params"]["base_url"] == "http://10.192.0.10:8000"
    assert client.context["team"] == "red" and client.context["tick"] == 2
    app.ctf_db["ad_topology"].update_one({"project_id": project.id}, {"$set": {"plan.nodes": {}}})
    with pytest.raises(ADConfigError, match="topology target does not exist"):
        app.ad.checker_for(project, instance, 2)


def test_flag_locations_can_include_their_creation_tick(competition):
    app, project, teams, _, _ = competition
    definition = app.ad.managers.definitions.get_doc_by_filter(key="queue_secret")
    definition.checker_params["key"] = "{{ team }}/{{ flag_key }}/{{ tick }}"
    app.ad.managers.definitions.update_doc(definition)
    app.ad.start_project(project)
    first = app.ad.open_tick(project)
    second = app.ad.advance(project, now=first.active_at + timedelta(seconds=2))
    victim = next(f for f in flags(app, project, 1) if f.owner_team_enrollment_id == teams[1].id)
    # The unscheduled TCP flag still lives at its tick-0 location.
    assert app.ad.submit(
        project, teams[0], app.ad.crypto.decrypt(victim.encrypted_value), now=second.active_at
    ).accepted


def test_preflight_rejects_team_services_without_a_checker(competition):
    app, project, _, _, _ = competition
    app.ctf_db["ad_topology"].insert_one(
        {
            "project_id": project.id,
            "plan": {
                "nodes": {"team/red/operations/node/observability": {"services": {"metrics": {}}}}
            },
        }
    )
    with pytest.raises(ADConfigError, match="operations/observability/metrics"):
        app.ad.preflight(project)


def test_real_deployment_requires_explicit_author_checker(competition, monkeypatch):
    app, project, _, _, _ = competition
    definition = app.ad.managers.definitions.get_doc_by_filter(key="notes_public")
    definition.checker_entrypoint = None
    app.ad.managers.definitions.update_doc(definition)
    instance = app.ad.managers.instances.get_doc_by_filter(service_definition_id=definition.id)
    monkeypatch.setattr(app, "_c_client", object())
    with pytest.raises(ADConfigError, match="requires an author checker"):
        app.ad.checker_for(project, instance, 0)
