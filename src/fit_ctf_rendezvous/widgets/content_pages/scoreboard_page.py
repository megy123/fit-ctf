"""Player-facing A&D scoreboard, updated without running checkers in the TUI."""

import asyncio
import time
from copy import deepcopy

from rich.console import Group
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from fit_ctf.ad.scoreboard_terminal import scoreboard_renderable
from fit_ctf_rendezvous.widgets.core_widget import CoreWidget


class ScoreboardPage(VerticalScroll, CoreWidget):
    DEFAULT_CSS = """
    ScoreboardPage { height: 1fr; padding: 1 2; }
    ScoreboardPage Static { height: auto; }
    """

    def __init__(self, owner_screen, **kwargs):
        VerticalScroll.__init__(self, **kwargs)
        CoreWidget.__init__(self, owner_screen)
        self._state = None
        self._received = 0
        self._busy = False
        self._project_id = None

    def compose(self) -> ComposeResult:
        yield Static("Loading A&D scoreboard…", classes="ad-scoreboard-content", markup=False)

    def on_mount(self):
        self.set_interval(1, self.refresh_scoreboard)
        self.call_after_refresh(self.refresh_scoreboard)

    async def refresh_scoreboard(self):
        if self._busy:
            return
        self._busy = True
        try:
            project = self.core_mgr.selected_project
            project_id = project.id if project else None
            if project_id != self._project_id:
                self._state = None
                self._project_id = project_id
            if (
                self._state is None
                or time.monotonic() - self._received >= self._state["refresh_seconds"]
            ):
                self._state = await asyncio.to_thread(self.core_mgr.get_ad_scoreboard)
                self._received = time.monotonic()
            state = deepcopy(self._state)
            elapsed = time.monotonic() - self._received
            if state["tick"]["deadline"]:
                elapsed = min(elapsed, state["tick"]["seconds_remaining"])
                for key in ("seconds_remaining", "match_seconds_remaining"):
                    if state["tick"][key] is not None:
                        state["tick"][key] = max(0, state["tick"][key] - elapsed)
            if self.is_mounted:
                self.query_one(Static).update(scoreboard_renderable(state))
        except Exception as exc:
            if self.is_mounted:
                previous = scoreboard_renderable(self._state) if self._state else Text("")
                self.query_one(Static).update(
                    Group(Text(f"Scoreboard unavailable: {exc}"), previous)
                )
        finally:
            self._busy = False
