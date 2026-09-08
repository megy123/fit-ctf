import re
from datetime import datetime, timezone

from bson import ObjectId
from pydantic import Field

from fit_ctf.models.base import Base, BaseManager
from fit_ctf.models.core.user import User


class UserAlreadyInTeam(Exception):
    pass


class TeamAlreadyExistError(Exception):
    pass


class TeamNotFoundError(Exception):
    pass


class InvalidTeamNameError(Exception):
    pass


class Team(Base):
    name: str
    display_name: str = ""
    user_ids: list[ObjectId] = Field(default_factory=list)
    captain_id: ObjectId | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TeamManager(BaseManager[Team]):
    def get_team(self, team_or_name: str | Team) -> Team:
        if isinstance(team_or_name, Team):
            return team_or_name
        team = self.get_doc_by_filter(name=team_or_name, active=True)
        if not team:
            raise TeamNotFoundError()
        return team

    def create_team(self, name: str, display_name: str = "") -> Team:
        if re.fullmatch(r"[a-z0-9_]+", name) is None:
            raise InvalidTeamNameError(
                "Team name may contain only lowercase letters, digits, and underscores."
            )
        team = self.get_doc_by_filter(name=name, active=True)
        if team:
            raise TeamAlreadyExistError()
        team = self.create_and_insert_doc(name=name, display_name=display_name or name)
        return team

    def add_user(self, team_or_name: str | Team, user: User, *, captain: bool = False):
        team = self.get_team(team_or_name)
        if user.id in team.user_ids:
            raise UserAlreadyInTeam()
        team.user_ids.append(user.id)
        if captain or team.captain_id is None:
            team.captain_id = user.id
        self.update_doc(team)

    def remove_user(self, team_or_name: str | Team, user: User):
        team = self.get_team(team_or_name)
        if user.id not in set(team.user_ids):
            return
        team.user_ids.remove(user.id)
        if team.captain_id == user.id:
            team.captain_id = team.user_ids[0] if team.user_ids else None
        self.update_doc(team)

    def rename_team(self, team: Team, name: str):
        team.name = name
        self.update_doc(team)

    def delete_team(self, team_or_name: str | Team):
        team = self.get_team(team_or_name)
        team.active = False
        self.update_doc(team)
