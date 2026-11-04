"""Dependency-free HTTP submission and scoreboard API.

The transport is intentionally thin; all authoritative validation remains in
AttackDefenseService.  It can later be replaced by an ASGI adapter without changing
the domain layer.
"""

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

MAX_BODY_BYTES = 64 * 1024


def create_http_server(app, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "fit-ctf-ad/1"

        def _json(self, status: HTTPStatus, payload: dict | list) -> None:
            context = getattr(self, "_audit_context", None)
            if context and not getattr(self, "_audit_recorded", False):
                try:
                    project = app.prj_mgr.get_project(context["project"])
                except Exception:
                    project = None
                if project:
                    values = context.get("values", [None])
                    for value in values:
                        app.audit.submission(
                            project,
                            context.get("enrollment"),
                            value,
                            payload.get("code", "REQUEST_FAILED"),
                            user=context.get("user"),
                            source_ip=self.client_address[0],
                        )
                self._audit_recorded = True
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _route(self):
            parts = [item for item in urlparse(self.path).path.split("/") if item]
            if len(parts) < 6 or parts[:3] != ["api", "v1", "projects"]:
                return None, None
            if parts[4] != "ad":
                return None, None
            return parts[3], "/".join(parts[5:])

        def do_GET(self):  # noqa: N802
            project_name, action = self._route()
            if not project_name:
                self._json(HTTPStatus.NOT_FOUND, {"code": "NOT_FOUND"})
                return
            try:
                project = app.prj_mgr.get_project(project_name)
                config = app.ad.managers.config.get_for_project(project.id)
                if action == "scoreboard":
                    self._json(HTTPStatus.OK, app.ad.scoreboard(project))
                    return
                if action == "scoreboard-state":
                    self._json(HTTPStatus.OK, app.ad.scoreboard_state(project))
                    return
                if action == "scoreboard.html" and config.scoreboard_html_enabled:
                    from fit_ctf.ad.scoreboard_html import render_scoreboard_html

                    body = render_scoreboard_html(app.ad.scoreboard_state(project)).encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if action == "status":
                    tick = app.ad.managers.ticks.latest(project.id)
                    self._json(
                        HTTPStatus.OK,
                        {
                            "state": config.state,
                            "mode": config.mode,
                            "latest_tick": tick.number if tick else None,
                            "tick_state": tick.state if tick else None,
                        },
                    )
                    return
            except Exception:
                self._json(HTTPStatus.NOT_FOUND, {"code": "PROJECT_NOT_FOUND"})
                return
            self._json(HTTPStatus.NOT_FOUND, {"code": "NOT_FOUND"})

        def do_POST(self):  # noqa: N802
            project_name, action = self._route()
            self._audit_context = None
            self._audit_recorded = False
            if not project_name or action not in {"submissions", "submissions/batch"}:
                self._json(HTTPStatus.NOT_FOUND, {"code": "NOT_FOUND"})
                return
            self._audit_context = {"project": project_name}
            raw_length = self.headers.get("Content-Length", "0")
            try:
                length = int(raw_length)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY_BYTES:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"code": "INVALID_BODY"})
                return
            if self.headers.get_content_type() != "application/json":
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"code": "INVALID_CONTENT_TYPE"})
                return
            try:
                payload = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"code": "INVALID_JSON"})
                return
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"code": "INVALID_BODY"})
                return
            values = payload.get("flags", [payload.get("flag")])
            self._audit_context.update(
                values=values if isinstance(values, list) and len(values) <= 100 else [values],
                user=payload.get("user") if isinstance(payload.get("user"), str) else None,
            )
            authorization = self.headers.get("Authorization", "")
            if not authorization.startswith("Bearer "):
                self._json(HTTPStatus.UNAUTHORIZED, {"code": "UNAUTHORIZED"})
                return
            try:
                project = app.prj_mgr.get_project(project_name)
                enrollment = app.team_enroll_mgr.authenticate_token(
                    project, authorization.removeprefix("Bearer ").strip()
                )
                if not enrollment:
                    self._json(HTTPStatus.UNAUTHORIZED, {"code": "UNAUTHORIZED"})
                    return
                self._audit_context["enrollment"] = enrollment
                values = payload.get("flags")
                if values is None:
                    values = [payload.get("flag", "")]
                if action == "submissions/batch" and "flags" not in payload:
                    self._json(HTTPStatus.BAD_REQUEST, {"code": "INVALID_BATCH"})
                    return
                if not isinstance(values, list) or not 1 <= len(values) <= 100:
                    self._json(HTTPStatus.BAD_REQUEST, {"code": "INVALID_BATCH"})
                    return
                base_request_id = str(payload.get("request_id", ""))
                self._audit_recorded = True
                results = [
                    app.ad.submit(
                        project,
                        enrollment,
                        str(value),
                        user=self._audit_context.get("user"),
                        source_ip=self.client_address[0],
                        request_id=(
                            f"{base_request_id}:{index}"
                            if base_request_id and len(values) > 1
                            else base_request_id or None
                        ),
                    )
                    for index, value in enumerate(values)
                ]
            except Exception:
                self._json(HTTPStatus.BAD_REQUEST, {"code": "REQUEST_FAILED"})
                return
            status = (
                HTTPStatus.OK
                if action == "submissions/batch"
                or "flags" in payload
                or any(result.accepted for result in results)
                else HTTPStatus.UNPROCESSABLE_ENTITY
            )
            if all(result.code == "RATE_LIMITED" for result in results):
                status = HTTPStatus.TOO_MANY_REQUESTS
            response = [
                {
                    "accepted": result.accepted,
                    "code": result.code,
                    "tick": result.tick,
                    "service": result.service,
                }
                for result in results
            ]
            self._json(status, response[0] if "flags" not in payload else {"results": response})

        def log_message(self, format, *args):
            app.logger.info(format % args, logger_name="ad_http_api")

    return ThreadingHTTPServer((host, port), Handler)
