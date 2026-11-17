"""Validated, durable yt-dlp preferences, snapshotted for each new transfer."""

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from threading import RLock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from yt_dlp import YoutubeDL
from yt_dlp.utils import parse_bytes

logger = logging.getLogger(__name__)


class DownloadOptions(BaseModel):
    # A typed allowlist keeps output paths, receipts, resume flags, and process
    # execution under the worker's control. A saved preference is immutable so
    # changing settings cannot mutate the options of a running video.
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    format: str = Field(default="bv*+ba/b", min_length=1, max_length=500)
    merge_output_format: Literal["mp4/mkv", "mp4", "mkv", "webm"] = "mp4/mkv"
    write_subs: bool = False
    write_auto_subs: bool = False
    sub_langs: str = Field(default="en.*", max_length=200)
    embed_subs: bool = False
    embed_metadata: bool = False
    write_thumbnail: bool = False
    limit_rate: str = Field(default="", max_length=30)
    concurrent_fragments: int = Field(default=1, ge=1, le=16)
    retries: int = Field(default=3, ge=0, le=100)
    fragment_retries: int = Field(default=3, ge=0, le=100)
    socket_timeout: int = Field(default=20, ge=1, le=300)
    sleep_interval: int = Field(default=0, ge=0, le=3600)

    @field_validator("format")
    @classmethod
    def valid_format(cls, value):
        value = value.strip()
        if (
            not value
            or value.startswith("-")
            or any(ord(c) < 32 for c in value)
            or "," in value
            or re.search(r"(?<![\w-])(?:all|mergeall)(?![\w-])", value)
        ):
            raise ValueError(
                "Choose one output format; use + to merge or / for fallbacks."
            )
        try:
            # Use the installed downloader's parser, without initialization or
            # network access, to reject malformed selectors before saving them.
            with YoutubeDL(
                {"quiet": True, "no_warnings": True}, auto_init=False
            ) as ydl:
                ydl.build_format_selector(value)
        except (SyntaxError, ValueError, TypeError) as exc:
            raise ValueError("Enter a valid yt-dlp format selector.") from exc
        return value

    @field_validator("sub_langs")
    @classmethod
    def valid_languages(cls, value):
        parts = [part.strip() for part in value.split(",")]
        if not parts or any(
            not re.fullmatch(r"-?[A-Za-z0-9_][A-Za-z0-9_-]*(?:\.\*)?", part)
            for part in parts
        ):
            raise ValueError("Use language codes such as en.*,ja or all,-live_chat.")
        return ",".join(parts)

    @field_validator("limit_rate")
    @classmethod
    def valid_rate(cls, value):
        value = value.strip().upper()
        if value and (
            not re.fullmatch(r"\d+(?:\.\d+)?[KMG]?", value)
            or (parse_bytes(value) or 0) < 1
        ):
            raise ValueError(
                "Use a speed of at least 1 byte/s, such as 500K or 5M, or leave it blank."
            )
        return value

    @model_validator(mode="after")
    def subtitles_available(self):
        if self.embed_subs and not (self.write_subs or self.write_auto_subs):
            raise ValueError(
                "Enable manual or automatic subtitles before embedding them."
            )
        return self

    def arguments(self) -> list[str]:
        result = [
            "--format",
            self.format,
            "--merge-output-format",
            self.merge_output_format,
            "--retries",
            str(self.retries),
            "--fragment-retries",
            str(self.fragment_retries),
            "--socket-timeout",
            str(self.socket_timeout),
            "--concurrent-fragments",
            str(self.concurrent_fragments),
            "--sleep-interval",
            str(self.sleep_interval),
        ]
        if self.limit_rate:
            result.extend(["--limit-rate", self.limit_rate])
        for field, flag in (
            ("write_subs", "--write-subs"),
            ("write_auto_subs", "--write-auto-subs"),
            ("embed_subs", "--embed-subs"),
            ("embed_metadata", "--embed-metadata"),
            ("write_thumbnail", "--write-thumbnail"),
        ):
            if getattr(self, field):
                result.append(flag)
        if self.write_subs or self.write_auto_subs:
            # The equals form also handles an exclusion starting with a hyphen.
            result.append(f"--sub-langs={self.sub_langs}")
        return result


class DownloadSettings:
    def __init__(self, settings):
        self.path = settings.data_dir / "download-settings.json"
        self.lock = RLock()
        self.defaults = DownloadOptions(format=settings.format)
        self.options = self.defaults
        self.overridden = False
        self.warning = ""
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                raise ValueError("Settings file too large")
            self.options = DownloadOptions.model_validate(json.loads(raw))
            self.overridden = True
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self.warning = "Saved download settings could not be read. Startup defaults are active; save settings to replace the file."
            logger.warning(
                "Saved download settings are unreadable; using startup defaults"
            )

    def status(self):
        with self.lock:
            return {
                "options": self.options.model_dump(),
                "defaults": self.defaults.model_dump(),
                "overridden": self.overridden,
                "warning": self.warning,
            }

    def snapshot(self) -> DownloadOptions:
        with self.lock:
            return self.options

    def configure(self, values: dict):
        options = DownloadOptions.model_validate(values)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=".download-settings-", dir=self.path.parent
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(options.model_dump_json())
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            # Publish only after durable replacement; failed writes leave both
            # the next transfer's options and the previous saved file unchanged.
            self.options, self.overridden, self.warning = options, True, ""
            logger.info("Updated yt-dlp download settings")
            return self.status()

    def reset(self):
        with self.lock:
            self.path.unlink(missing_ok=True)
            self.options, self.overridden, self.warning = self.defaults, False, ""
            logger.info("Restored yt-dlp startup defaults")
            return self.status()
