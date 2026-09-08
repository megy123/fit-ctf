"""CLI for team-owned Attack & Defense clusters."""

import asyncio
import pathlib

import click

from fit_ctf.components.data_parser.yaml_parser import YamlParser
from fit_ctf.models.infra.utils import (
    scenario_config_from_dict,
    validate_canonical_scenario_yaml_dict,
)
from fit_ctf_cli.cli.utils import project_option, requires_database, team_option


def _objects(app, team_name: str, project_name: str):
    team = app.team_mgr.get_team(team_name)
    project = app.prj_mgr.get_project(project_name)
    enrollment = app.team_enroll_mgr.get_enrollment(team, project)
    return project, team, app.team_cluster_mgr.get_cluster(enrollment)


@click.group(name="team-cluster")
@click.pass_context
@requires_database
def team_cluster(ctx: click.Context):
    """Manage team-owned scenario clusters."""
    ctx.obj = ctx.parent.obj  # pyright: ignore


@team_cluster.command("add-scenario")
@project_option
@team_option
@click.option("-s", "--scenario", required=True)
@click.option("-f", "--file", required=True, type=click.Path(path_type=pathlib.Path))
@click.pass_context
def add_scenario(ctx, project_name, team_name, scenario, file):
    app = ctx.parent.obj["ctf_app"]
    try:
        _, _, cluster = _objects(app, team_name, project_name)
        raw = YamlParser.load_data_file(file)
        validate_canonical_scenario_yaml_dict(raw)
        config = scenario_config_from_dict(scenario, raw)
        app.team_cluster_mgr.create_or_update_scenario_config(cluster, config)
    except Exception as exc:
        raise click.ClickException(str(exc) or type(exc).__name__) from exc
    click.echo(f"Scenario `{scenario}` added to {team_name}@{project_name}.")


@team_cluster.command("compile")
@project_option
@team_option
@click.option("-s", "--scenario")
@click.pass_context
def compile_cluster(ctx, project_name, team_name, scenario):
    app = ctx.parent.obj["ctf_app"]
    _, _, cluster = _objects(app, team_name, project_name)
    names = [scenario] if scenario else cluster.scenario_names
    for name in names:
        app.team_cluster_mgr.compile_scenario(cluster, name)
    click.echo(f"Team cluster `{cluster.name}` compiled.")


@team_cluster.command("build")
@project_option
@team_option
@click.option("-v", "--verbose", is_flag=True)
@click.pass_context
def build_cluster(ctx, project_name, team_name, verbose):
    app = ctx.parent.obj["ctf_app"]
    _, _, cluster = _objects(app, team_name, project_name)
    code = asyncio.run(app.team_cluster_mgr.build_cluster_images(cluster, verbose=verbose))
    if code:
        raise click.ClickException(f"Build failed with exit code {code}.")
    click.echo("Team cluster images built.")


@team_cluster.command("start")
@project_option
@team_option
@click.option("-v", "--verbose", is_flag=True)
@click.pass_context
def start_cluster(ctx, project_name, team_name, verbose):
    app = ctx.parent.obj["ctf_app"]
    _, _, cluster = _objects(app, team_name, project_name)
    code = asyncio.run(app.team_cluster_mgr.start_cluster(cluster, verbose=verbose))
    if code:
        raise click.ClickException(f"Start failed with exit code {code}.")
    click.echo(f"Team cluster `{cluster.name}` started.")


@team_cluster.command("stop")
@project_option
@team_option
@click.option("-v", "--verbose", is_flag=True)
@click.pass_context
def stop_cluster(ctx, project_name, team_name, verbose):
    app = ctx.parent.obj["ctf_app"]
    _, _, cluster = _objects(app, team_name, project_name)
    code = asyncio.run(app.team_cluster_mgr.stop_cluster(cluster, verbose=verbose))
    if code:
        raise click.ClickException(f"Stop failed with exit code {code}.")
    click.echo(f"Team cluster `{cluster.name}` stopped.")


@team_cluster.command("status")
@project_option
@team_option
@click.pass_context
def status_cluster(ctx, project_name, team_name):
    app = ctx.parent.obj["ctf_app"]
    _, _, cluster = _objects(app, team_name, project_name)
    running = asyncio.run(app.team_cluster_mgr.cluster_is_running(cluster))
    click.echo(f"{team_name}@{project_name}: {'RUNNING' if running else 'STOPPED'}")
