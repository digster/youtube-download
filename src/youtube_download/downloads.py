"""Single-worker durable queue with interruptible yt-dlp subprocesses."""

from collections import deque
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .config import PLAYLIST_ID, VIDEO_ID, contained_file
from .cookies import CookieError, CookieSettings
from .store import now

logger = logging.getLogger(__name__)


def build_command(
    settings, playlist_id: str, video_id: str, directory: Path | None = None
) -> list[str]:
    if not PLAYLIST_ID.fullmatch(playlist_id) or not VIDEO_ID.fullmatch(video_id):
        raise ValueError("Invalid playlist or video ID")
    folder = (directory or settings.download_dir / playlist_id) / video_id
    folder.mkdir(parents=True, exist_ok=True)
    # Stable IDs protect resume from title changes and playlist reordering. Ignore
    # global yt-dlp configuration so user hooks/output options cannot break the ledger.
    command = [
        sys.executable,
        "-m",
        "youtube_download.runner",
        str(os.getpid()),
        str(folder / ".download.lock"),
        "--ignore-config",
        "--no-plugin-dirs",
        "--no-playlist",
        "--continue",
        "--part",
        "--no-overwrites",
        "--newline",
        "--no-colors",
        "--progress",
        "--progress-delta",
        "1",
        "--socket-timeout",
        "20",
        "--retries",
        "3",
        "--fragment-retries",
        "3",
        "--abort-on-unavailable-fragments",
        "--js-runtimes",
        "node",
        "--format",
        settings.format,
        "--merge-output-format",
        "mp4/mkv",
        "--write-info-json",
        "--no-simulate",
        "--output",
        str(folder / f"{video_id}.%(ext)s"),
        "--progress-template",
        "download:__PROGRESS__%(progress)j",
        "--print-to-file",
        "after_move:%(filepath)j",
        str(folder / ".completed.jsonl"),
    ]
    if settings.cookies_file:
        command.extend(
            ["--cookies", str(Path(settings.cookies_file).expanduser().resolve())]
        )
    if settings.cookies_browser:
        command.extend(["--cookies-from-browser", settings.cookies_browser])
    command.append(f"https://www.youtube.com/watch?v={video_id}")
    return command


def completed_file(
    settings, playlist_id: str, video_id: str, directory: Path | None = None
) -> Path | None:
    folder = (directory or settings.download_dir / playlist_id) / video_id
    marker = folder / ".completed.jsonl"
    if not marker.is_file():
        return None
    # Only an after_move receipt counts as complete. Glob-matching an .mp4 would
    # mistake an unmerged format stream or truncated download for the final video.
    for line in reversed(marker.read_text().splitlines()):
        try:
            value = json.loads(line)
            if not isinstance(value, str):
                continue
            # A moved playlist keeps the receipt and relative filename. Ignore the
            # old absolute directory stored by yt-dlp when resolving that receipt.
            path = contained_file(folder, Path(value).name)
            if path and path.is_relative_to(folder.resolve()):
                return path
        except (ValueError, OSError):
            continue
    return None


def error_status(message: str) -> str:
    phrases = (
        "video unavailable",
        "private video",
        "video has been removed",
        "video is unavailable",
        "deleted video",
        "removed by the uploader",
        "copyright claim",
        "account has been terminated",
    )
    return (
        "unavailable"
        if any(phrase in message.lower() for phrase in phrases)
        else "failed"
    )


