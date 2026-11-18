import shutil

import pytest

from conftest import PID, VID, VID2, playlist, save_video, video

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
