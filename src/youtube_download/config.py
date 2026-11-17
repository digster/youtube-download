"""Runtime settings; secrets stay in the environment and outside API responses."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
from urllib.parse import urlparse

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
PLAYLIST_ID = re.compile(r"^[A-Za-z0-9_-]{2,100}$")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    download_dir: Path
    cookies_file: str = ""
    cookies_browser: str = ""
    format: str = "bv*+ba/b"
    finder_url: str = "https://findyoutubevideo.thetechrobo.ca"

    @classmethod
    def from_env(cls) -> "Settings":
        data = Path(os.getenv("YTDL_DATA_DIR", "data")).expanduser().resolve()
        settings = cls(
            data_dir=data,
            download_dir=Path(os.getenv("YTDL_DOWNLOAD_DIR") or data / "downloads")
            .expanduser()
            .resolve(),
            cookies_file=os.getenv("YTDL_COOKIES_FILE", ""),
            cookies_browser=os.getenv("YTDL_COOKIES_BROWSER", ""),
            format=os.getenv("YTDL_FORMAT") or "bv*+ba/b",
            finder_url=os.getenv("YTDL_FINDER_URL", cls.finder_url).rstrip("/"),
        )
        if settings.cookies_file and settings.cookies_browser:
            raise ValueError(
                "Set only one of YTDL_COOKIES_FILE and YTDL_COOKIES_BROWSER."
            )
        if urlparse(settings.finder_url).scheme not in {"http", "https"}:
            raise ValueError("YTDL_FINDER_URL must be an HTTP(S) URL.")
        return settings


def contained_file(root: Path, relative: str | None) -> Path | None:
    """Never serve paths outside the library, including symlinks escaping it."""
    if not relative:
        return None
    path = (root / relative).resolve()
    if path.is_relative_to(root.resolve()) and path.is_file() and path.stat().st_size:
        return path
    return None
