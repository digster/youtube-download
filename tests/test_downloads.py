import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from functools import partial
from unittest.mock import Mock

import pytest

from conftest import PID, VID, VID2, video, save_video
from youtube_download.downloads import (
    DownloadManager,
    build_command,
    completed_file,
    error_status,
)
from youtube_download.cookies import CookieSettings
from youtube_download.download_settings import DownloadOptions


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    raise AssertionError("Timed out waiting for worker state")


def test_command_uses_stable_paths_and_resume_flags(settings):
    command = build_command(settings, PID, VID)
    assert "--continue" in command and "--ignore-config" in command
    assert "--abort-on-unavailable-fragments" in command
    assert command[command.index("--output") + 1].endswith(f"{PID}/{VID}/{VID}.%(ext)s")
    assert command[-1] == f"https://www.youtube.com/watch?v={VID}"


def test_receipt_required_to_recognize_complete_media(settings):
    folder = settings.download_dir / PID / VID
    folder.mkdir(parents=True)
    path = folder / f"{VID}.mp4"
    path.write_bytes(b"unmerged stream")
    assert completed_file(settings, PID, VID) is None
    (folder / ".completed.jsonl").write_text(json.dumps(str(path)) + "\n")
    assert completed_file(settings, PID, VID) == path


def test_receipt_survives_moving_playlist_folder(settings, tmp_path):
    folder = tmp_path / "moved" / VID
    folder.mkdir(parents=True)
    path = folder / f"{VID}.mp4"
    path.write_bytes(b"completed")
    (folder / ".completed.jsonl").write_text(json.dumps(f"/old/drive/{VID}/{VID}.mp4"))
    assert completed_file(settings, PID, VID, folder.parent) == path


def test_transient_failures_are_not_misclassified_as_unavailable():
    assert error_status("HTTP Error 429: Too Many Requests") == "failed"
    assert error_status("Sign in to confirm you're not a bot") == "failed"
    assert error_status("Private video. Sign in if granted access") == "unavailable"


@pytest.mark.parametrize("custom", [False, True])
def test_worker_pause_resume_and_idempotent_completion(
    store, settings, monkeypatch, custom
):
    if custom:
        playlist_id = store.create_custom_playlist("Resumable custom list", [VID])
    else:
        playlist_id = PID
        store.sync_videos(PID, [video()])
    folder = store.playlist_directory(playlist_id) / VID
    folder.mkdir(parents=True)
    # A real subprocess writes a .part before waiting. The second invocation sees
    # that partial data, completes it, and writes the same receipt yt-dlp uses.
    script = """import json, pathlib, sys, time
p = pathlib.Path(sys.argv[1]); partial = p / "video.part"
if not partial.exists():
    partial.write_bytes(b"first half")
    time.sleep(20)
final = p / "video.mp4"
final.write_bytes(partial.read_bytes() + b" second half")
partial.unlink()
(p / ".completed.jsonl").write_text(json.dumps(str(final)))
"""
    calls = []

    def command(*args):
        calls.append(args[4])
        return [sys.executable, "-c", script, str(folder)]

    monkeypatch.setattr("youtube_download.downloads.build_command", command)
    manager = DownloadManager(store, settings, Mock())
    manager.start()
    try:
        manager.enqueue(playlist_id)
        wait_for(lambda: (folder / "video.part").exists())
        # Saved settings change only future launches; the running command keeps
        # its immutable snapshot, and resume picks up the new retry preference.
        manager.download_settings.configure({"retries": 7})
        assert calls[0].retries == 3
        manager.pause(playlist_id)
        wait_for(lambda: manager.active_playlist is None)
        assert store.playlist(playlist_id)["state"] == "paused"
        assert (folder / "video.part").exists()
        manager.enqueue(playlist_id)
        wait_for(lambda: store.playlist(playlist_id)["state"] == "completed")
        assert store.video(playlist_id, VID)["local_available"]
        assert (folder / "video.mp4").read_bytes() == b"first half second half"
        manager.enqueue(playlist_id)
        wait_for(lambda: store.playlist(playlist_id)["state"] == "completed")
        assert len(calls) == 2
        assert calls[1].retries == 7
    finally:
        manager.close()