class DownloadManager:
    def __init__(self, store, settings, finder, cookies=None):
        self.store, self.settings, self.finder = store, settings, finder
        self.cookies = cookies or CookieSettings(settings)
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.RLock()
        self.thread = None
        self.process = None
        self.active_playlist = None

    def start(self):
        self.store.recover_jobs()
        self.thread = threading.Thread(
            target=self._run, name="playlist-downloader", daemon=True
        )
        self.thread.start()

    def enqueue(self, playlist_id: str):
        with self.lock:
            playlist = self.store.playlist(playlist_id)
            if not playlist:
                raise ValueError("Playlist not found")
            if playlist["state"] in {"queued", "running"}:
                return  # Repeated clicks cannot enqueue duplicate work.
            if self.active_playlist == playlist_id:
                raise ValueError("Pause is finishing. Try Resume in a moment.")
            self.store.update_playlist(
                playlist_id, state="queued", queued_at=now(), error="", progress=0
            )
        self.wake.set()

    def pause(self, playlist_id: str):
        with self.lock:
            self.store.update_playlist(
                playlist_id, state="paused", current_video=None, progress=0
            )
            if self.active_playlist == playlist_id:
                self._terminate()

    def close(self):
        self.stop.set()
        self.wake.set()
        with self.lock:
            self._terminate()
        if self.thread:
            self.thread.join(timeout=40)

    def _terminate(self):
        process = self.process
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
            except ProcessLookupError:
                pass

    def _interrupted(self, playlist_id):
        return (
            self.stop.is_set() or self.store.playlist(playlist_id)["state"] != "running"
        )

    def _run(self):
        # Give orphan watchdogs from a hard-killed previous server time to stop
        # before the recovered queue can write to the same .part files.
        if self.stop.wait(0.5):
            return
        while not self.stop.is_set():
            with self.lock:
                playlist_id = self.store.next_job()
                if playlist_id:
                    self.active_playlist = playlist_id
                    self.store.update_playlist(playlist_id, state="running", error="")
            if not playlist_id:
                self.wake.wait(1)
                self.wake.clear()
                continue
            try:
                self._download_playlist(playlist_id)
            except Exception:
                logger.exception("Playlist job failed: %s", playlist_id)
                with self.lock:
                    if not self._interrupted(playlist_id):
                        self.store.update_playlist(
                            playlist_id,
                            state="error",
                            error="Download worker failed. Check the app log and retry.",
                        )
            finally:
                with self.lock:
                    self._terminate()
                    self.process = None
                    # Launch/filesystem errors can happen before the per-video
                    # handler runs. Do not leave stale in-flight status in the UI.
                    self.store.reset_inflight_videos(playlist_id)
                    self.active_playlist = None
                    self.store.update_playlist(
                        playlist_id, current_video=None, progress=0
                    )
                    if (
                        self.stop.is_set()
                        and self.store.playlist(playlist_id)["state"] == "running"
                    ):
                        self.store.update_playlist(playlist_id, state="queued")

    def _save_file(self, playlist_id, video_id, path):
        metadata = {}
        if self.store.playlist(playlist_id)["source"] == "custom":
            # Custom lists are created offline from IDs. Enrich them using the
            # sidecar yt-dlp already writes; no OAuth or extra metadata requests.
            # A damaged sidecar must never turn a completed download into a failure.
            try:
                info_path = contained_file(
                    self.store.playlist_directory(playlist_id),
                    f"{video_id}/{video_id}.info.json",
                )
                if info_path and info_path.stat().st_size <= 4 * 1024 * 1024:
                    info = json.loads(info_path.read_text(encoding="utf-8"))
                    if isinstance(info, dict) and info.get("id") == video_id:
                        for field, value in (
                            ("title", info.get("title")),
                            ("channel", info.get("channel") or info.get("uploader")),
                        ):
                            if isinstance(value, str) and value.strip():
                                metadata[field] = value.strip()[:1000]
            except (OSError, ValueError):
                logger.warning(
                    "Could not read completed-video metadata for %s", video_id
                )
        self.store.update_video(
            playlist_id,
            video_id,
            status="downloaded",
            error="",
            file_path=str(path.relative_to(self.store.playlist_directory(playlist_id))),
            size=path.stat().st_size,
            downloaded_at=now(),
            **metadata,
        )

    def _download_playlist(self, playlist_id):
        logger.info("Downloading playlist %s", playlist_id)
        for video in self.store.videos(playlist_id):
            if self._interrupted(playlist_id):
                return
            if not video["in_playlist"] or video["local_available"]:
                continue
            path = completed_file(
                self.settings,
                playlist_id,
                video["id"],
                self.store.playlist_directory(playlist_id),
            )
            if path:
                self._save_file(playlist_id, video["id"], path)
                continue
            if video["availability"] == "unavailable":
                self.store.update_video(playlist_id, video["id"], status="unavailable")
                self.finder.lookup(playlist_id, video["id"])
                continue
            self._download_video(playlist_id, video["id"])
        with self.lock:
            if not self._interrupted(playlist_id):
                videos = [v for v in self.store.videos(playlist_id) if v["in_playlist"]]
                incomplete = any(not v["local_available"] for v in videos)
                self.store.update_playlist(
                    playlist_id,
                    state="completed_with_errors" if incomplete else "completed",
                )

    def _download_video(self, playlist_id, video_id):
        try:
            with self.cookies.snapshot() as settings:
                self._download_video_with_settings(playlist_id, video_id, settings)
        except CookieError as exc:
            with self.lock:
                if not self._interrupted(playlist_id):
                    self.store.update_video(
                        playlist_id, video_id, status="failed", error=str(exc)
                    )
            logger.warning("Cookie setup prevented download of %s", video_id)

    def _download_video_with_settings(self, playlist_id, video_id, settings):
        directory = self.store.playlist_directory(playlist_id)
        command = build_command(settings, playlist_id, video_id, directory)
        with self.lock:
            if self._interrupted(playlist_id):
                return
            self.store.update_video(
                playlist_id, video_id, status="downloading", error=""
            )
            self.store.update_playlist(playlist_id, current_video=video_id, progress=0)
            self.process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
            process = self.process
        errors = deque(maxlen=8)
        last_update = 0
        try:
            for line in process.stdout:
                if line.startswith("__PROGRESS__"):
                    try:
                        progress = json.loads(line.removeprefix("__PROGRESS__"))
                        total = (
                            progress.get("total_bytes")
                            or progress.get("total_bytes_estimate")
                            or 0
                        )
                        percent = (
                            min(
                                100,
                                100 * (progress.get("downloaded_bytes") or 0) / total,
                            )
                            if total
                            else 0
                        )
                        if time.monotonic() - last_update >= 0.8:
                            self.store.update_playlist(playlist_id, progress=percent)
                            last_update = time.monotonic()
                    except (ValueError, TypeError):
                        pass  # Progress is advisory; final completion comes from the receipt.
                elif line.startswith("ERROR:"):
                    errors.append(line.strip()[:1000])
            returncode = process.wait()
        finally:
            process.stdout.close()
            with self.lock:
                self._terminate()
                self.process = None
        path = completed_file(self.settings, playlist_id, video_id, directory)
        if path:
            self._save_file(playlist_id, video_id, path)
        elif self._interrupted(playlist_id):
            self.store.update_video(playlist_id, video_id, status="pending")
        else:
            error = (
                "\n".join(errors)
                or f"yt-dlp exited with code {returncode} without a completed file."
            )
            status = error_status(error)
            self.store.update_video(playlist_id, video_id, status=status, error=error)
            logger.warning("Download %s for video %s", status, video_id)
            if status == "unavailable":
                self.finder.lookup(playlist_id, video_id)
