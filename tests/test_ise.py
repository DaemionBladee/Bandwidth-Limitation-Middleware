import json
from types import SimpleNamespace

import httpx
import pytest

from bandwidth.config import Settings
from bandwidth.ise import ISEError, LiveISE

TARGET = {"mode": "LIMITED", "download_kbps": 2000, "upload_kbps": 1000, "period_id": 1, "version_id": 1}


def contract():
    # Synthetic fixture routes, deliberately not presented as Cisco endpoints.
    return {
        "version": "test-3.3",
        "version_path": "/version",
        "version_field": "version",
        "capabilities_verified": True,
        "profile_mapping_verified": True,
        "session_confirmation_verified": True,
        "profiles": [
            {"mode": "NORMAL", "download_kbps": 20000, "upload_kbps": 10000, "profile": "normal"},
            {"mode": "LIMITED", "download_kbps": 2000, "upload_kbps": 1000, "profile": "limited"},
        ],
        "update": {
            "method": "PUT",
            "path": "/identity/{username}",
            "payload": {"profile": "{profile}", "username": "{username}"},
        },
        "reauthorize": {"method": "POST", "path": "/reauth", "payload": {"username": "{username}"}},
        "reauthorization_required": True,
        "confirmation_path": "/sessions/{username}",
        "confirmation_profile_field": "profile",
        "confirmation_username_field": "username",
        "confirmation_session_field": "session_id",
        "confirmation_active_field": "active",
    }


def adapter(tmp_path, handler, overrides=None):
    path = tmp_path / "contract.json"
    path.write_text(json.dumps({**contract(), **(overrides or {})}))
    return LiveISE(
        Settings(
            ise_base_url="https://ise.example.test",
            ise_username="test",
            ise_password="fixture-only",
            ise_contract_file=str(path),
        ),
        httpx.MockTransport(handler),
    )


def test_live_version_update_reauth_and_session_confirmation(tmp_path):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": "test-3.3"})
        if request.url.path.startswith("/sessions"):
            return httpx.Response(
                200,
                json={
                    "username": "alice",
                    "profile": "limited" if len(calls) > 3 else "normal",
                    "session_id": "s",
                    "active": True,
                },
            )
        return httpx.Response(202)

    assert adapter(tmp_path, handler).reconcile(None, SimpleNamespace(username="alice"), TARGET)
    assert calls == [
        ("GET", "/version"),
        ("GET", "/sessions/alice"),
        ("PUT", "/identity/alice"),
        ("POST", "/reauth"),
        ("GET", "/sessions/alice"),
    ]


def test_acknowledgement_is_not_confirmation(tmp_path):
    def handler(request):
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": "test-3.3"})
        if request.url.path.startswith("/sessions"):
            return httpx.Response(
                200, json={"username": "alice", "profile": "normal", "session_id": "s", "active": True}
            )
        return httpx.Response(202)

    assert not adapter(tmp_path, handler).reconcile(None, SimpleNamespace(username="alice"), TARGET)


def test_wrong_version_blocks_mutation(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={"version": "different"})

    with pytest.raises(ISEError, match="version differs"):
        adapter(tmp_path, handler).reconcile(None, SimpleNamespace(username="alice"), TARGET)
    assert calls == ["GET"]


@pytest.mark.parametrize(
    "override",
    [
        {"capabilities_verified": False},
        {"profile_mapping_verified": False},
        {"session_confirmation_verified": False},
        {"reauthorize": None},
        {"version_path": "//untrusted.example/version"},
    ],
)
def test_unverified_contract_rejected(tmp_path, override):
    with pytest.raises(ISEError):
        adapter(tmp_path, lambda r: httpx.Response(500), override)


def test_timeout_is_sanitized(tmp_path):
    def handler(request):
        raise httpx.ReadTimeout("fixture secret body", request=request)

    with pytest.raises(ISEError, match="ReadTimeout") as exc:
        adapter(tmp_path, handler).reconcile(None, SimpleNamespace(username="alice"), TARGET)
    assert "fixture secret body" not in str(exc.value)


def test_unmapped_speed_never_calls_network(tmp_path):
    def handler(request):
        pytest.fail("Unmapped policy must not reach the network")

    with pytest.raises(ISEError, match="No verified ISE profile"):
        adapter(tmp_path, handler).reconcile(
            None, SimpleNamespace(username="alice"), {**TARGET, "download_kbps": 2222}
        )
