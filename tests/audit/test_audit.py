import asyncio
import base64
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import click
import mongomock
import pytest
from click.testing import CliRunner

from fit_ctf.ad.audit import AuditLog, file_lock
from fit_ctf.ad.audit_worker import Collector
from fit_ctf.ad.http_api import create_http_server
from fit_ctf.components.container_client.mock_client import MockClient
from fit_ctf.ctf_base import CTFBase
from fit_ctf.models.core.team_enrollment import hash_team_token
from fit_ctf_cli.cli.ad import ad


@pytest.fixture
def competition(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DEST", str(tmp_path))
    paths = {key: tmp_path / key for key in ("projects", "users", "modules", "scenarios")}
    for path in paths.values():
        path.mkdir()
    app = CTFBase(
        {"db_name": "audit_test", "db_password": "test-secret"},
        paths,
        mongomock.MongoClient(tz_aware=True),
        MockClient,
    )
    project = app.prj_mgr.init_project("audit_match", max_nof_users=4)
    app.ad.initialize_project(project, tick_seconds=10)
    teams = [app.team_mgr.create_team(name) for name in ("red", "blue")]
    enrolled = [app.team_enroll_mgr.enroll(team, project) for team in teams]

    # mongomock cannot query MongoDB's DBRef.$id fields. Adapt those existing
    # repository lookups only; the submission service and audit paths are real.
    def list_enrolled(p, active=True):
        return [e for e in app.team_enroll_mgr.get_docs(active=active) if e.project_id.id == p.id]

    app.team_enroll_mgr.list_for_project = list_enrolled
    app.team_enroll_mgr.authenticate_token = lambda p, token: next(
        (e for e in list_enrolled(p) if e.token_hash == hash_team_token(token)), None
    )
    app.ad.add_service_definition(project, "notes", "notes", "web")
    app.ad.start_project(project)
    app.ad.open_tick(project)
    return app, project, enrolled


def test_independent_switches_and_atomic_json(tmp_path):
    log = AuditLog(tmp_path)
    assert not log.write("flags", flag="disabled")
    log.set_enabled("flags", True)
    assert log.write("flags", flag="hello\nworld", team="red", user="alice")
    assert not log.write("terminal", command="disabled")
    log.set_enabled("terminal", True)
    log.terminal_input("alice", "s", "echo čau\n".encode())
    log.set_enabled("flags", False)
    assert not log.write("flags", flag="disabled again")
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda n: log.write("terminal", command=f"cmd-{n}"), range(30)))
    records = json.loads(log.path.read_text())
    assert len(records) == 32
    assert records[0]["flag"] == "hello\nworld"
    assert records[1]["command"] == "echo čau"
    assert all(next(iter(record)) == "timestamp" for record in records)
    assert all(record["timestamp"].endswith("+00:00") for record in records)


def test_process_writers_do_not_lose_records(tmp_path):
    log = AuditLog(tmp_path)
    log.set_enabled("flags", True)
    code = (
        "from fit_ctf.ad.audit import AuditLog; import sys; l=AuditLog(sys.argv[1]); "
        "[l.write('flags',flag=str(n)) for n in range(8)]"
    )
    processes = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path)]) for _ in range(3)]
    assert all(process.wait(timeout=30) == 0 for process in processes)
    assert len(json.loads(log.path.read_text())) == 24


def test_all_domain_submission_results_are_audited(competition):
    app, project, enrolled = competition
    log = app.audit.log(project)
    log.set_enabled("flags", True)
    victim = app.ad.managers.flags.get_doc_by_filter(owner_team_enrollment_id=enrolled[1][0].id)
    flag = app.ad.crypto.decrypt(victim.encrypted_value)
    assert app.ad.submit(project, enrolled[0][0], "bad", user="alice").code == "INVALID_FORMAT"
    assert app.ad.submit(project, enrolled[0][0], flag, user="alice").code == "ACCEPTED"
    assert app.ad.submit(project, enrolled[0][0], flag).code == "DUPLICATE"
    assert app.ad.submit(project, enrolled[1][0], flag).code == "OWN_FLAG"
    records = json.loads(log.path.read_text())
    assert [record["result"] for record in records] == [
        "INVALID_FORMAT",
        "ACCEPTED",
        "DUPLICATE",
        "OWN_FLAG",
    ]
    assert records[0]["team"] == "red" and records[0]["user"] == "alice"
    assert not records[0]["user_verified"]
    assert records[2]["user"] is None


