"""Reuse youtube-backup's OAuth, pagination, retries, and account API."""

from threading import Lock

from youtube_backup.api import YouTubeAPI
from youtube_backup.auth import get_auth_status, get_credentials
from youtube_backup.quota import QuotaTracker

from .config import PLAYLIST_ID, VIDEO_ID


def thumbnail(snippet: dict) -> str:
    images = snippet.get("thumbnails", {})
    return next(
        (
            images[key]["url"]
            for key in ("high", "medium", "default")
            if images.get(key, {}).get("url")
        ),
        "",
    )


class Account:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.lock = Lock()

    def status(self) -> dict:
        status = get_auth_status()
        return {
            "authenticated": status.get("authenticated", False),
            "expired": status.get("expired", False),
            "provider": "youtube-backup",
        }

    def _api(self):
        # Keep this app's advisory quota ledger separate; the server serializes all
        # account operations. Google's actual project quota remains shared.
        return YouTubeAPI(
            get_credentials(), quota=QuotaTracker(self.data_dir / "quota")
        )

    def playlists(self) -> list[dict]:
        with self.lock:
            api = self._api()
            try:
                playlists = []
                for item in api.list_playlists():
                    if not PLAYLIST_ID.fullmatch(item["id"]):
                        continue
                    snippet = item["snippet"]
                    playlists.append(
                        {
                            "id": item["id"],
                            "title": snippet["title"],
                            "description": snippet.get("description", ""),
                            "thumbnail": thumbnail(snippet),
                            "channel": snippet.get("channelTitle", ""),
                            "privacy": item.get("status", {}).get("privacyStatus", ""),
                            "item_count": item.get("contentDetails", {}).get(
                                "itemCount", 0
                            ),
                        }
                    )
                channel = api.get_channel_info()
                liked = (
                    (channel or {})
                    .get("contentDetails", {})
                    .get("relatedPlaylists", {})
                    .get("likes")
                )
                if (
                    liked
                    and PLAYLIST_ID.fullmatch(liked)
                    and liked not in {p["id"] for p in playlists}
                ):
                    playlists.append(
                        {
                            "id": liked,
                            "title": "Liked videos",
                            "description": "Videos you liked on YouTube.",
                            "thumbnail": "",
                            "channel": "Your account",
                            "privacy": "private",
                            "item_count": 0,
                        }
                    )
                return playlists
            finally:
                api._service.close()

    def videos(self, playlist_id: str) -> list[dict]:
        with self.lock:
            api = self._api()
            try:
                videos = []
                for item in api.list_playlist_items(playlist_id):
                    snippet = item.get("snippet", {})
                    video_id = item.get("contentDetails", {}).get(
                        "videoId"
                    ) or snippet.get("resourceId", {}).get("videoId", "")
                    if not VIDEO_ID.fullmatch(video_id):
                        continue
                    title = snippet.get("title", "Untitled video")
                    unavailable = title.lower() in {
                        "private video",
                        "deleted video",
                        "[private video]",
                        "[deleted video]",
                    }
                    videos.append(
                        {
                            "id": video_id,
                            "title": title,
                            "position": snippet.get("position", 0),
                            "channel": snippet.get("videoOwnerChannelTitle", ""),
                            "thumbnail": thumbnail(snippet),
                            "availability": "unavailable" if unavailable else "unknown",
                        }
                    )
                return videos
            finally:
                api._service.close()
