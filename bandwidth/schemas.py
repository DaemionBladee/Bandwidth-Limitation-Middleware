from datetime import UTC, datetime, time
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_COUNTER = 9_000_000_000_000_000


class PolicySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120, pattern=r".*\S.*")
    enabled: bool = True
    quota_bytes: int = Field(gt=0, le=MAX_COUNTER)
    normal_download_kbps: int = Field(gt=0, le=10**9)
    normal_upload_kbps: int = Field(gt=0, le=10**9)
    limited_download_kbps: int = Field(gt=0, le=10**9)
    limited_upload_kbps: int = Field(gt=0, le=10**9)
    period_type: Literal["daily", "weekly", "monthly", "custom"] = "monthly"
    timezone: str = "UTC"
    reset_time: time = time(0, 0)
    weekly_day: int = Field(default=0, ge=0, le=6)
    monthly_day: int = Field(default=1, ge=1, le=31)
    custom_start: datetime | None = None
    custom_end: datetime | None = None
    repeat_interval_seconds: int | None = Field(default=None, gt=0, le=315_576_000)
    expiry_action: Literal["NORMAL", "LIMITED"] | None = None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use an IANA timezone, for example Asia/Yangon") from exc
        return value

    @model_validator(mode="after")
    def validate_policy(self):
        if (
            self.limited_download_kbps > self.normal_download_kbps
            or self.limited_upload_kbps > self.normal_upload_kbps
        ):
            raise ValueError("Limited speeds must not exceed normal speeds")
        if self.reset_time.tzinfo is not None or self.reset_time.second or self.reset_time.microsecond:
            raise ValueError("Reset time must be a local HH:MM time")
        if self.period_type == "custom":
            if not self.custom_start or not self.custom_end:
                raise ValueError("Custom policies require start and end timestamps")
            if self.custom_start.utcoffset() is None or self.custom_end.utcoffset() is None:
                raise ValueError("Custom timestamps must include a UTC offset")
            duration = (self.custom_end.astimezone(UTC) - self.custom_start.astimezone(UTC)).total_seconds()
            if duration <= 0:
                raise ValueError("Custom end must be after start")
            if self.repeat_interval_seconds and self.repeat_interval_seconds < duration:
                raise ValueError("Repeat interval cannot overlap the custom interval")
            if (
                not self.repeat_interval_seconds or self.repeat_interval_seconds > duration
            ) and not self.expiry_action:
                raise ValueError("One-time intervals and repeat gaps require an explicit expiry action")
        return self


class PolicyEdit(BaseModel):
    spec: PolicySpec
    application: Literal["immediate", "next_period"] = "immediate"
    expected_revision: int | None = Field(default=None, gt=0)


class DefaultPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: PolicySpec | None = None
    policy_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def one_plan(self):
        if (self.spec is None) == (self.policy_id is None):
            raise ValueError("Provide either a policy specification or an existing policy ID")
        return self


class DefaultPlanEdit(DefaultPlanInput):
    expected_revision: int = Field(ge=0)


class AdminCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.@-]*$")
    password: str = Field(min_length=12, max_length=512, repr=False)
    confirm_password: str = Field(min_length=1, max_length=512, repr=False)
    current_password: str = Field(min_length=1, max_length=512, repr=False)

    @model_validator(mode="after")
    def valid_password(self):
        if self.password != self.confirm_password:
            raise ValueError("New passwords must match")
        if not self.password.strip() or self.password.casefold() == self.username.casefold():
            raise ValueError("Choose a password distinct from the administrator username")
        return self


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=200, pattern=r"^\S+$")


class ResetUsageInput(BaseModel):
    expected_period_id: int = Field(gt=0)
    request_id: UUID
    reason: str = Field(min_length=3, max_length=300)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value):
        value = value.strip()
        if len(value) < 3:
            raise ValueError("Enter a reason of at least 3 characters")
        return value


class AssignmentInput(BaseModel):
    policy_id: int = Field(gt=0)
    enabled: bool = True
    application: Literal["immediate", "next_period"] = "immediate"


class Measurement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_id: str = Field(min_length=1, max_length=250)
    username: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=250)
    observed_at: datetime
    kind: Literal["delta", "cumulative"] = "cumulative"
    upload: int = Field(ge=0, le=MAX_COUNTER)
    download: int = Field(ge=0, le=MAX_COUNTER)
    unit: Literal["bytes", "KB", "MB", "GB", "KiB", "MiB", "GiB"] = "bytes"

    @field_validator("observed_at")
    @classmethod
    def aware(cls, value):
        if value.utcoffset() is None:
            raise ValueError("Accounting timestamps must include a UTC offset")
        return value

    def counters(self):
        factor = {
            "bytes": 1,
            "KB": 1000,
            "MB": 10**6,
            "GB": 10**9,
            "KiB": 1024,
            "MiB": 1024**2,
            "GiB": 1024**3,
        }[self.unit]
        values = (self.upload * factor, self.download * factor)
        if max(values) > MAX_COUNTER:
            raise ValueError("Accounting measurement is too large")
        return values
