"""Human-readable names for playlist folders and searchable video title files."""

import logging
import os
import re
import stat
from pathlib import Path

from .config import VIDEO_ID

logger = logging.getLogger(__name__)


def safe_stem(title: str, *, max_bytes: int, max_chars: int | None = None) -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', " ", title)
    clean = " ".join(clean.split()).strip(" .")[:max_chars]
    # Component limits count UTF-8 bytes on Linux. Cut only at a character
    # boundary and leave room for the caller's extension or playlist suffix.
    return (
        clean.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore").rstrip(" .")
    )


def write_title_file(
    directory: Path, video_id: str, file_path: str, title: str
) -> bool:
    relative = Path(file_path)
    if not VIDEO_ID.fullmatch(video_id) or relative.parent != Path(video_id):
        return False
    stem = safe_stem(title, max_bytes=251) or video_id
    try:
        # Pin the ID directory while checking the recorded media and creating
        # its sibling. A symlink or an unplugged drive must not redirect writes.
        folder = os.open(
            directory / video_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            media = os.stat(relative.name, dir_fd=folder, follow_symlinks=False)
            if not stat.S_ISREG(media.st_mode) or not media.st_size:
                return False
            # Exclusive creation also preserves user notes or symlinks with the
            # same name. Rechecking an existing marker leaves its mtime intact.
            os.close(
                os.open(
                    f"{stem}.txt",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o644,
                    dir_fd=folder,
                )
            )
            return True
        finally:
            os.close(folder)
    except (FileExistsError, FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        logger.warning(
            "Could not create title file for %s (%s)", video_id, type(exc).__name__
        )
        return False
