"""Resumable custom-playlist metadata jobs, separate from media downloads."""

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .config import VIDEO_ID
from .cookies import CookieError
from .store import now

logger = logging.getLogger(__name__)
LOOKUP_TIMEOUT = 90
MAX_METADATA_BYTES = 1024 * 1024


class MetadataError(ValueError):
    """A single video could not be inspected; other videos can still proceed."""


def metadata_command(settings, video_id, directory, options):
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError("Invalid video ID")
    # Do not reuse download arguments: preferences may request thumbnails,
    # subtitles or postprocessing. Simulation explicitly prohibits media/files.
    # Compact output avoids persisting format URLs or other extractor internals.
    command = [
        sys.executable,
        "-m",
        "youtube_download.runner",
        str(os.getpid()),
        str(directory / ".metadata.lock"),
        "--ignore-config",
        "--no-plugin-dirs",
        "--no-playlist",
        "--simulate",
        "--skip-download",
        "--no-progress",
        "--no-colors",
        "--no-cache-dir",
        "--ignore-no-formats-error",
        "--no-check-formats",
        "--js-runtimes",
        "node",
        "--socket-timeout",
        str(min(options.socket_timeout, 30)),
        "--extractor-retries",
        str(min(options.retries, 1)),
        "--retries",
        str(min(options.retries, 1)),
        "--print",
        "%(.{id,title,channel,uploader,thumbnail})j",
    ]
    if settings.cookies_file:
        command.extend(["--cookies", settings.cookies_file])
    if settings.cookies_browser:
        command.extend(["--cookies-from-browser", settings.cookies_browser])
    command.append(f"https://www.youtube.com/watch?v={video_id}")
    return command


def parse_metadata(raw: bytes, video_id: str) -> dict:
    try:
        if len(raw) > MAX_METADATA_BYTES:
            raise ValueError("Oversized metadata")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("id") != video_id:
            raise ValueError("Unexpected video")
        title = data.get("title")
        if (
            not isinstance(title, str)
            or not title.strip()
            or title in {"[Deleted video]", "[Private video]"}
        ):
            raise ValueError("Missing title")
        result = {"title": title.strip()[:1000]}
        channel = data.get("channel") or data.get("uploader")
        if isinstance(channel, str) and channel.strip():
            result["channel"] = channel.strip()[:1000]
        thumbnail = data.get("thumbnail")
        if isinstance(thumbnail, str) and len(thumbnail) <= 4096:
            # A bad optional image must not discard an otherwise useful title.
            try:
                url = urlsplit(thumbnail)
                if (
                    url.scheme == "https"
                    and url.hostname
                    and not url.username
                    and not url.password
                ):
                    result["thumbnail"] = thumbnail
            except ValueError:
                pass
        return result
    except (ValueError, TypeError) as exc:
        raise MetadataError(
            "YouTube did not return usable metadata for this video."
        ) from exc


