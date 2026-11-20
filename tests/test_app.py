import re
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


def test_browser_revalidates_the_library_ui_after_an_update(client):
    # Overview and detail pages share assets whose contents change on upgrade.
    for url in ("/", f"/playlists/{PID}", "/static/app.js", "/static/style.css"):
        response = client.get(url)
        assert response.headers.get("cache-control") == "no-cache", url
    script = client.get("/static/app.js")
    cached = client.get(
        "/static/app.js", headers={"If-None-Match": script.headers["etag"]}
    )
    assert cached.status_code == 304
    assert cached.headers.get("cache-control") == "no-cache"
    assert client.get(BASE).headers["cache-control"] == "no-store"


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
    assert store.playlist_directory(custom["id"]) == (
        settings.download_dir / Path(custom["download_dir"]).name
    )
    assert (
        store.playlist_directory(store.create_custom_playlist("Next", [VID])).parent
        == settings.download_dir
    )


def test_default_folder_updates_existing_unused_playlists(client, tmp_path):
    store = client.app.state.store
    original = store.playlist_directory(PID)
    original.mkdir()  # Even an allocated, empty directory has no files to strand.
    custom_id = store.create_custom_playlist("Clips", [VID])
    custom_name = store.playlist_directory(custom_id).name
    root = tmp_path / "New default"
    root.mkdir()
    client.post(
        "/api/download-directory", json={"directory": str(root)}
    ).raise_for_status()
    assert client.get(BASE).json()["playlist"]["download_dir"] == str(
        root / original.name
    )
    assert store.playlist_directory(custom_id) == root / custom_name
    client.post("/api/sync").raise_for_status()
    assert store.playlist_directory(PID) == root / original.name


def test_default_folder_repairs_previously_pinned_paths_on_upgrade(settings, tmp_path):
    app = create_app(settings, FakeAccount(), start_worker=False)
    store = app.state.store
    store.sync_playlists([playlist(), playlist("PLreadable"), playlist("PLmanual")])
    readable = store.playlist_directory("PLreadable")
    root = tmp_path / "Previously saved default"
    root.mkdir()
    manual = settings.download_dir / "My chosen folder"
    manual.mkdir()
    # Reproduce a database saved by the previous release, including its
    # absolute startup/ID path and a manually selected child of that same root.
    with store.connect() as db:
        db.execute("INSERT INTO download_directory VALUES(1,?)", (str(root),))
        db.execute(
            "UPDATE playlists SET download_dir=? WHERE id=?",
            (str(settings.download_dir / PID), PID),
        )
        db.execute(
            "UPDATE playlists SET download_dir=? WHERE id='PLmanual'", (str(manual),)
        )
        db.execute("ALTER TABLE playlists DROP COLUMN folder_is_custom")

    assigned = []
    for _ in range(2):
        reopened = create_app(settings, FakeAccount(), start_worker=False)
        with TestClient(reopened, base_url="http://127.0.0.1") as client:
            directory = Path(client.get(BASE).json()["playlist"]["download_dir"])
            assert directory.parent == root
            assert re.fullmatch(r"My playlist - [a-f0-9]{6}", directory.name)
            assigned.append(directory)
            assert (
                reopened.state.store.playlist_directory("PLreadable")
                == root / readable.name
            )
            assert reopened.state.store.playlist_directory("PLmanual") == manual
    assert assigned[0] == assigned[1]
    assert list(root.iterdir()) == []


def test_already_rebound_id_folder_gets_one_readable_name(
    client, tmp_path, monkeypatch
):
    store = client.app.state.store
    root = tmp_path / "Media"
    root.mkdir()
    endpoint = "/api/download-directory"
    client.post(endpoint, json={"directory": str(root)}).raise_for_status()
    # The previous repair saved the correct parent but kept the YouTube ID.
    legacy = root / PID
    legacy.mkdir()
    store.update_playlist(PID, download_dir=str(legacy))
    store.sync_playlists([playlist(title="Trips / 2026")])
    (root / "Trips 2026 - aaaaaa").mkdir()
    monkeypatch.setattr(
        "youtube_download.store.secrets.token_hex",
        Mock(side_effect=["aaaaaa", "bbbbbb"]),
    )

    result = client.post(endpoint, json={"directory": str(root)})
    assert result.json()["updated_playlists"] == 1
    expected = root / "Trips 2026 - bbbbbb"
    assert client.get(BASE).json()["playlist"]["download_dir"] == str(expected)
    store.sync_playlists([playlist(title="Renamed after repair")])
    assert (
        client.post(endpoint, json={"directory": str(root)}).json()["updated_playlists"]
        == 0
    )
    assert store.playlist_directory(PID) == expected
    assert not expected.exists()


