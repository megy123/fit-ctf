"""Replaceable Attack & Defense policy and adapter boundaries."""

from pathlib import Path
from typing import Protocol


class CompetitionModePolicy(Protocol):
    mode: str
    attack_enabled: bool
    defense_enabled: bool


class FullAttackDefensePolicy:
    mode = "attack_defense"
    attack_enabled = True
    defense_enabled = True


class FlagPlantingAdapter(Protocol):
    def plant(self, path: Path, value: str) -> None: ...


class ServiceChecker(Protocol):
    def check_service(self) -> bool: ...
    def put_flag(self, flag: str) -> None: ...
    def get_flag(self) -> str: ...


class ScoringPolicy(Protocol):
    name: str
    version: int

    def compute_team(self, **kwargs): ...
