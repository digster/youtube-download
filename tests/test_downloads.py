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


def test_worker_pause_resume_and_idempotent_completion(store, settings, monkeypatch):
    store.sync_videos(PID, [video()])
    folder = store.playlist_directory(PID) / VID
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
        calls.append(1)
        return [sys.executable, "-c", script, str(folder)]

    monkeypatch.setattr("youtube_download.downloads.build_command", command)
    manager = DownloadManager(store, settings, Mock())
    manager.start()
    try:
        manager.enqueue(PID)
        wait_for(lambda: (folder / "video.part").exists())
        manager.pause(PID)
        wait_for(lambda: manager.active_playlist is None)
        assert store.playlist(PID)["state"] == "paused"
        assert (folder / "video.part").exists()
        manager.enqueue(PID)
        wait_for(lambda: store.playlist(PID)["state"] == "completed")
        assert store.video(PID, VID)["local_available"]
        assert (folder / "video.mp4").read_bytes() == b"first half second half"
        manager.enqueue(PID)
        wait_for(lambda: store.playlist(PID)["state"] == "completed")
        assert len(calls) == 2
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
            command = build_command(current, PID, VID)
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
