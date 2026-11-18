import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

import pytest
from conftest import PID, VID, VID2
from test_downloads import wait_for
from yt_dlp import parse_options

from youtube_download.cookies import CookieError, CookieSettings
from youtube_download.download_settings import DownloadOptions, DownloadSettings
from youtube_download.metadata import (
    MetadataError,
    MetadataManager,
    metadata_command,
    parse_metadata,
)
from youtube_download.store import Store


def manager_for(store, settings):
    return MetadataManager(
        store, settings, CookieSettings(settings), DownloadSettings(settings)
    )


def test_metadata_command_is_simulation_and_independent_of_download_preferences(
    settings, tmp_path
):
    cookies = CookieSettings(settings)
    cookies.configure("browser", "firefox", "/tmp/test-profile")
    options = DownloadOptions(
        write_thumbnail=True,
        write_subs=True,
        embed_metadata=True,
        socket_timeout=200,
        retries=50,
    )
    with cookies.snapshot() as current:
        command = metadata_command(current, VID, tmp_path, options)
    parsed = parse_options(command[command.index("--ignore-config") :]).ydl_opts
    assert parsed["simulate"] and parsed["skip_download"]
    assert parsed["ignore_no_formats_error"] and not parsed["check_formats"]
    assert parsed["socket_timeout"] == 30 and parsed["retries"] == 1
    assert parsed["cookiesfrombrowser"][0] == "firefox"
    assert not parsed["writethumbnail"] and not parsed["writesubtitles"]
    assert "--no-plugin-dirs" in command and "--no-cache-dir" in command
    assert "--write-info-json" not in command and "--output" not in command
    assert command[-1] == f"https://www.youtube.com/watch?v={VID}"


def test_parse_metadata_preserves_only_supported_fields():
    data = {
        "id": VID,
        "title": " A title ",
        "uploader": " Channel ",
        "thumbnail": "https://i.ytimg.com/test.jpg",
        "url": "signed secret URL",
        "description": "discard",
    }
    assert parse_metadata(json.dumps(data).encode(), VID) == {
        "title": "A title",
        "channel": "Channel",
        "thumbnail": data["thumbnail"],
    }
    data["thumbnail"] = "javascript:alert(1)"
    assert "thumbnail" not in parse_metadata(json.dumps(data).encode(), VID)


def test_invalid_optional_thumbnail_keeps_valid_title():
    data = {"id": VID, "title": "Useful title", "thumbnail": "https://["}
    assert parse_metadata(json.dumps(data).encode(), VID) == {"title": "Useful title"}


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {},
        {"id": VID2, "title": "Wrong"},
        {"id": VID, "title": []},
        {"id": VID, "title": "[Private video]"},
        {"id": VID, "title": " "},
    ],
)
def test_invalid_metadata_cannot_overwrite_video(data):
    with pytest.raises(MetadataError):
        parse_metadata(json.dumps(data).encode(), VID)


@pytest.mark.parametrize("data", [b"not json", b"\xff", b" " * (1024 * 1024 + 1)])
def test_corrupt_or_oversized_metadata_is_rejected(data):
    with pytest.raises(MetadataError):
        parse_metadata(data, VID)


def test_api_queues_custom_metadata_independently_and_guards_mutations(
    client, monkeypatch
):
    custom = client.post(
        "/api/playlists/custom",
        json={
            "title": "Mine",
            "urls": f"https://youtu.be/{VID}\nhttps://youtu.be/{VID2}",
        },
    ).json()["playlist"]["id"]
    route = f"/api/playlists/{custom}/metadata"
    store = client.app.state.store
    monkeypatch.setattr(
        client.app.state.manager,
        "enqueue",
        Mock(side_effect=AssertionError("No media download")),
    )
    first = client.post(route).json()
    assert first["playlist"]["metadata"]["state"] == "queued"
    assert first["playlist"]["state"] == "idle"
    store.update_video(custom, VID, metadata_status="updated", title="Known")
    assert client.post(route).json()["playlist"]["metadata"]["updated"] == 1
    assert (
        client.post(f"{route}/cancel").json()["playlist"]["metadata"]["state"]
        == "paused"
    )
    assert client.post(f"{route}/resume").json()["playlist"]["metadata"]["updated"] == 1
    assert store.pending_metadata_ids(custom) == [VID2]
    client.post(f"{route}/cancel")
    assert client.post(route).json()["playlist"]["metadata"]["updated"] == 0
    assert store.video(custom, VID)["title"] == "Known"
    for suffix in ["", "/resume", "/cancel"]:
        assert (
            client.post(route + suffix, headers={"X-Local-App": ""}).status_code == 403
        )
        assert (
            client.post(
                route + suffix, headers={"Origin": "https://evil.test"}
            ).status_code
            == 403
        )
        assert client.post(f"/api/playlists/{PID}/metadata{suffix}").status_code == 400
    assert client.post("/api/playlists/missing/metadata").status_code == 404


