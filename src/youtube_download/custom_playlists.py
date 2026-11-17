"""Normalize pasted YouTube links without contacting arbitrary input URLs."""

from urllib.parse import parse_qs, urlsplit

from .config import VIDEO_ID

MAX_VIDEOS = 500
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
EMBED_HOSTS = {"youtube-nocookie.com", "www.youtube-nocookie.com"}
SHORT_HOSTS = {"youtu.be", "www.youtu.be"}


def video_id_from_url(value: str) -> str:
    # Retain only the video ID. Tracking, timestamps, and playlist parameters must
    # never turn a pasted video into a whole-playlist download or an external URL.
    if any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        raise ValueError("Invalid video URL")
    url = urlsplit(value if "://" in value else f"https://{value}")
    if (
        url.scheme not in {"http", "https"}
        or url.username is not None
        or url.password is not None
        or url.port not in {None, 80, 443}
    ):
        raise ValueError("Invalid video URL")
    host = url.hostname
    parts = url.path.strip("/").split("/")
    video_id = ""
    if host in SHORT_HOSTS and len(parts) == 1:
        video_id = parts[0]
    elif host in YOUTUBE_HOSTS and url.path == "/watch":
        ids = parse_qs(url.query).get("v", [])
        if len(ids) == 1:
            video_id = ids[0]
    elif len(parts) == 2 and (
        (host in YOUTUBE_HOSTS and parts[0] in {"shorts", "live", "embed"})
        or (host in EMBED_HOSTS and parts[0] == "embed")
    ):
        video_id = parts[1]
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError("Invalid video URL")
    return video_id


def parse_video_urls(text: str) -> list[str]:
    lines = [
        (number, line.strip())
        for number, line in enumerate(text.splitlines(), 1)
        if line.strip()
    ]
    if not lines:
        raise ValueError("Paste at least one YouTube video URL.")
    if len(lines) > MAX_VIDEOS:
        raise ValueError(f"Use at most {MAX_VIDEOS} video URLs per playlist.")
    ids, invalid = [], []
    seen = set()
    for number, line in lines:
        try:
            video_id = video_id_from_url(line)
        except ValueError:
            invalid.append(str(number))
            continue
        if video_id not in seen:
            seen.add(video_id)
            ids.append(video_id)
    if invalid:
        locations = ", ".join(invalid[:5]) + (", …" if len(invalid) > 5 else "")
        raise ValueError(
            f"Invalid YouTube video URL on line(s) {locations}. Use one video URL per line; playlist-only and channel URLs are not supported."
        )
    return ids
