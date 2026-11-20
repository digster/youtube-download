"""Adapter for the v5 API implemented by TheTechRobo/youtubevideofinder."""

import json
import logging
import time
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from threading import Event, Lock, Thread
from urllib.parse import urlparse

import httpx

from .config import VIDEO_ID

logger = logging.getLogger(__name__)
REQUEST_DELAY = 5.0


def retry_delay(response):
    """Respect both HTTP forms of Retry-After, with a cooldown for busy servers."""
    busy = response.status_code in {429, 503}
    if not busy and "Retry-After" not in response.headers:
        return REQUEST_DELAY
    value = response.headers.get("Retry-After", "")
    try:
        seconds = (
            float(int(value))
            if value.isdigit()
            else parsedate_to_datetime(value).timestamp() - time.time()
        )
        return max(REQUEST_DELAY, seconds)
    except (ValueError, TypeError, OverflowError):
        return 60.0 if busy else REQUEST_DELAY


def parse_response(payload: dict) -> dict:
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "ok"
        or not isinstance(payload.get("keys"), list)
    ):
        raise ValueError("Unexpected youtubevideofinder response")
    links, errors, seen = [], [], set()
    for service in payload["keys"]:
        if not isinstance(service, dict):
            continue
        if service.get("error"):
            errors.append(str(service.get("name", "Archive service")))
        service_name = str(service.get("name") or "Archive")
        original = (
            service.get("classname") == "YouTube"
            or service_name.casefold() == "youtube"
        )
        for link in service.get("available") or []:
            if not isinstance(link, dict):
                continue
            url = link.get("url", "")
            if not isinstance(url, str) or (service_name, url) in seen:
                continue
            try:
                parsed = urlparse(url)
                if parsed.scheme not in {"https", "http"} or not parsed.hostname:
                    continue
            except ValueError:
                continue
            seen.add((service_name, url))
            contains = link.get("contains") or {}
            contents = (
                [key for key, value in contains.items() if value is True]
                if isinstance(contains, dict)
                else []
            )
            if original:
                kind = "original"
            elif "video" in contents or "standalone_video" in contents:
                kind = "video"
            elif "standalone_audio" in contents:
                kind = "audio"
            else:
                kind = "metadata"
            links.append(
                {
                    "url": url,
                    "title": str(link.get("title") or service.get("name") or "Archive"),
                    "service": service_name,
                    "kind": kind,
                    "contents": contents,
                    "note": str(link.get("note") or ""),
                }
            )
    return {
        "links": links,
        "service_errors": errors,
        "verdict": str(
            (payload.get("verdict") or {}).get("human_friendly") or "Search complete"
        ),
        "error": None,
    }


class Finder:
    def __init__(self, store, base_url: str, client=None):
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.client = client
        self.lock = Lock()
        self.stop = Event()
        self.wake = Event()
        self.thread = None

    def start(self):
        self.thread = Thread(target=self._run, name="archive-checker", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join()

    def _run(self):
        while not self.stop.is_set():
            self.wake.clear()
            try:
                if self.check_next():
                    continue
            except InterruptedError:
                if self.stop.is_set():
                    return
                raise
            except Exception:
                logger.exception("Archive worker could not finish a lookup")
            self.wake.wait(10)

    def check_next(self):
        with self.lock:
            video_id = self.store.next_archive_id()
            if video_id is None or self.stop.is_set():
                return False
            self._fetch(video_id, self.store.archive_check(video_id), pending_only=True)
            return True

    def wait_for_checks(self, playlist_id, interrupted, video_id=None):
        # Downloads can be requested during a scan. Wait outside the download
        # launch lock so pause, shutdown and checkbox changes remain available.
        if video_id is None:
            self.store.queue_missing_archives(playlist_id)
            self.wake.set()
        while self.store.pending_archives(playlist_id, video_id):
            if interrupted() or self.stop.wait(0.5):
                return False
        return True

    def lookup(self, playlist_id: str, video_id: str, force=False) -> dict:
        if not VIDEO_ID.fullmatch(video_id):
            raise ValueError("Invalid video ID")
        with self.lock:
            if self.store.video(playlist_id, video_id) is None:
                raise LookupError("Video not found")
            row = self.store.archive_check(video_id)
            if (
                not force
                and row
                and row["checked_at"]
                and row["result"]
                and not row["requested_at"]
            ):
                age = datetime.now(UTC) - datetime.fromisoformat(row["checked_at"])
                result = json.loads(row["result"])
                if age < timedelta(days=7) and not result["error"]:
                    return result
            return self._fetch(video_id, row)

    def _wait_for_slot(self):
        deadline = self.store.finder_next_request()
        while (remaining := deadline - time.time()) > 0 and not self.stop.is_set():
            self.stop.wait(min(remaining, 60))
        if self.stop.is_set():
            raise InterruptedError("Archive checker is stopping.")

    def _request(self, video_id, pending_only):
        # Follow redirects one hop at a time. Automatic redirects would send a
        # second request immediately, bypassing the shared site-wide cooldown.
        transport = (
            nullcontext(self.client)
            if self.client is not None
            else httpx.Client(
                timeout=httpx.Timeout(30, connect=10),
                headers={
                    "User-Agent": "youtube-download/0.1 (youtubevideofinder integration)"
                },
            )
        )
        with transport as client:
            request = client.build_request("GET", f"{self.base_url}/api/v5/{video_id}")
            for _ in range(client.max_redirects + 1):
                self._wait_for_slot()
                if pending_only:
                    check = self.store.archive_check(video_id)
                    if not check or not check["requested_at"]:
                        return None  # Removed while waiting for the cooldown.
                self.store.reserve_finder_request(time.time() + REQUEST_DELAY)
                response = client.send(request, follow_redirects=False)
                if response.next_request is None:
                    return response
                self.store.reserve_finder_request(time.time() + retry_delay(response))
                request = response.next_request
        raise httpx.TooManyRedirects("Finder exceeded the redirect limit")

    def _fetch(self, video_id, previous, pending_only=False):
        # Both manual lookups and the worker hold the same lock from admission
        # through persistence. The delay starts when the previous response ends.
        delay = REQUEST_DELAY
        try:
            response = self._request(video_id, pending_only)
            if response is None:
                return
            delay = retry_delay(response)
            if delay > REQUEST_DELAY:
                logger.info(
                    "Finder returned HTTP %s. Waiting %.0f seconds before the next request.",
                    response.status_code,
                    delay,
                )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and payload.get("id", video_id) != video_id:
                raise ValueError("Finder returned a different video")
            result = parse_response(payload)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            logger.warning("Archive lookup failed for video %s", video_id)
            result = (
                json.loads(previous["result"])
                if previous and previous["result"]
                else {"links": [], "service_errors": [], "verdict": ""}
            )
            result["error"] = (
                "Archive lookup failed. Retry the check or open YouTube Video Finder."
            )
        self.store.finish_archive_check(video_id, result, time.time() + delay)
        return result
