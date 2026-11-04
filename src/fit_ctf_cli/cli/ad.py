"""Operator CLI for full Attack & Defense competitions."""

import asyncio
import json
import time
from pathlib import Path

import click

from fit_ctf.components.data_view import get_view
from fit_ctf_cli.cli.utils import project_option, requires_database, team_option


def _project(app, name):
    return app.prj_mgr.get_project(name)


def _team_enrollment(app, project, team_name):
    return app.team_enroll_mgr.get_enrollment(app.team_mgr.get_team(team_name), project)


@click.group(name="ad")
@click.pass_context
@requires_database
def ad(ctx: click.Context):
    """Manage full Attack & Defense competitions."""
    ctx.obj = ctx.parent.obj  # pyright: ignore


@ad.group("topology")
def topology():
    """Define and deploy general networks and replicated team clusters."""


@topology.command("load")
@project_option
@click.option("-f", "--file", required=True, type=click.Path(exists=True, path_type=Path))
@click.pass_context
def topology_load(ctx, project_name, file):
    import yaml

    app = ctx.find_object(dict)["ctf_app"]
    try:
        app.topology.load(
            _project(app, project_name), yaml.safe_load(file.read_text(encoding="utf-8"))
        )
    except (ValueError, OSError, yaml.YAMLError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("Topology definition saved. Run topology compile to instantiate enrolled teams.")


@topology.command("compile")
@project_option
@click.pass_context
def topology_compile(ctx, project_name):
    app = ctx.find_object(dict)["ctf_app"]
    try:
        plan = app.topology.compile(_project(app, project_name))
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"Compiled {len(plan['nodes'])} nodes, {len(plan['networks'])} networks, "
        f"{len(plan['links'])} links for {len(plan['teams'])} teams."
    )


@topology.command("plan")
@project_option
@click.pass_context
def topology_plan(ctx, project_name):
    app = ctx.find_object(dict)["ctf_app"]
    try:
        click.echo(json.dumps(app.topology.plan(_project(app, project_name)), indent=2))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