def test_success_failure_and_retry_preserve_membership_media_and_download_status(
    store, settings
):
    custom = store.create_custom_playlist("Mine", [VID, VID2])
    path = store.playlist_directory(custom) / VID / f"{VID}.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"existing media")
    partial = path.with_suffix(".mp4.part")
    partial.write_bytes(b"partial")
    store.update_video(
        custom,
        VID,
        status="downloaded",
        file_path=f"{VID}/{VID}.mp4",
        size=14,
        error="",
    )
    store.update_video(
        custom,
        VID2,
        title="Keep this title",
        status="failed",
        error="Download failed earlier",
    )
    store.update_playlist(custom, state="paused")
    before = store.videos(custom)
    manager = manager_for(store, settings)
    manager._lookup = Mock(
        side_effect=[
            {
                "title": "Fresh title",
                "channel": "Fresh channel",
                "thumbnail": "https://i.ytimg.com/fresh.jpg",
            },
            MetadataError("Unavailable"),
        ]
    )
    manager.enqueue(custom)
    store.update_metadata_job(custom, state="running")
    manager._update_playlist(custom)
    summary = store.playlist_summary(store.playlist(custom))
    assert (
        summary["state"] == "paused"
        and summary["metadata"]["state"] == "completed_with_errors"
    )
    assert summary["metadata"]["updated"] == summary["metadata"]["failed"] == 1
    assert store.video(custom, VID)["title"] == "Fresh title"
    assert store.video(custom, VID2)["title"] == "Keep this title"
    assert store.video(custom, VID2)["metadata_error"] == "Unavailable"
    for old, new in zip(before, store.videos(custom)):
        for key in [
            "id",
            "position",
            "in_playlist",
            "status",
            "error",
            "file_path",
            "size",
        ]:
            assert new[key] == old[key]
    assert path.read_bytes() == b"existing media" and partial.read_bytes() == b"partial"
    manager._lookup = Mock(return_value={"title": "Now available"})
    manager.enqueue(custom, resume=True)
    store.update_metadata_job(custom, state="running")
    manager._update_playlist(custom)
    manager._lookup.assert_called_once_with(custom, VID2)
    assert store.metadata_job(custom)["state"] == "completed"
    assert store.video(custom, VID2)["metadata_error"] == ""


def test_real_process_cancel_resume_skips_completed_lookups(
    store, settings, tmp_path, monkeypatch
):
    custom = store.create_custom_playlist("Mine", [VID, VID2])
    marker = tmp_path / "waiting"
    calls = []
    script = """import json, pathlib, sys, time
marker = pathlib.Path(sys.argv[2])
if sys.argv[1] == sys.argv[3] and not marker.exists():
    marker.write_text('waiting')
    time.sleep(30)
print(json.dumps({'id': sys.argv[1], 'title': 'Fetched ' + sys.argv[1]}))
"""

    def command(settings, video_id, directory, options):
        calls.append(video_id)
        return [sys.executable, "-c", script, video_id, str(marker), VID2]

    monkeypatch.setattr("youtube_download.metadata.metadata_command", command)
    manager = manager_for(store, settings)
    manager.start()
    try:
        manager.enqueue(custom)
        wait_for(marker.exists)
        process = manager.process
        manager.cancel(custom)
        wait_for(lambda: manager.active_playlist is None)
        assert process.poll() is not None
        assert store.metadata_job(custom)["state"] == "paused"
        assert store.pending_metadata_ids(custom) == [VID2]
        manager.enqueue(custom, resume=True)
        wait_for(lambda: store.metadata_job(custom)["state"] == "completed")
        assert calls == [VID, VID2, VID2]
        assert not store.playlist_directory(custom).exists()
    finally:
        manager.close()
    assert not list(manager.sessions.iterdir())


