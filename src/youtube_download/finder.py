"""Adapter for the v5 API implemented by TheTechRobo/youtubevideofinder."""

from datetime import datetime, timedelta, timezone
import json
import logging
from threading import Lock
from urllib.parse import urlparse

import httpx

from .config import VIDEO_ID
from .store import now

logger = logging.getLogger(__name__)


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
        for link in service.get("available") or []:
            if not isinstance(link, dict):
                continue
            url = link.get("url", "")
            if (
                not isinstance(url, str)
                or urlparse(url).scheme not in {"https", "http"}
                or url in seen
            ):
                continue
            seen.add(url)
            contains = link.get("contains") or {}
            links.append(
                {
                    "url": url,
                    "title": str(link.get("title") or service.get("name") or "Archive"),
                    "service": str(service.get("name", "Archive")),
                    "kind": "video"
                    if isinstance(contains, dict) and contains.get("video")
                    else "metadata",
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

    def lookup(self, playlist_id: str, video_id: str, force=False) -> dict:
        if not VIDEO_ID.fullmatch(video_id):
            raise ValueError("Invalid video ID")
        # Serialize requests and cache for seven days so retries and duplicate
        # button presses do not fan out calls across the upstream archive services.
        with self.lock:
            video = self.store.video(playlist_id, video_id)
            if video is None:
                raise ValueError("Video not found")
            if not force and video["recovery_checked_at"]:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(
                    video["recovery_checked_at"]
                )
                if (
                    age < timedelta(days=7)
                    and video["recovery"]
                    and not video["recovery"].get("error")
                ):
                    return video["recovery"]
            try:
                if self.client is not None:
                    response = self.client.get(f"{self.base_url}/api/v5/{video_id}")
                else:
                    with httpx.Client(
                        timeout=httpx.Timeout(30, connect=10),
                        follow_redirects=True,
                        headers={
                            "User-Agent": "youtube-download/0.1 (youtubevideofinder integration)"
                        },
                    ) as client:
                        response = client.get(f"{self.base_url}/api/v5/{video_id}")
                response.raise_for_status()
                result = parse_response(response.json())
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                logger.warning("Archive lookup failed for video %s", video_id)
                result = {
                    **(
                        video["recovery"]
                        or {"links": [], "service_errors": [], "verdict": ""}
                    ),
                    "error": "Archive lookup failed. Try again later or open YouTube Video Finder.",
                }
            self.store.update_video(
                playlist_id,
                video_id,
                recovery=json.dumps(result),
                recovery_checked_at=now(),
            )
            return result
