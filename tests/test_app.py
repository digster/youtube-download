import shutil

import pytest

from conftest import PID, VID, playlist, save_video

BASE = f"/api/playlists/{PID}"


def test_pages_and_security_headers(client):
    for url in ["/", f"/playlists/{PID}", "/static/polling.js", "/static/app.js"]:
        response = client.get(url)
        assert response.status_code == 200
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.get("/api/status").json()["account"]["provider"] == "youtube-backup"
    html = client.get("/").text
    assert html.index("/static/polling.js") < html.index("/static/app.js")


def test_start_is_idempotent_and_pause_persists(client):
    assert client.post(f"{BASE}/download").status_code == 200
    first = client.app.state.store.playlist(PID)["queued_at"]
    client.post(f"{BASE}/download")
    assert client.app.state.store.playlist(PID)["queued_at"] == first
    assert client.post(f"{BASE}/pause").json()["playlist"]["state"] == "paused"


def test_cross_site_mutations_and_host_rebinding_are_rejected(client):
    assert client.post("/api/sync", headers={"X-Local-App": ""}).status_code == 403
    assert (
        client.post("/api/sync", headers={"Origin": "https://example.com"}).status_code
        == 403
    )
    assert (
        client.get("/api/status", headers={"Host": "evil.example"}).status_code == 400
    )


def test_refresh_is_rejected_while_queued(client):
    client.post(f"{BASE}/download")
    assert client.post(f"{BASE}/refresh").status_code == 409


def test_file_supports_range_requests_and_missing_files(client):
    path = save_video(client.app.state.store, content=b"0123456789")
    url = f"{BASE}/videos/{VID}/file"
    response = client.get(url, headers={"Range": "bytes=2-5"})
    assert response.status_code == 206 and response.content == b"2345"
    path.unlink()
    assert client.get(url).status_code == 404


def test_change_folder_rechecks_files_and_serves_moved_video(client, tmp_path):
    store = client.app.state.store
    save_video(store)
    new = tmp_path / "Moved playlist"
    shutil.move(store.playlist_directory(PID), new)
    response = client.post(f"{BASE}/folder", json={"directory": str(new)})
    assert response.status_code == 200
    assert response.json()["playlist"]["downloaded"] == 1
    assert client.get(f"{BASE}/videos/{VID}/file").content == b"local video"


@pytest.mark.parametrize(
    "directory", ["relative/path", "/does-not-exist-yt-download-test"]
)
def test_invalid_folder_does_not_change_library(client, directory):
    old = client.app.state.store.playlist_directory(PID)
    assert (
        client.post(f"{BASE}/folder", json={"directory": directory}).status_code == 400
    )
    assert client.app.state.store.playlist_directory(PID) == old


def test_active_folder_and_already_assigned_folder_are_rejected(client, tmp_path):
    client.post(f"{BASE}/download")
    assert (
        client.post(f"{BASE}/folder", json={"directory": str(tmp_path)}).status_code
        == 409
    )
    client.post(f"{BASE}/pause")
    store = client.app.state.store
    store.sync_playlists([playlist("PLanother")])
    store.update_playlist("PLanother", download_dir=str(tmp_path))
    assert (
        client.post(f"{BASE}/folder", json={"directory": str(tmp_path)}).status_code
        == 409
    )


def test_account_error_preserves_cached_library(client, monkeypatch):
    from conftest import FakeAccount

    def fail(*args):
        raise RuntimeError("secret data that must not appear")

    monkeypatch.setattr(FakeAccount, "playlists", fail)
    response = client.post("/api/sync")
    assert response.status_code == 502
    assert "secret" not in response.text
    assert len(client.get("/api/playlists").json()["playlists"]) == 1


def test_unknown_ids_return_404(client):
    assert client.get("/api/playlists/PLunknown").status_code == 404
    assert client.post(f"{BASE}/videos/abcdefghijk/find").status_code == 404


def test_expired_oauth_gives_reconnect_instructions(client, monkeypatch):
    from conftest import FakeAccount
    from google.auth.exceptions import RefreshError

    def fail(*args):
        raise RefreshError("invalid_grant")

    monkeypatch.setattr(FakeAccount, "playlists", fail)
    response = client.post("/api/sync")
    assert response.status_code == 401
    assert "Reconnect" in response.json()["detail"]
