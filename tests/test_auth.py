from dataclasses import replace
import json
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import pytest

from youtube_download.account import Account
from youtube_download.auth import GoogleLogin, LoginError, LOGIN_SECONDS
from youtube_backup.auth import SCOPES


def client_config():
    return {
        "installed": {
            "client_id": "test.apps.googleusercontent.com",
            "client_secret": "test-client-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


@pytest.fixture
def login(settings, tmp_path):
    settings = replace(settings, client_secrets_file=str(tmp_path / "missing.json"))
    manager = GoogleLogin(settings, Mock(), browser_open=Mock())
    manager.configure(json.dumps(client_config()).encode())
    return manager


@pytest.fixture
def flow(monkeypatch):
    flow = Mock()
    flow.authorization_url.return_value = (
        "https://accounts.google.com/o/oauth2/auth?state=expected-state",
        "expected-state",
    )
    flow.credentials.valid = True
    flow.credentials.refresh_token = "new-refresh-token"
    flow.credentials.has_scopes.return_value = True
    monkeypatch.setattr(
        "youtube_download.auth.InstalledAppFlow.from_client_config",
        Mock(return_value=flow),
    )
    return flow


URI = "http://127.0.0.1:8765/auth/callback"


def test_real_flow_uses_pkce_readonly_consent_and_offline_access(login):
    result = login.start(URI)
    query = parse_qs(urlparse(result["authorization_url"]).query)
    assert query["scope"] == [SCOPES[0]]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) >= 43
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["redirect_uri"] == [URI]
    assert "test-client-secret" not in json.dumps(result)
    assert login.status()["phase"] == "waiting"
    login.browser_open.assert_called_once()
    login.cancel()


def test_repeated_start_is_idempotent(login, flow):
    first = login.start(URI)
    assert login.start(URI)["authorization_url"] == first["authorization_url"]
    flow.authorization_url.assert_called_once()
    login.browser_open.assert_called_once()


def test_callback_saves_credentials_once_and_clears_state(login, flow):
    login.start(URI)
    assert login.complete("expected-state", "authorization-code", "") == "connected"
    login.save_credentials.assert_called_once_with(flow.credentials)
    flow.fetch_token.assert_called_once_with(code="authorization-code", timeout=30)
    assert login.status()["authorization_url"] == ""
    assert login.flow is None
    assert login.complete("expected-state", "authorization-code", "") == "invalid"


def test_invalid_state_cannot_cancel_or_complete_another_login(login, flow):
    login.start(URI)
    assert login.complete("wrong-state", "code", "access_denied") == "invalid"
    assert login.status()["phase"] == "waiting"
    flow.fetch_token.assert_not_called()
    login.save_credentials.assert_not_called()


@pytest.mark.parametrize("code,error", [("", "access_denied"), ("", "")])
def test_denial_or_missing_code_keeps_previous_credentials(login, flow, code, error):
    login.start(URI)
    assert login.complete("expected-state", code, error) == "error"
    login.save_credentials.assert_not_called()
    flow.fetch_token.assert_not_called()
    assert login.status()["authorization_url"] == ""


def test_waiting_attempt_expires_and_late_callback_cannot_save(login, flow):
    clock = [100.0]
    login.clock = lambda: clock[0]
    login.start(URI)
    clock[0] += LOGIN_SECONDS + 1
    assert login.status()["phase"] == "expired"
    assert login.complete("expected-state", "code", "") == "invalid"
    login.save_credentials.assert_not_called()


def test_cancel_allows_retry_and_rejects_old_callback(login, flow):
    login.start(URI)
    assert login.cancel()["phase"] == "cancelled"
    assert login.complete("expected-state", "code", "") == "invalid"
    flow.authorization_url.return_value = (
        "https://accounts.google.com/new",
        "new-state",
    )
    assert login.start(URI)["phase"] == "waiting"
    assert login.complete("expected-state", "code", "") == "invalid"


def test_cancel_during_token_exchange_does_not_overwrite_saved_login(login, flow):
    entered, proceed = Event(), Event()

    def exchange(**kwargs):
        entered.set()
        assert proceed.wait(2)

    flow.fetch_token.side_effect = exchange
    login.start(URI)
    result = []
    thread = Thread(
        target=lambda: result.append(login.complete("expected-state", "code", ""))
    )
    thread.start()
    try:
        assert entered.wait(2)
        assert login.cancel()["phase"] == "cancelled"
    finally:
        proceed.set()
        thread.join(2)
    assert result == ["cancelled"]
    login.save_credentials.assert_not_called()


def test_network_errors_are_sanitized_and_retryable(login, flow):
    flow.fetch_token.side_effect = RuntimeError("secret token response")
    login.start(URI)
    assert login.complete("expected-state", "code", "") == "error"
    assert "secret" not in json.dumps(login.status())
    login.save_credentials.assert_not_called()
    assert login.start(URI)["phase"] == "waiting"


@pytest.mark.parametrize("field,value", [("valid", False), ("refresh_token", None)])
def test_incomplete_credentials_are_not_saved(login, flow, field, value):
    setattr(flow.credentials, field, value)
    login.start(URI)
    assert login.complete("expected-state", "code", "") == "error"
    login.save_credentials.assert_not_called()


def test_missing_scope_is_not_saved(login, flow):
    flow.credentials.has_scopes.return_value = False
    login.start(URI)
    assert login.complete("expected-state", "code", "") == "error"
    login.save_credentials.assert_not_called()


def test_browser_launch_failure_keeps_manual_link(login, flow):
    login.browser_open.side_effect = OSError("Browser unavailable")
    result = login.start(URI)
    assert result["phase"] == "waiting" and result["authorization_url"].startswith(
        "https://accounts.google.com/"
    )


def test_client_file_is_private_and_upload_never_echoes_secret(login):
    path = login.settings.data_dir / "oauth-client.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert "client_secret" not in json.dumps(login.status())


@pytest.mark.parametrize(
    "config", [b"not JSON", b"{}", b'{"web": {}}', b"x" * (64 * 1024 + 1)]
)
def test_invalid_upload_preserves_existing_config(login, config):
    path = login.settings.data_dir / "oauth-client.json"
    old = path.read_bytes()
    with pytest.raises(LoginError):
        login.configure(config)
    assert path.read_bytes() == old


def test_untrusted_token_endpoint_is_rejected(login):
    config = client_config()
    config["installed"]["token_uri"] = "http://127.0.0.1/secrets"
    with pytest.raises(LoginError):
        login.configure(json.dumps(config).encode())


def test_missing_client_file_is_actionable(settings, tmp_path):
    manager = GoogleLogin(
        replace(settings, client_secrets_file=str(tmp_path / "absent.json")),
        Mock(),
        browser_open=Mock(),
    )
    assert not manager.status()["configured"]
    with pytest.raises(LoginError, match="Choose your Google Desktop app"):
        manager.start(URI)


def test_sibling_client_file_is_discovered(settings, tmp_path, monkeypatch):
    checkout, sibling = tmp_path / "youtube-download", tmp_path / "youtube-backup"
    checkout.mkdir()
    sibling.mkdir()
    (sibling / "client_secrets.json").write_text(json.dumps(client_config()))
    monkeypatch.chdir(checkout)
    manager = GoogleLogin(settings, Mock(), browser_open=Mock())
    assert manager.status()["configured"]
    assert manager.start(URI)["phase"] == "waiting"


def test_token_write_is_atomic_and_owner_only(settings, tmp_path, monkeypatch):
    path = tmp_path / "token.json"
    path.write_text("old credentials")
    monkeypatch.setattr("youtube_download.account._token_path", lambda: path)
    account = Account(settings.data_dir)
    credentials = Mock()
    credentials.to_json.side_effect = RuntimeError("Cannot serialize")
    with pytest.raises(RuntimeError):
        account.save_credentials(credentials)
    assert path.read_text() == "old credentials"
    credentials.to_json.side_effect = None
    credentials.to_json.return_value = '{"token":"new"}'
    account.save_credentials(credentials)
    assert path.read_text() == '{"token":"new"}'
    assert path.stat().st_mode & 0o777 == 0o600


def test_ui_auth_endpoints_round_trip_without_exposing_tokens(
    client, flow, monkeypatch
):
    manager = client.app.state.login
    manager.browser_open = Mock()
    assert client.post("/api/auth/config", json=client_config()).status_code == 200
    result = client.post("/api/auth/start")
    assert result.status_code == 200 and result.json()["phase"] == "waiting"
    assert "test-client-secret" not in result.text
    assert flow.redirect_uri == "http://127.0.0.1:80/auth/callback"
    response = client.get(
        "/auth/callback?state=expected-state&code=private-code", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/?auth=connected"
    assert "private-code" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/api/auth/status").json()["phase"] == "connected"


def test_login_mutations_use_existing_cross_origin_protection(client):
    for endpoint in ("start", "cancel", "config"):
        assert (
            client.post(
                f"/api/auth/{endpoint}",
                json=client_config(),
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )
    assert (
        client.post("/api/auth/config", content=b"x" * (64 * 1024 + 1)).status_code
        == 413
    )


def test_callback_invalid_state_does_not_change_pending_status(client, flow):
    manager = client.app.state.login
    manager.browser_open = Mock()
    client.post("/api/auth/config", json=client_config())
    client.post("/api/auth/start")
    response = client.get(
        "/auth/callback?state=wrong&code=code", follow_redirects=False
    )
    assert response.headers["location"] == "/?auth=invalid"
    assert client.get("/api/auth/status").json()["phase"] == "waiting"
    client.post("/api/auth/cancel")
    assert client.get("/api/auth/status").json()["phase"] == "cancelled"


def test_non_ascii_callback_state_is_rejected_without_server_error(login, flow):
    login.start(URI)
    assert login.complete("invalid-☃", "code", "") == "invalid"


def test_malformed_endpoint_types_produce_actionable_upload_error(login):
    config = client_config()
    config["installed"]["auth_uri"] = []
    with pytest.raises(LoginError, match="Google's authorization"):
        login.configure(json.dumps(config).encode())