class MetadataManager:
    def __init__(self, store, settings, cookies, download_settings):
        self.store, self.settings = store, settings
        self.cookies, self.download_settings = cookies, download_settings
        self.sessions = settings.data_dir / "metadata-sessions"
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.process = None
        self.active_playlist = None

    def start(self):
        # The app holds its exclusive data-directory lock before starting workers.
        # Only scratch metadata/lock files live here; no downloaded media does.
        if self.sessions.exists():
            shutil.rmtree(self.sessions)
        self.store.recover_metadata_jobs()
        self.thread = threading.Thread(
            target=self._run, name="playlist-metadata", daemon=True
        )
        self.thread.start()

    def enqueue(self, playlist_id, resume=False):
        with self.lock:
            playlist = self.store.playlist(playlist_id)
            if not playlist or playlist["source"] != "custom":
                raise ValueError("Metadata updates are available for custom playlists.")
            if self.store.metadata_job(playlist_id)["state"] in {"queued", "running"}:
                return  # Repeated clicks must not clear completed lookup progress.
            if self.active_playlist == playlist_id:
                raise ValueError("Metadata lookup is stopping. Try again in a moment.")
            self.store.queue_metadata(playlist_id, resume)
        self.wake.set()

    def cancel(self, playlist_id):
        with self.lock:
            if self.store.metadata_job(playlist_id)["state"] in {"queued", "running"}:
                self.store.update_metadata_job(
                    playlist_id, state="paused", current_video=None
                )
                if self.active_playlist == playlist_id:
                    self._terminate()

    def close(self):
        self.stop.set()
        self.wake.set()
        with self.lock:
            self._terminate()
        if self.thread:
            self.thread.join(timeout=10)

    def _terminate(self):
        process = self.process
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
            except ProcessLookupError:
                pass

    def _interrupted(self, playlist_id):
        return (
            self.stop.is_set()
            or self.store.metadata_job(playlist_id)["state"] != "running"
        )

    def _run(self):
        while not self.stop.is_set():
            with self.lock:
                playlist_id = self.store.next_metadata_job()
                if playlist_id:
                    self.active_playlist = playlist_id
                    self.store.update_metadata_job(
                        playlist_id, state="running", error=""
                    )
            if not playlist_id:
                self.wake.wait(1)
                self.wake.clear()
                continue
            try:
                self._update_playlist(playlist_id)
            except Exception as exc:  # noqa: BLE001 - isolate failures so later queued jobs still run
                # Cookie errors are already user-safe. Never log raw extractor
                # output, which can contain credentials or signed media URLs.
                message = (
                    str(exc)
                    if isinstance(exc, CookieError)
                    else "Metadata update stopped. Check the data folder and retry."
                )
                logger.warning(
                    "Metadata job %s stopped (%s)", playlist_id, type(exc).__name__
                )
                with self.lock:
                    if not self._interrupted(playlist_id):
                        self.store.update_metadata_job(
                            playlist_id, state="error", error=message
                        )
            finally:
                with self.lock:
                    self._terminate()
                    self.process = None
                    self.active_playlist = None
                    self.store.update_metadata_job(playlist_id, current_video=None)
                    if (
                        self.stop.is_set()
                        and self.store.metadata_job(playlist_id)["state"] == "running"
                    ):
                        self.store.update_metadata_job(playlist_id, state="queued")

    def _update_playlist(self, playlist_id):
        logger.info("Updating metadata for custom playlist %s", playlist_id)
        for video_id in self.store.pending_metadata_ids(playlist_id):
            with self.lock:
                if self._interrupted(playlist_id):
                    return
                self.store.update_metadata_job(playlist_id, current_video=video_id)
            try:
                metadata = self._lookup(playlist_id, video_id)
                error = ""
            except MetadataError as exc:
                metadata, error = None, str(exc)
            with self.lock:
                if self._interrupted(playlist_id):
                    return
                if error:
                    self.store.update_video(
                        playlist_id,
                        video_id,
                        metadata_status="failed",
                        metadata_error=error,
                    )
                else:
                    self.store.update_video(
                        playlist_id,
                        video_id,
                        **metadata,
                        metadata_status="updated",
                        metadata_error="",
                        metadata_updated_at=now(),
                    )
            # Yield between requests. Cancellation wakes promptly and successful
            # per-video updates are already committed if the server exits here.
            if self.stop.wait(0.25):
                return
        with self.lock:
            if not self._interrupted(playlist_id):
                state = (
                    "completed_with_errors"
                    if self.store.metadata_has_failures(playlist_id)
                    else "completed"
                )
                self.store.update_metadata_job(
                    playlist_id, state=state, finished_at=now(), current_video=None
                )
                logger.info("Metadata job %s: %s", playlist_id, state)

    def _lookup(self, playlist_id, video_id):
        self.sessions.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (
            self.cookies.snapshot() as settings,
            tempfile.TemporaryDirectory(dir=self.sessions) as temporary,
        ):
            directory = Path(temporary)
            command = metadata_command(
                settings, video_id, directory, self.download_settings.snapshot()
            )
            # Disk-backed bounded output avoids an unbounded communicate() buffer
            # and allows cancellation even if the child produces no output.
            with tempfile.TemporaryFile(dir=directory) as output:
                with self.lock:
                    if self._interrupted(playlist_id):
                        return None
                    self.process = subprocess.Popen(
                        command,
                        stdout=output,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    process = self.process
                deadline = time.monotonic() + LOOKUP_TIMEOUT
                try:
                    while process.poll() is None:
                        if self.stop.wait(0.1) or self._interrupted(playlist_id):
                            return None
                        if time.monotonic() >= deadline:
                            raise MetadataError(
                                "Metadata lookup timed out. Retry when your connection is available."
                            )
                        if os.fstat(output.fileno()).st_size > MAX_METADATA_BYTES:
                            raise MetadataError(
                                "The metadata response was too large. Try again later."
                            )
                    if process.returncode:
                        raise MetadataError(
                            "Could not fetch metadata. The video may be private or unavailable; check your cookies and retry."
                        )
                    output.seek(0)
                    return parse_metadata(output.read(MAX_METADATA_BYTES + 1), video_id)
                finally:
                    with self.lock:
                        self._terminate()
                        self.process = None
