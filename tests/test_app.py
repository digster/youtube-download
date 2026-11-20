import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest
from conftest import PID, VID, VID2, FakeAccount, playlist, save_video, video
from fastapi.testclient import TestClient

from youtube_download.app import create_app
from youtube_download.config import Settings

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
    assert (new / VID / "A video.txt").read_bytes() == b""


def test_default_download_directory_applies_to_new_playlists(
    client, settings, tmp_path, monkeypatch
):
    store = client.app.state.store
    media = save_video(store)
    client.post(f"{BASE}/download").raise_for_status()
    root = tmp_path / "Video library"
    root.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    usage = Mock(return_value=Mock(free=12345))
    monkeypatch.setattr(shutil, "disk_usage", usage)
    response = client.post(
        "/api/download-directory", json={"directory": "~/Video library"}
    )
    response.raise_for_status()
    assert response.json()["download_dir"] == str(root)
    assert client.get("/api/status").json()["free_bytes"] == 12345
    usage.assert_called_with(root)
    monkeypatch.setattr(
        FakeAccount, "playlists", lambda self: [playlist(), playlist("PLnew")]
    )
    client.post("/api/sync").raise_for_status()
    assert store.playlist_directory(PID) == media.parent.parent
    assert client.get(f"{BASE}/videos/{VID}/file").content == b"local video"
    assert store.playlist(PID)["state"] == "queued"
    assert store.playlist_directory("PLnew").parent == root
    custom = client.post(
        "/api/playlists/custom",
        json={"title": "Local", "urls": f"https://youtu.be/{VID}"},
    ).json()["playlist"]
    assert Path(custom["download_dir"]).parent == root
    client.post("/api/download-directory/reset").raise_for_status()
    assert client.get("/api/status").json()["download_dir"] == str(
        settings.download_dir
    )
    assert store.playlist_directory(custom["id"]).parent == root
    assert (
        store.playlist_directory(store.create_custom_playlist("Next", [VID])).parent
        == settings.download_dir
    )


def test_saved_directory_survives_restart_and_disconnected_drive(settings, tmp_path):
    root = tmp_path / "External drive"
    root.mkdir()
    options = {"base_url": "http://127.0.0.1", "headers": {"X-Local-App": "1"}}
    app = create_app(settings, FakeAccount(), start_worker=False)
    with TestClient(app, **options) as client:
        # Legacy rows have no stored folder. Their ID-based paths must still
        # resolve if the startup environment changes after saving a default.
        client.post("/api/sync").raise_for_status()
        client.post(f"{BASE}/refresh").raise_for_status()
        app.state.store.update_playlist(PID, download_dir="")
        media = save_video(app.state.store)
        client.post(
            "/api/download-directory", json={"directory": str(root)}
        ).raise_for_status()
    root.rmdir()
    new_startup = tmp_path / "Different startup folder"
    reopened = create_app(
        Settings(settings.data_dir, new_startup), FakeAccount(), start_worker=False
    )
    with TestClient(reopened, **options) as client:
        status = client.get("/api/status").json()
        assert status["download_dir"] == str(root) and status["free_bytes"] is None
        assert not root.exists() and not new_startup.exists()
        assert reopened.state.store.playlist_directory(PID) == media.parent.parent
        client.post("/api/download-directory/reset").raise_for_status()
        assert client.get("/api/download-directory").json()["download_dir"] == str(
            new_startup
        )


def test_invalid_default_download_directory_keeps_saved_location(
    client, tmp_path, monkeypatch
):
    endpoint = "/api/download-directory"
    client.post(endpoint, json={"directory": str(tmp_path)}).raise_for_status()
    file = tmp_path / "file"
    file.touch()
    for value in ("relative/folder", str(file), str(tmp_path / "missing"), "/a\0b"):
        response = client.post(endpoint, json={"directory": value})
        assert response.status_code == 400
    assert (
        client.post(
            endpoint, json={"directory": "/"}, headers={"X-Local-App": ""}
        ).status_code
        == 403
    )
    monkeypatch.setattr(
        "youtube_download.app.TemporaryFile", Mock(side_effect=PermissionError)
    )
    response = client.post(endpoint, json={"directory": str(file.parent.parent)})
    assert response.status_code == 400 and "write" in response.json()["detail"]
    assert client.get(endpoint).json()["download_dir"] == str(tmp_path)


def test_failed_directory_save_rolls_back_legacy_paths(client, tmp_path):
    store = client.app.state.store
    store.update_playlist(PID, download_dir="")
    with store.connect() as db:
        db.execute("""CREATE TRIGGER fail_save BEFORE INSERT ON download_directory
                      BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END""")
    response = client.post("/api/download-directory", json={"directory": str(tmp_path)})
    assert response.status_code == 500
    assert store.directory_override() is None
    assert store.playlist(PID)["download_dir"] == ""


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
    from google.auth.exceptions import RefreshError

    def fail(*args):
        raise RefreshError("invalid_grant")

    monkeypatch.setattr(FakeAccount, "playlists", fail)
    response = client.post("/api/sync")
    assert response.status_code == 401
    assert "Reconnect" in response.json()["detail"]


def test_skip_is_scoped_to_membership_and_survives_refresh(client):
    store = client.app.state.store
    other = store.create_custom_playlist("Other list", [VID2])
    endpoint = f"{BASE}/videos/{VID2}/skip"
    for _ in range(2):
        assert client.post(endpoint, json={"skipped": True}).is_success
    detail = client.post(f"{BASE}/refresh").json()
    assert detail["playlist"]["skipped"] == 1
    assert detail["playlist"]["unavailable"] == 0
    assert detail["videos"][1]["download_skipped"]
    assert not store.video(other, VID2)["download_skipped"]
    client.post(endpoint, json={"skipped": False}).raise_for_status()
    assert client.get(BASE).json()["playlist"]["unavailable"] == 1
    assert client.post(endpoint, json={}).status_code == 422
    assert (
        client.post(
            endpoint, json={"skipped": True}, headers={"X-Local-App": ""}
        ).status_code
        == 403
    )


def test_saved_video_selection_controls_future_downloads(client):
    store = client.app.state.store
    media = save_video(store)
    endpoint = f"{BASE}/videos/{VID}/skip"
    client.post(endpoint, json={"skipped": True}).raise_for_status()
    assert client.get(BASE).json()["playlist"]["downloaded"] == 1
    store.sync_videos(PID, [])
    assert client.post(endpoint, json={"skipped": True}).status_code == 409
    assert media.read_bytes() == b"local video"


def test_bulk_download_selection_covers_all_current_members_only(client):
    store = client.app.state.store
    ids = [f"{n:011d}" for n in range(150)]
    custom = store.create_custom_playlist("Many videos", ids)
    store.sync_videos(
        custom, [video(vid, position=i) for i, vid in enumerate(ids[:-1])]
    )
    url = f"/api/playlists/{custom}/videos/skip"
    for skipped in (True, True, False):
        client.post(url, json={"skipped": skipped}).raise_for_status()
        rows = store.videos(custom)
        assert all(v["download_skipped"] == skipped for v in rows if v["in_playlist"])
        assert not rows[-1]["download_skipped"]
    assert not store.video(PID, VID)["download_skipped"]
