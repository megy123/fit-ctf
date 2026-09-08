import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fit_ctf.ad.http_api import create_http_server
from tests import FixtureData


def _request(url: str, *, token: str | None = None, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=data, headers=headers)
    try:
        response = urlopen(request, timeout=3)
    except HTTPError as exc:
        response = exc
    return response.status, json.loads(response.read())


def test_submission_and_scoreboard_http_api(empty_data: FixtureData):
    app, _ = empty_data
    project = app.prj_mgr.init_project(name="http_ad", max_nof_users=10)
    teams = [app.team_mgr.create_team(name) for name in ("red", "blue")]
    enrollments_and_tokens = [app.team_enroll_mgr.enroll(team, project) for team in teams]
    app.ad.initialize_project(project, tick_seconds=10)
    app.ad.add_service_definition(project, "notes", "notes_scenario", "web")
    app.ad.start_project(project)
    app.ad.open_tick(project)

    blue = enrollments_and_tokens[1][0]
    flag = app.ad.managers.flags.get_doc_by_filter(
        project_id=project.id, owner_team_enrollment_id=blue.id
    )
    value = app.ad.crypto.decrypt(flag.encrypted_value)

    server = create_http_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}/api/v1/projects/{project.name}/ad"
    try:
        status, body = _request(
            f"{base}/submissions",
            token=enrollments_and_tokens[0][1],
            payload={"flag": value},
        )
        assert status == 200
        assert body["code"] == "ACCEPTED"

        status, body = _request(
            f"{base}/submissions/batch",
            token=enrollments_and_tokens[0][1],
            payload={"flags": [value, "bad"]},
        )
        assert status == 200
        assert [item["code"] for item in body["results"]] == ["DUPLICATE", "INVALID_FORMAT"]

        app.ad.close_tick(project)
        status, scoreboard = _request(f"{base}/scoreboard")
        assert status == 200
        assert scoreboard[0]["team"] == "red"

        status, body = _request(f"{base}/submissions", payload={"flag": value})
        assert status == 401
        assert body["code"] == "UNAUTHORIZED"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
