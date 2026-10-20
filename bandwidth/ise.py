"""Deployment-specific ISE integration. An HTTP acknowledgement is never confirmation."""

import ssl
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from bandwidth.config import settings
from bandwidth.models import MockISEState


class ISEError(Exception):
    pass


class ISEAdapter(Protocol):
    name: str

    def reconcile(self, db, user, desired: dict) -> bool: ...


class MockISE:
    name = "mock"

    def reconcile(self, db, user, desired):
        row = db.get(MockISEState, user.id)
        if not row:
            row = MockISEState(user_id=user.id, applied=desired.copy())
            db.add(row)
        else:
            row.applied = desired.copy()
        db.flush()
        return row.applied == desired


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["PUT", "POST", "PATCH"]
    path: str
    payload: dict
    success_codes: list[int] = Field(default=[200, 201, 202, 204], min_length=1)


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["NORMAL", "LIMITED"]
    download_kbps: int = Field(gt=0)
    upload_kbps: int = Field(gt=0)
    profile: str = Field(min_length=1)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1)
    version_path: str
    version_field: str
    capabilities_verified: bool
    profile_mapping_verified: bool
    session_confirmation_verified: bool
    profiles: list[Profile] = Field(min_length=2)
    update: Operation
    reauthorize: Operation | None
    reauthorization_required: bool
    confirmation_path: str
    confirmation_profile_field: str
    confirmation_username_field: str
    confirmation_session_field: str
    confirmation_active_field: str

    @model_validator(mode="after")
    def verified(self):
        if not all(
            (self.capabilities_verified, self.profile_mapping_verified, self.session_confirmation_verified)
        ):
            raise ValueError(
                "ISE deployment capabilities, speed mapping, and session confirmation must be verified"
            )
        if self.reauthorization_required and not self.reauthorize:
            raise ValueError("This deployment requires a reauthorization operation")
        paths = [self.version_path, self.update.path, self.confirmation_path]
        if self.reauthorize:
            paths.append(self.reauthorize.path)
        for path in paths:
            if (
                not path.startswith("/")
                or path.startswith("//")
                or ".." in path
                or "\\" in path
                or "#" in path
            ):
                raise ValueError("ISE paths must be absolute appliance-relative paths")
            if set(part.split("}")[0] for part in path.split("{")[1:]) - {"username"}:
                raise ValueError("Only {username} is supported in paths")
        mappings = {(p.mode, p.download_kbps, p.upload_kbps) for p in self.profiles}
        if len(mappings) != len(self.profiles):
            raise ValueError("Duplicate speed/profile mapping")
        return self


def field(data, dotted):
    for key in dotted.split("."):
        data = data[key]
    return data


def render(value, bindings):
    if isinstance(value, str):
        if value in {"{" + key + "}" for key in bindings}:
            return bindings[value[1:-1]]
        return value.format_map(bindings)
    if isinstance(value, list):
        return [render(item, bindings) for item in value]
    if isinstance(value, dict):
        return {key: render(item, bindings) for key, item in value.items()}
    return value


class LiveISE:
    name = "live"

    def __init__(self, config=None, transport=None):
        self.config = config or settings()
        cfg = self.config
        parsed = urlsplit(cfg.ise_base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ISEError("ISE_BASE_URL must be an HTTPS appliance origin without credentials or path")
        if not cfg.ise_contract_file or not cfg.ise_username or not cfg.ise_password:
            raise ISEError("Live ISE requires credentials and an operator-reviewed deployment contract")
        try:
            self.contract = Contract.model_validate_json(Path(cfg.ise_contract_file).read_text())
            self.tls = ssl.create_default_context(cafile=cfg.ise_ca_bundle or None)
        except (OSError, ValueError) as exc:
            raise ISEError("Invalid ISE contract or trusted CA configuration") from exc
        self.transport = transport

    def reconcile(self, db, user, desired):
        contract = self.contract
        profile = next(
            (
                p.profile
                for p in contract.profiles
                if (p.mode, p.download_kbps, p.upload_kbps)
                == (desired["mode"], desired["download_kbps"], desired["upload_kbps"])
            ),
            None,
        )
        if not profile:
            raise ISEError("No verified ISE profile maps to the requested speeds and mode")
        bindings = {"username": user.username, "profile": profile, **desired}
        path_bindings = {"username": quote(user.username, safe="")}
        try:
            with httpx.Client(
                base_url=self.config.ise_base_url,
                auth=(self.config.ise_username, self.config.ise_password),
                verify=self.tls,
                timeout=self.config.ise_timeout_seconds,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                version = client.get(contract.version_path)
                version.raise_for_status()
                if str(field(version.json(), contract.version_field)) != contract.version:
                    raise ISEError("ISE version differs from the reviewed deployment contract")

                def confirmed():
                    response = client.get(contract.confirmation_path.format_map(path_bindings))
                    response.raise_for_status()
                    data = response.json()
                    return (
                        field(data, contract.confirmation_username_field) == user.username
                        and field(data, contract.confirmation_profile_field) == profile
                        and bool(field(data, contract.confirmation_session_field))
                        and field(data, contract.confirmation_active_field) is True
                    )

                if confirmed():
                    return True
                for operation in [contract.update, contract.reauthorize]:
                    if operation is None:
                        continue
                    response = client.request(
                        operation.method,
                        operation.path.format_map(path_bindings),
                        json=render(operation.payload, bindings),
                    )
                    if response.status_code not in operation.success_codes:
                        raise ISEError(f"ISE {operation.method} rejected with HTTP {response.status_code}")
                # Success means an active session reports the expected profile. A 202
                # response alone leaves enforcement pending and triggers a bounded retry.
                return confirmed()
        except ISEError:
            raise
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            # Do not persist response bodies, headers, URLs or credentials in errors.
            raise ISEError(f"ISE request or readback failed ({type(exc).__name__})") from exc


def ise_adapter():
    mode = settings().ise_mode
    if mode == "mock":
        return MockISE()
    if mode == "live":
        return LiveISE()
    raise ISEError("ISE_MODE must be mock or live")