def test_http_rejections_and_batch_attempts(competition):
    app, project, enrolled = competition
    log = app.audit.log(project)
    log.set_enabled("flags", True)
    server = create_http_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/api/v1/projects/{project.name}/ad/submissions"

    def post(data, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(url, data=data, headers=headers)
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status

    try:
        assert post(b'{"flag":"stolen","user":"alice"}') == 401
        assert post(b'{"flag":"wrong-token"}', "invalid") == 401
        assert post(b'{"flags":["one","two"],"user":"alice"}', enrolled[0][1]) == 200
        assert post(b"broken json") == 400
        assert post(b"[]") == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    records = json.loads(log.path.read_text())
    assert len(records) == 6
    assert [r["result"] for r in records] == [
        "UNAUTHORIZED",
        "UNAUTHORIZED",
        "INVALID_FORMAT",
        "INVALID_FORMAT",
        "INVALID_JSON",
        "INVALID_BODY",
    ]
    assert records[0]["flag"] == "stolen" and records[0]["team"] is None
    assert records[2]["team"] == "red"


def test_three_cli_switches(competition, monkeypatch):
    app, project, _ = competition
    monkeypatch.setattr(type(app.audit), "start_collector", lambda *a: None)
    root = click.Group()
    root.add_command(ad)
    runner = CliRunner()
    for category in ("terminal", "infrastructure", "flags"):
        for option, expected in [("--enabled", True), ("--disabled", False)]:
            result = runner.invoke(
                root, ["ad", "logging", category, "-pn", project.name, option], obj={"ctf_app": app}
            )
            assert result.exit_code == 0, (result.output, result.exception)
            assert app.audit.log(project).settings()["enabled"][category] is expected


def test_invalid_cli_team_attempt_is_logged(competition):
    app, project, _ = competition
    log = app.audit.log(project)
    log.set_enabled("flags", True)
    root = click.Group()
    root.add_command(ad)
    result = CliRunner().invoke(
        root,
        [
            "ad",
            "submit",
            "-pn",
            project.name,
            "-t",
            "missing",
            "--flag",
            "FITCTF{example}",
            "--username",
            "alice",
        ],
        obj={"ctf_app": app},
    )
    assert result.exit_code == 1
    record = json.loads(log.path.read_text())[0]
    assert record["result"] == "UNAUTHORIZED_TEAM" and record["user"] == "alice"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux SSH audit collector")
def test_switch_starts_and_stops_real_detached_collector(competition):
    app, project, _ = competition
    log = app.audit.log(project)
    directory = log.terminal_directory("alice")
    try:
        app.audit.configure(project, "terminal", True)
        assert (directory / "input.sock").exists()
        ready = json.loads((log.root / "collector-ready.json").read_text())
        assert ready["revision"] == log.settings()["revision"]
    finally:
        app.audit.configure(project, "terminal", False)
        with file_lock(log.root / ".collector.lock"):
            pass


def test_engine_messages_and_nonzero_exit_are_audited(competition):
    app, project, _ = competition
    app.audit.log(project).set_enabled("infrastructure", True)

    async def execute():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import sys; print('engine failed',file=sys.stderr); sys.exit(7)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await app.c_client._process_logging(process, project.name)

    asyncio.run(execute())
    records = json.loads(app.audit.log(project).path.read_text())
    assert records[0]["message"] == "engine failed"
    assert "exit 7" in records[1]["message"]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux SSH PTY recorder")
def test_real_pty_and_nested_shell_input_is_recorded(tmp_path):
    import importlib.util

    # Keep the socket connect path short even when pytest uses a long temporary directory.
    log = AuditLog(tmp_path)
    log.set_enabled("terminal", True)
    directory = log.terminal_directory("alice")
    collector = Collector(log, "test", "unused")
    collector.register_sockets(log.settings())
    script = Path(__file__).resolve().parents[2] / "src/fit_ctf/templates/v1/audit_session.py"
    spec = importlib.util.spec_from_file_location("recorder", script)
    assert spec
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        socket_path = f"/proc/{os.getpid()}/fd/{directory_fd}/input.sock"
        code = (
            "import importlib.util,sys; s=importlib.util.spec_from_file_location('r',sys.argv[1]); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            "sys.exit(m.main(sys.argv[2],sys.argv[3]))"
        )
        process = subprocess.run(
            [sys.executable, "-c", code, str(script), socket_path, str(log.control)],
            input=b"echo outer\nsh\necho nested\nexit\nexit\n",
            capture_output=True,
            timeout=15,
            env={**os.environ, "SHELL": "/bin/sh", "SSH_ORIGINAL_COMMAND": ""},
        )
        assert process.returncode == 0, process.stderr
        records = json.loads(log.path.read_text())
        commands = [r["command"] for r in records if r.get("event") == "command"]
        assert "echo outer" in commands and "echo nested" in commands
        assert all(r["user"] == "alice" for r in records)
        raw = b"".join(
            base64.b64decode(r["input_base64"]) for r in records if r.get("event") == "input"
        )
        assert b"echo outer\nsh\necho nested\nexit\nexit\n" in raw
        assert b"nested" in process.stdout
    finally:
        os.close(directory_fd)
        collector.stop.set()
        for server in collector.sockets.values():
            server.close()


def test_event_filter_keeps_other_projects_out(tmp_path):
    log = AuditLog(tmp_path)
    log.set_enabled("infrastructure", True)
    collector = Collector(log, "match", "unused")
    for project in ["match", "other"]:
        collector.messages.put(
            (
                "events",
                "stdout",
                json.dumps(
                    {
                        "Action": "die",
                        "id": "container",
                        "Actor": {"Attributes": {"fit_ctf.project": project}},
                    }
                ),
            )
        )
    collector.consume()
    records = json.loads(log.path.read_text())
    assert len(records) == 1 and records[0]["event"] == "die"


@pytest.mark.parametrize("isolated", [False, True])
def test_login_node_audit_mounts_and_classic_compatibility(competition, monkeypatch, isolated):
    import yaml

    from fit_ctf.models.infra.config_models import ScenarioConfig
    from fit_ctf.templates import get_template, validate_variable_parse

    app, project, _ = competition
    user = SimpleNamespace(username="alice")
    monkeypatch.setattr(app.user_cluster_mgr, "get_user_and_project", lambda _: (user, project))
    monkeypatch.setattr(
        app.user_cluster_mgr,
        "_enrollment_for_cluster",
        lambda _: SimpleNamespace(container_port=10000, forwarded_port=10000),
    )
    monkeypatch.setattr(app.user_cluster_mgr, "is_ad_isolated", lambda _: isolated)
    cluster = SimpleNamespace(
        enrollment_id=SimpleNamespace(id="enrollment"),
        scenario_configs={"login_node": ScenarioConfig(scenario_name="login_node")},
    )
    params = app.user_cluster_mgr._compose_template_extras(cluster)
    params.update(
        paths__modules="/modules",
        network_map__shared="shared",
        network_map__private="private",
        network_map__mode="ad_isolated" if isolated else "classic",
        login_node__volume_map__home="/home",
        login_node__volume_map__shadow="/shadow",
    )
    template_dir = (
        Path(__file__).resolve().parents[2] / "src/fit_ctf/templates/v1/scenarios/login_node"
    )
    validate_variable_parse("scenario_compose.yaml.j2", template_dir, params)
    document = yaml.safe_load(
        get_template("scenario_compose.yaml.j2", str(template_dir)).render(**params)
    )
    mounts = document["services"]["login_node"]["volumes"]
    assert len(mounts) == (5 if isolated else 2)
    if isolated:
        assert Path(params["audit_session_script"]).is_file()
        assert any(m.endswith(":/run/fitctf-audit:ro") for m in mounts)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux SSH recorder")
def test_non_pty_exec_preserves_binary_input_and_output(tmp_path):
    script = Path(__file__).resolve().parents[2] / "src/fit_ctf/templates/v1/audit_session.py"
    code = (
        "import runpy,sys; m=runpy.run_path(sys.argv[1]); "
        "sys.exit(m['main'](sys.argv[2],sys.argv[3]))"
    )
    data = b"\x00binary\xff\x01\r\n"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(script),
            str(tmp_path / "missing.sock"),
            str(tmp_path / "missing.json"),
        ],
        input=data,
        capture_output=True,
        timeout=5,
        env={**os.environ, "SHELL": "/bin/sh", "SSH_ORIGINAL_COMMAND": "cat"},
    )
    assert result.returncode == 0 and result.stdout == data, result.stderr