@pytest.mark.parametrize("protected", ["partial", "manual", "queued"])
def test_id_folder_protections_apply_inside_current_default(
    client, settings, protected
):
    store = client.app.state.store
    legacy = settings.download_dir / PID
    legacy.mkdir()
    store.update_playlist(PID, download_dir=str(legacy))
    if protected == "partial":
        (legacy / "download.part").write_bytes(b"unfinished")
    elif protected == "manual":
        client.post(
            f"{BASE}/folder", json={"directory": str(legacy)}
        ).raise_for_status()
    else:
        client.post(f"{BASE}/download").raise_for_status()

    assert client.post("/api/download-directory/reset").json()["updated_playlists"] == 0
    assert store.playlist_directory(PID) == legacy


def test_default_folder_preserves_partial_manual_and_conflicting_paths(
    client, tmp_path
):
    store = client.app.state.store
    root = tmp_path / "New default"
    root.mkdir()
    store.sync_playlists(
        [playlist("PLmanual"), playlist("PLcollision"), playlist("PLqueued")]
    )
    manual = store.playlist_directory("PLmanual")
    manual.mkdir()
    client.post(
        "/api/playlists/PLmanual/folder", json={"directory": str(manual)}
    ).raise_for_status()
    old = store.playlist_directory(PID)
    (old / VID).mkdir(parents=True)
    partial = old / VID / "video.part"
    partial.write_bytes(b"partial transfer")
    conflict = store.playlist_directory("PLcollision")
    (root / conflict.name).mkdir()
    queued = store.playlist_directory("PLqueued")
    client.post("/api/playlists/PLqueued/download").raise_for_status()

    client.post(
        "/api/download-directory", json={"directory": str(root)}
    ).raise_for_status()
    assert store.playlist_directory("PLmanual") == manual
    assert store.playlist_directory("PLcollision") == conflict
    assert store.playlist_directory("PLqueued") == queued
    assert store.playlist_directory(PID) == old
    assert partial.read_bytes() == b"partial transfer"


def test_default_folder_preserves_disconnected_source_and_dangling_symlinks(
    client, tmp_path
):
    store = client.app.state.store
    endpoint = "/api/download-directory"
    first = tmp_path / "First drive"
    second = tmp_path / "Second drive"
    first.mkdir()
    second.mkdir()
    client.post(endpoint, json={"directory": str(first)}).raise_for_status()
    source = store.playlist_directory(PID)
    first.rmdir()
    client.post(endpoint, json={"directory": str(second)}).raise_for_status()
    assert store.playlist_directory(PID) == source
    assert not first.exists()
    first.mkdir()
    target = second / source.name
    target.symlink_to(tmp_path / "Missing")
    client.post(endpoint, json={"directory": str(second)}).raise_for_status()
    assert store.playlist_directory(PID) == source
    target.unlink()
    client.post(endpoint, json={"directory": str(second)}).raise_for_status()
    assert store.playlist_directory(PID) == target


def test_default_folder_waits_for_stopping_worker(client, tmp_path):
    manager = client.app.state.manager
    store = client.app.state.store
    original = store.playlist_directory(PID)
    # Pause changes the database state before the active worker releases its folder.
    manager.active_playlist = PID
    client.post(f"{BASE}/pause").raise_for_status()
    result = client.post("/api/download-directory", json={"directory": str(tmp_path)})
    assert result.json()["updated_playlists"] == 0
    assert store.playlist_directory(PID) == original
    manager.active_playlist = None
    result = client.post("/api/download-directory", json={"directory": str(tmp_path)})
    assert result.json()["updated_playlists"] == 1
    assert store.playlist_directory(PID) == tmp_path / original.name


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


def test_failed_directory_save_rolls_back_preference_and_paths(client, tmp_path):
    store = client.app.state.store
    store.update_playlist(PID, download_dir="")
    store.sync_playlists([playlist("PLsecond")])
    original = store.playlist_directory("PLsecond")
    with store.connect() as db:
        db.execute("""CREATE TRIGGER fail_save BEFORE UPDATE OF download_dir ON playlists
                      WHEN NEW.id='PLsecond'
                      BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END""")
    response = client.post("/api/download-directory", json={"directory": str(tmp_path)})
    assert response.status_code == 500
    assert store.directory_override() is None
    assert store.playlist(PID)["download_dir"] == ""
    assert store.playlist_directory("PLsecond") == original


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