def _topology_action(action):
    @topology.command(action)
    @project_option
    @click.option("-v", "--verbose", is_flag=True)
    @click.pass_context
    def run(ctx, project_name, verbose):
        app = ctx.find_object(dict)["ctf_app"]
        try:
            result = asyncio.run(app.topology.run(_project(app, project_name), action, verbose))
        except (ValueError, RuntimeError, OSError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(
            json.dumps(result, default=str) if action == "status" else f"Topology {action}: OK"
        )

    return run


for _action in ("build", "start", "stop", "status"):
    _topology_action(_action)


@ad.group("logging")
def audit_logging():
    """Independently enable or disable project JSON audit categories."""


def _logging_switch(category):
    @audit_logging.command(category)
    @project_option
    @click.option(
        "--enabled/--disabled",
        default=None,
        help="Enable or disable this category; omit to show its state.",
    )
    @click.pass_context
    def toggle(ctx, project_name, enabled):
        app = ctx.find_object(dict)["ctf_app"]
        project = _project(app, project_name)
        log = app.audit.log(project)
        if enabled is not None:
            try:
                log = app.audit.configure(project, category, enabled)
            except (OSError, RuntimeError) as exc:
                raise click.ClickException(str(exc)) from exc
        state = log.settings()["enabled"][category]
        click.echo(f"{category}: {'enabled' if state else 'disabled'}; {log.path}")

    return toggle


for _category in ("terminal", "infrastructure", "flags"):
    _logging_switch(_category)


@ad.command("init")
@project_option
@click.option("--tick-seconds", default=120, type=int, show_default=True)
@click.option("--tick-count", type=click.IntRange(min=1), default=None)
@click.pass_context
def init_ad(ctx, project_name, tick_seconds, tick_count):
    app = ctx.parent.obj["ctf_app"]
    try:
        config = app.ad.initialize_project(
            _project(app, project_name), tick_seconds=tick_seconds, tick_count=tick_count
        )
    except Exception as exc:
        raise click.ClickException(str(exc) or type(exc).__name__) from exc
    click.echo(f"Attack & Defense initialized for `{project_name}` ({config.tick_seconds}s tick).")


@ad.command("flags-load")
@project_option
@click.option("-f", "--file", required=True, type=click.Path(exists=True, path_type=Path))
@click.pass_context
def flags_load(ctx, project_name, file):
    """Load tick schedule and author-provided checkers for every named flag."""

    app = ctx.find_object(dict)["ctf_app"]
    try:
        catalogue = app.ad.configure_flags(_project(app, project_name), file)
    except Exception as exc:
        raise click.ClickException(str(exc) or type(exc).__name__) from exc
    click.echo(
        f"Loaded {len(catalogue.flags)} flag definitions; "
        f"{catalogue.ticks.count} ticks of {catalogue.ticks.seconds}s."
    )


@ad.command("enroll-team")
@project_option
@team_option
@click.pass_context
def enroll_team(ctx, project_name, team_name):
    app = ctx.parent.obj["ctf_app"]
    try:
        team = app.team_mgr.get_team(team_name)
        enrollment, token = app.team_enroll_mgr.enroll(team, _project(app, project_name))
    except Exception as exc:
        raise click.ClickException(str(exc) or type(exc).__name__) from exc
    click.echo(f"Team `{team_name}` enrolled (id={enrollment.id}).")
    click.echo(f"Team token (shown once): {token}")


@ad.command("rotate-token")
@project_option
@team_option
@click.pass_context
def rotate_token(ctx, project_name, team_name):
    app = ctx.parent.obj["ctf_app"]
    project = _project(app, project_name)
    token = app.team_enroll_mgr.rotate_token(_team_enrollment(app, project, team_name))
    click.echo(f"New team token (shown once): {token}")


@ad.command("service-add")
@project_option
@click.option("--key", required=True)
@click.option("--scenario", required=True)
@click.option("--service", required=True)
@click.option("--flag-path", default="runtime/flags/current.flag", show_default=True)
@click.pass_context
def service_add(ctx, project_name, key, scenario, service, flag_path):
    app = ctx.parent.obj["ctf_app"]
    try:
        definition = app.ad.add_service_definition(
            _project(app, project_name), key, scenario, service, flag_path
        )
    except Exception as exc:
        raise click.ClickException(str(exc) or type(exc).__name__) from exc
    click.echo(f"A&D service `{definition.key}` added.")


@ad.command("service-import")
@project_option
@click.option("-s", "--scenario", required=True)
@click.pass_context
def service_import(ctx, project_name, scenario):
    """Import A&D definitions from SCENARIO/ad.yaml."""
    app = ctx.parent.obj["ctf_app"]
    try:
        definitions = app.ad.import_service_catalog(_project(app, project_name), scenario)
    except Exception as exc:
        raise click.ClickException(str(exc) or type(exc).__name__) from exc
    click.echo(f"Imported {len(definitions)} A&D service definition(s).")


@ad.command("service-ls")
@project_option
@click.pass_context
def service_ls(ctx, project_name):
    app = ctx.parent.obj["ctf_app"]
    project = _project(app, project_name)
    rows = app.ad.managers.definitions.get_docs(project_id=project.id, active=True)
    get_view("tabulate").print_data(
        ["Key", "Scenario", "Compose service", "Adapter"],
        [[item.key, item.scenario_name, item.service_name, item.flag_adapter] for item in rows],
    )


@ad.command("start")
@project_option
@click.pass_context
def start_ad(ctx, project_name):
    app = ctx.parent.obj["ctf_app"]
    try:
        app.ad.start_project(_project(app, project_name))
    except Exception as exc:
        raise click.ClickException(str(exc) or type(exc).__name__) from exc
    click.echo(f"Attack & Defense project `{project_name}` is running.")


@ad.command("finish")
@project_option
@click.pass_context
def finish_ad(ctx, project_name):
    app = ctx.parent.obj["ctf_app"]
    app.ad.finish_project(_project(app, project_name))
    click.echo(f"Attack & Defense project `{project_name}` finished.")


@ad.command("preflight")
@project_option
@click.pass_context
def preflight(ctx, project_name):
    app = ctx.parent.obj["ctf_app"]
    app.ad.preflight(_project(app, project_name))
    click.echo(f"Attack & Defense project `{project_name}` is ready.")


@ad.command("pause")
@project_option
@click.pass_context
def pause(ctx, project_name):
    app = ctx.parent.obj["ctf_app"]
    app.ad.pause_project(_project(app, project_name))
    click.echo(f"Attack & Defense project `{project_name}` paused.")


@ad.command("resume")
@project_option
@click.pass_context
def resume(ctx, project_name):
    app = ctx.parent.obj["ctf_app"]
    app.ad.resume_project(_project(app, project_name))
    click.echo(f"Attack & Defense project `{project_name}` resumed.")


@ad.command("tick-open")
@project_option
@click.pass_context
def tick_open(ctx, project_name):
    app = ctx.parent.obj["ctf_app"]
    tick = app.ad.open_tick(_project(app, project_name))
    click.echo(f"Tick {tick.number} active; planted {tick.planted_instance_count} flags.")


@ad.command("tick-close")
@project_option
@click.pass_context
def tick_close(ctx, project_name):
    app = ctx.parent.obj["ctf_app"]
    tick = app.ad.close_tick(_project(app, project_name))
    click.echo(f"Tick {tick.number} closed; checked {tick.checked_instance_count} flag locations.")


@ad.command("tick-advance")
@project_option
@click.pass_context
def tick_advance(ctx, project_name):
    app = ctx.parent.obj["ctf_app"]
    tick = app.ad.advance(_project(app, project_name))
    click.echo(f"Tick {tick.number} active.")


@ad.command("submit")
@project_option
@team_option
@click.option("--flag", required=True)
@click.option("--username", default=None, help="Player name for audit (operator supplied).")
@click.pass_context
def submit(ctx, project_name, team_name, flag, username):
    app = ctx.parent.obj["ctf_app"]
    project = _project(app, project_name)
    try:
        enrollment = _team_enrollment(app, project, team_name)
    except Exception as exc:
        app.audit.submission(project, None, flag, "UNAUTHORIZED_TEAM", user=username, source="cli")
        raise click.ClickException(str(exc) or type(exc).__name__) from exc
    result = app.ad.submit(project, enrollment, flag, source="cli", user=username)
    click.echo(result.code)
    if not result.accepted:
        ctx.exit(1)


@ad.command("scoreboard")
@project_option
@click.pass_context
def scoreboard(ctx, project_name):
    app = ctx.parent.obj["ctf_app"]
    rows = app.ad.scoreboard(_project(app, project_name))
    values = [
        [row[key] for key in ("rank", "team", "tick", "attack", "defense", "sla", "total")]
        for row in rows
    ]
    get_view("tabulate").print_data(
        ["Rank", "Team", "Tick", "Attack", "Defense", "SLA", "Total"], values
    )


@ad.command("status")
@project_option
@click.pass_context
def status(ctx, project_name):
    app = ctx.parent.obj["ctf_app"]
    project = _project(app, project_name)
    config = app.ad.managers.config.get_for_project(project.id)
    tick = app.ad.managers.ticks.latest(project.id)
    latest = tick.number if tick else "-"
    click.echo(f"state={config.state} mode={config.mode} latest_tick={latest}")


@ad.command("serve")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8088, type=int, show_default=True)
@click.pass_context
def serve(ctx, host, port):
    """Run the team submission and scoreboard HTTP API."""
    from fit_ctf.ad.http_api import create_http_server

    app = ctx.parent.obj["ctf_app"]
    server = create_http_server(app, host, port)
    click.echo(f"A&D API listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


@ad.command("worker")
@click.option("--poll-seconds", default=1.0, type=float, show_default=True)
@click.option("--once", is_flag=True, help="Process due projects once and exit.")
@click.pass_context
def worker(ctx, poll_seconds, once):
    """Run the project-aware tick worker."""
    app = ctx.parent.obj["ctf_app"]
    while True:
        try:
            ticks = app.ad.run_due_projects()
            for tick in ticks:
                click.echo(f"project={tick.project_id} tick={tick.number} state={tick.state}")
        except Exception as exc:
            app.logger.warning(str(exc), logger_name="ad_worker")
        if once:
            return
        time.sleep(max(poll_seconds, 0.1))