def test_unavailable_video_does_not_block_later_video(store, settings, monkeypatch):
    store.sync_videos(PID, [video(availability="unavailable"), video(VID2, position=1)])
    save_video(store, VID2)
    finder = Mock()
    manager = DownloadManager(store, settings, finder)
    store.update_playlist(PID, state="running")
    manager._download_playlist(PID)
    finder.lookup.assert_called_once_with(PID, VID)
    assert store.video(PID, VID2)["local_available"]
    assert store.playlist(PID)["state"] == "completed_with_errors"


def test_skip_active_and_upcoming_videos_then_include_again(
    store, settings, monkeypatch
):
    third = "01234567890"
    store.sync_videos(PID, [video(), video(VID2, position=1), video(third, position=2)])
    started = []
    script = """import json, pathlib, sys, time
folder = pathlib.Path(sys.argv[1])
partial = folder / "video.part"
if sys.argv[2] == "wait":
    partial.write_bytes(b"partial")
    time.sleep(30)
media = folder / "video.mp4"
media.write_bytes(b"complete")
(folder / ".completed.jsonl").write_text(json.dumps(str(media)))
"""

    def command(settings, playlist_id, video_id, directory, options):
        folder = directory / video_id
        folder.mkdir(exist_ok=True, parents=True)
        started.append(video_id)
        return [
            sys.executable,
            "-c",
            script,
            str(folder),
            "wait" if len(started) == 1 else "finish",
        ]

    monkeypatch.setattr("youtube_download.downloads.build_command", command)
    manager = DownloadManager(store, settings, Mock())
    partial = store.playlist_directory(PID) / VID / "video.part"
    manager.start()
    try:
        manager.enqueue(PID)
        wait_for(partial.exists)
        manager.set_skipped(PID, VID2, True)
        manager.set_skipped(PID, VID, True)
        wait_for(
            lambda: (
                store.playlist(PID)["state"] == "completed"
                and manager.active_playlist is None
            )
        )
        assert partial.read_bytes() == b"partial"
        assert started == [VID, third]
        assert store.playlists()[0]["skipped"] == 2
        manager.enqueue(PID)
        wait_for(
            lambda: (
                store.playlist(PID)["state"] == "completed"
                and manager.active_playlist is None
            )
        )
        assert started == [VID, third]
        manager.set_skipped(PID, VID, False)
        manager.enqueue(PID)
        wait_for(lambda: store.playlist(PID)["state"] == "completed")
        assert started == [VID, third, VID]
        assert store.playlists()[0]["downloaded"] == 2
        manager.finder.lookup.assert_not_called()
    finally:
        manager.close()


def test_skipping_only_unavailable_video_completes_without_archive_lookup(
    store, settings
):
    store.sync_videos(PID, [video(availability="unavailable")])
    manager = DownloadManager(store, settings, Mock())
    manager.set_skipped(PID, VID, True)
    store.recover_jobs()
    store.update_playlist(PID, state="running")
    manager._download_playlist(PID)
    manager.finder.lookup.assert_not_called()
    assert store.playlist(PID)["state"] == "completed"
    assert store.playlists()[0]["downloaded"] == 0
    assert store.playlists()[0]["skipped"] == 1


def test_restart_reconciles_receipt_without_downloading_again(
    store, settings, monkeypatch
):
    store.sync_videos(PID, [video()])
    folder = store.playlist_directory(PID) / VID
    folder.mkdir(parents=True)
    path = folder / f"{VID}.mp4"
    path.write_bytes(b"download finished before app crashed")
    (folder / ".completed.jsonl").write_text(json.dumps(str(path)))
    manager = DownloadManager(store, settings, Mock())
    store.update_playlist(PID, state="running")
    monkeypatch.setattr(
        manager,
        "_download_video",
        Mock(side_effect=AssertionError("Must not download")),
    )
    manager._download_playlist(PID)
    assert store.video(PID, VID)["local_available"]
    assert (folder / "A video.txt").read_bytes() == b""


def test_startup_adds_missing_title_files_for_existing_and_retained_media(
    store, settings
):
    first = save_video(store)
    second = save_video(store, VID2)
    store.sync_videos(PID, [video(VID2, title="Café / jam: live?")])
    manager = DownloadManager(store, settings, Mock())
    manager.start()
    try:
        marker = second.parent / "Café jam live.txt"
        wait_for(lambda: marker.exists() and (first.parent / "A video.txt").exists())
        assert marker.read_bytes() == b""
        assert (first.parent / "A video.txt").read_bytes() == b""
        marker.write_text("My notes")
        manager.backfill_title_files()
        assert marker.read_text() == "My notes"
        assert first.read_bytes() == second.read_bytes() == b"local video"
        assert store.playlist(PID)["state"] == "idle"
    finally:
        manager.close()


