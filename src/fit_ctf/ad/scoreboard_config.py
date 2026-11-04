"""Author-controlled scoreboard presentation and scoring coefficients."""

from pydantic import BaseModel, ConfigDict, Field


class Coefficients(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ma: float = Field(default=1, ge=0, allow_inf_nan=False)
    mb: float = Field(default=1, ge=0, allow_inf_nan=False)
    mc: float = Field(default=1, ge=0, allow_inf_nan=False)


class ScoreboardSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    coefficients: Coefficients = Field(default_factory=Coefficients)
    html: bool = False
    refresh_seconds: int = Field(default=2, ge=1, le=60)
    service_check_seconds: int = Field(default=5, ge=1, le=300)