def test_timeout_kills_process_and_leaves_media_state_untouched(
    store, settings, monkeypatch
):
    custom = store.create_custom_playlist("Mine", [VID])
    manager = manager_for(store, settings)
    monkeypatch.setattr("youtube_download.metadata.LOOKUP_TIMEOUT", 0.1)
    monkeypatch.setattr(
        "youtube_download.metadata.metadata_command",
        lambda *args: [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    manager.start()
    try:
        manager.enqueue(custom)
        wait_for(lambda: store.metadata_job(custom)["state"] == "completed_with_errors")
        assert "timed out" in store.video(custom, VID)["metadata_error"]
        assert store.video(custom, VID)["status"] == "pending"
        assert manager.process is None
    finally:
        manager.close()


def test_cookie_failure_stops_job_without_failing_every_video(store, settings):
    custom = store.create_custom_playlist("Mine", [VID, VID2])
    manager = manager_for(store, settings)
    manager._lookup = Mock(side_effect=CookieError("Choose a cookies file."))
    manager.start()
    try:
        manager.enqueue(custom)
        wait_for(lambda: store.metadata_job(custom)["state"] == "error")
        assert "cookies" in store.metadata_job(custom)["error"]
        assert store.pending_metadata_ids(custom) == [VID, VID2]
        manager._lookup.assert_called_once()
    finally:
        manager.close()


def test_restart_recovers_running_job_and_preserves_cancelled_job(store, settings):
    running = store.create_custom_playlist("Running", [VID, VID2])
    paused = store.create_custom_playlist("Paused", [VID])
    store.queue_metadata(running)
    store.update_metadata_job(running, state="running", current_video=VID2)
    store.update_video(
        running, VID, metadata_status="updated", title="Saved before crash"
    )
    store.queue_metadata(paused)
    store.update_metadata_job(paused, state="paused")
    reopened = Store(store.path, store.download_dir)
    manager = manager_for(reopened, settings)
    manager._lookup = Mock(return_value={"title": "After restart"})
    manager.start()
    try:
        wait_for(lambda: reopened.metadata_job(running)["state"] == "completed")
        manager._lookup.assert_called_once_with(running, VID2)
        assert reopened.video(running, VID)["title"] == "Saved before crash"
        assert reopened.metadata_job(paused)["state"] == "paused"
    finally:
        manager.close()


def test_shutdown_requeues_active_lookup_and_kills_child(
    store, settings, tmp_path, monkeypatch
):
    custom = store.create_custom_playlist("Mine", [VID])
    marker = tmp_path / "started"
    script = (
        "import pathlib, sys, time; pathlib.Path(sys.argv[1]).touch(); time.sleep(30)"
    )
    monkeypatch.setattr(
        "youtube_download.metadata.metadata_command",
        lambda *args: [sys.executable, "-c", script, str(marker)],
    )
    manager = manager_for(store, settings)
    manager.start()
    try:
        manager.enqueue(custom)
        wait_for(marker.exists)
        process = manager.process
    finally:
        manager.close()
    assert not manager.thread.is_alive()
    assert process.poll() is not None
    assert store.metadata_job(custom)["state"] == "queued"
    assert store.pending_metadata_ids(custom) == [VID]
    assert not list(manager.sessions.iterdir())


def test_metadata_migration_preserves_existing_library(store):
    with store.connect() as db:
        db.execute("DROP TABLE metadata_jobs")
        for column in ["metadata_status", "metadata_error", "metadata_updated_at"]:
            db.execute(f"ALTER TABLE videos DROP COLUMN {column}")
    reopened = Store(store.path, store.download_dir)
    assert reopened.video(PID, VID)["title"] == "A video"
    assert reopened.video(PID, VID)["metadata_status"] == "pending"
    assert reopened.metadata_job(PID)["state"] == "idle"


def test_real_yt_dlp_metadata_does_not_fetch_media_or_write_sidecars(
    settings, tmp_path
):
    requests = []

    class MediaTrap(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_error(500)

        do_HEAD = do_GET

    server = ThreadingHTTPServer(("127.0.0.1", 0), MediaTrap)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    info = tmp_path / "fixture.json"
    info.write_text(
        json.dumps(
            {
                "id": VID,
                "title": "Offline fixture title",
                "uploader": "Fixture channel",
                "extractor": "generic",
                "webpage_url": "https://www.youtube.com/watch?v=" + VID,
                "formats": [
                    {
                        "format_id": "test",
                        "url": f"http://127.0.0.1:{server.server_port}/must-not-download.mp4",
                        "ext": "mp4",
                        "vcodec": "h264",
                        "acodec": "aac",
                    }
                ],
            }
        )
    )
    command = metadata_command(
        settings, VID, tmp_path, DownloadOptions(write_thumbnail=True, write_subs=True)
    )
    command[-1:] = ["--load-info-json", str(info)]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=20,
            cwd=tmp_path,
            start_new_session=True,
        )
        assert result.returncode == 0, result.stderr
        assert parse_metadata(result.stdout, VID) == {
            "title": "Offline fixture title",
            "channel": "Fixture channel",
        }
        assert requests == []
        assert {p.name for p in tmp_path.iterdir()} == {
            "fixture.json",
            ".metadata.lock",
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
