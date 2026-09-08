"""CLI for first-class teams."""

import click

from fit_ctf.components.data_view import get_view
from fit_ctf_cli.cli.utils import requires_database, team_option, user_option


@click.group(name="team")
@click.pass_context
@requires_database
def team(ctx: click.Context):
    """Manage competition teams and their members."""
    ctx.obj = ctx.parent.obj  # pyright: ignore


@team.command("create")
@team_option
@click.option("--display-name", default="", help="Human-readable team name.")
@click.pass_context
def create_team(ctx: click.Context, team_name: str, display_name: str):
    app = ctx.parent.obj["ctf_app"]
    try:
        created = app.team_mgr.create_team(team_name, display_name)
    except Exception as exc:
        raise click.ClickException(str(exc) or type(exc).__name__) from exc
    click.echo(f"Team `{created.name}` created.")


@team.command("add-member")
@team_option
@user_option
@click.option("--captain", is_flag=True, help="Make this user the team captain.")
@click.pass_context
def add_member(ctx: click.Context, team_name: str, username: str, captain: bool):
    app = ctx.parent.obj["ctf_app"]
    try:
        user = app.user_mgr.get_user(username)
        app.team_mgr.add_user(team_name, user, captain=captain)
    except Exception as exc:
        raise click.ClickException(str(exc) or type(exc).__name__) from exc
    click.echo(f"User `{username}` added to team `{team_name}`.")


@team.command("remove-member")
@team_option
@user_option
@click.pass_context
def remove_member(ctx: click.Context, team_name: str, username: str):
    app = ctx.parent.obj["ctf_app"]
    try:
        app.team_mgr.remove_user(team_name, app.user_mgr.get_user(username))
    except Exception as exc:
        raise click.ClickException(str(exc) or type(exc).__name__) from exc
    click.echo(f"User `{username}` removed from team `{team_name}`.")


@team.command("ls")
@click.pass_context
def list_teams(ctx: click.Context):
    app = ctx.parent.obj["ctf_app"]
    rows = [
        [item.name, item.display_name, len(item.user_ids), str(item.captain_id or "")]
        for item in app.team_mgr.get_docs(active=True)
    ]
    get_view("tabulate").print_data(["Name", "Display name", "Members", "Captain ID"], rows)
