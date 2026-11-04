"""Rich terminal view shared by the CLI and demo exporter."""

from math import ceil

from rich.console import Group
from rich.table import Table
from rich.text import Text


def duration(seconds):
    if seconds is None:
        return "—"
    seconds = max(0, ceil(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def scoreboard_renderable(state):
    c, tick = state["coefficients"], state["tick"]
    table = Table(title=f"{state['project']} · {state['state']}")
    for name in ("Rank", "Team", "Attack", "Defense", "SLA", "Total"):
        table.add_column(name, justify="left" if name == "Team" else "right")
    for row in state["teams"]:
        table.add_row(
            str(row["rank"]),
            Text(row["team"]),
            *(f"{row['weighted'][k]:.3f}" for k in ("attack", "defense", "sla")),
            f"{row['total']:.3f}",
        )
    service_keys = sorted({(s["cluster"], s["node"], s["service"]) for s in state["services"]})
    service_lookup = {
        (s["team_id"], s["cluster"], s["node"], s["service"]): s for s in state["services"]
    }
    services = Table()
    services.add_column("Team")
    for key in service_keys:
        services.add_column(Text(" / ".join(filter(None, key))), justify="center")
    for team in state["teams"]:
        services.add_row(
            Text(team["team"]),
            *(
                Text(
                    "UP"
                    if service_lookup.get((team["team_id"], *key), {}).get("status") == "up"
                    else "DOWN",
                    style="green"
                    if service_lookup.get((team["team_id"], *key), {}).get("status") == "up"
                    else "red",
                )
                for key in service_keys
            ),
        )
    last_check = max((s["checked_at"] for s in state["services"] if s["checked_at"]), default=None)
    current = "—" if tick["number"] is None else str(tick["number"] + 1)
    count = tick["count"] if tick["count"] is not None else "∞"
    scored = "none" if tick["scored_through"] is None else str(tick["scored_through"] + 1)
    return Group(
        table,
        Text(f"Total = {c['ma']} × Attack + {c['mb']} × Defense + {c['mc']} × SLA."),
        Text(f"Current tick {current}/{count} · time left {duration(tick['seconds_remaining'])}"),
        Text(f"Match time left ≈ {duration(tick['match_seconds_remaining'])}"),
        Text(f"Final scores through tick: {scored}."),
        Text("Team services", style="bold"),
        Text(f"Last check: {last_check or 'Never'}"),
        services,
        Text("Data: " + state["generated_at"]),
    )