def test_backfill_ignores_missing_media_and_symlinked_video_folder(
    store, settings, tmp_path
):
    first = save_video(store)
    first.unlink()
    (first.parent / "unfinished.part").write_bytes(b"partial")
    second = save_video(store, VID2)
    moved = tmp_path / "outside"
    second.parent.rename(moved)
    second.parent.symlink_to(moved)
    DownloadManager(store, settings, Mock()).backfill_title_files()
    assert not list(first.parent.glob("*.txt"))
    assert not list(moved.glob("*.txt"))


def test_backfill_recovers_unknown_custom_title_and_caps_unicode_bytes(store, settings):
    custom = store.create_custom_playlist("Existing library", [VID])
    folder = store.playlist_directory(custom) / VID
    folder.mkdir(parents=True)
    (folder / "video.mkv").write_bytes(b"saved")
    (folder / f"{VID}.info.json").write_text(
        json.dumps({"id": VID, "title": "../" + "🌿" * 100})
    )
    store.update_video(custom, VID, status="downloaded", file_path=f"{VID}/video.mkv")
    manager = DownloadManager(store, settings, Mock())
    assert manager.backfill_title_files(custom) == 1
    (marker,) = folder.glob("*.txt")
    assert marker.name == "🌿" * 62 + ".txt"
    assert manager.backfill_title_files(custom) == 0


def test_title_file_permission_failure_preserves_completed_video(
    store, settings, monkeypatch
):
    path = save_video(store)
    monkeypatch.setattr(
        "youtube_download.filenames.os.open", Mock(side_effect=PermissionError())
    )
    DownloadManager(store, settings, Mock())._save_file(PID, VID, path)
    assert store.video(PID, VID)["status"] == "downloaded"
    assert path.read_bytes() == b"local video"


def test_process_launch_failure_does_not_leave_video_downloading(
    store, settings, monkeypatch
):
    store.sync_videos(PID, [video()])
    monkeypatch.setattr(
        "youtube_download.downloads.subprocess.Popen",
        Mock(side_effect=OSError("Cannot launch")),
    )
    manager = DownloadManager(store, settings, Mock())
    manager.start()
    try:
        manager.enqueue(PID)
        wait_for(
            lambda: (
                store.playlist(PID)["state"] == "error"
                and manager.active_playlist is None
            )
        )
        assert store.video(PID, VID)["status"] == "pending"
    finally:
        manager.close()


@pytest.mark.skipif(
    not shutil.which("ffmpeg"), reason="ffmpeg is required for real media verification"
)
@pytest.mark.parametrize("with_cookies", [False, True])
def test_real_yt_dlp_subprocess_downloads_local_media(settings, tmp_path, with_cookies):
    source = tmp_path / "fixture.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=160x90:d=0.3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )
    request_cookies = []

    class FixtureHandler(SimpleHTTPRequestHandler):
        def do_GET(self):
            request_cookies.append(self.headers.get("Cookie", ""))
            super().do_GET()

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(FixtureHandler, directory=str(tmp_path))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cookies = CookieSettings(settings)
    if with_cookies:
        cookies.upload(
            b"# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t0\tfixture\ttest-value\n",
            "fixture-cookies.txt",
        )
    try:
        with cookies.snapshot() as current:
            command = build_command(
                current, PID, VID,
                options=DownloadOptions(
                    retries=5, fragment_retries=5, socket_timeout=30,
                    concurrent_fragments=2, limit_rate="5M",
                ),
            )
            command[-1] = f"http://127.0.0.1:{server.server_port}/fixture.mp4"
            result = subprocess.run(command, capture_output=True, text=True, timeout=25)
            assert result.returncode == 0, result.stderr + result.stdout
            path = completed_file(settings, PID, VID)
            assert path and path.read_bytes() == source.read_bytes()
            modified = path.stat().st_mtime_ns
            result = subprocess.run(command, capture_output=True, text=True, timeout=25)
            assert result.returncode == 0, result.stderr
            assert path.stat().st_mtime_ns == modified
            if with_cookies:
                assert request_cookies and all(
                    "fixture=test-value" in value for value in request_cookies
                )
            else:
                assert not any(request_cookies)
    finally:
        server.shutdown()
        server.server_close()
