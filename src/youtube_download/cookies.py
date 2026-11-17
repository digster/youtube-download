"""Persistent cookie preferences and private, per-download cookie snapshots."""

import json
import logging
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import RLock

logger = logging.getLogger(__name__)
MAX_COOKIE_BYTES = 5 * 1024 * 1024
BROWSERS = (
    "firefox",
    "safari",
    "chrome",
    "brave",
    "chromium",
    "edge",
    "opera",
    "vivaldi",
    "whale",
)


class CookieError(ValueError):
    """An actionable error that is safe to show without cookie contents."""


def validate_cookies(content: bytes) -> str:
    if len(content) > MAX_COOKIE_BYTES:
        raise CookieError("Choose a cookies.txt file smaller than 5 MB.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeError:
        raise CookieError(
            "Choose a text file in Netscape cookies.txt format."
        ) from None
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or not re.match(r"^# (?:Netscape )?HTTP Cookie File", lines[0]):
        raise CookieError(
            "Choose a Netscape cookies.txt export, not JSON or a browser database."
        )
    count = 0
    # yt-dlp's permissive loader prints malformed records, including secret values.
    # Validate all records ourselves before either persisting or launching yt-dlp.
    for line in lines[1:]:
        if line.startswith("#HttpOnly_"):
            line = line.removeprefix("#HttpOnly_")
        elif line.startswith("#") or not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 7 or any(ord(c) < 32 and c != "\t" for c in line):
            raise CookieError("The cookies file contains an invalid Netscape record.")
        domain, subdomains, path, secure, expires, name, value = fields
        if (
            not domain
            or any(c.isspace() for c in domain)
            or subdomains not in {"TRUE", "FALSE"}
            or domain.startswith(".") != (subdomains == "TRUE")
            or not path.startswith("/")
            or secure not in {"TRUE", "FALSE"}
            or (expires and not re.fullmatch(r"[0-9]{1,16}", expires))
            or not (name or value)
        ):
            raise CookieError("The cookies file contains an invalid Netscape record.")
        count += 1
    if not count:
        raise CookieError(
            "The cookies file is empty. Export cookies after signing in to YouTube."
        )
    return "\n".join(lines).rstrip("\n") + "\n"


def browser_spec(browser: str, profile: str = "", container: str = "") -> str:
    if browser not in BROWSERS:
        raise CookieError(
            "Choose a supported browser, such as Firefox, Safari, or Chrome."
        )
    profile, container = profile.strip(), container.strip()
    if (
        any(ord(c) < 32 for c in profile + container)
        or "::" in profile
        or ":" in container
    ):
        raise CookieError("The profile or container contains unsupported characters.")
    if container and browser != "firefox":
        raise CookieError("Containers are only supported for Firefox.")
    return (
        browser
        + (f":{profile}" if profile else "")
        + (f"::{container}" if container else "")
    )


class CookieSettings:
    def __init__(self, settings):
        self.settings = settings
        self.path = settings.data_dir / "cookies.json"
        self.sessions = settings.data_dir / "cookie-sessions"
        self.lock = RLock()
        self.config = None
        if self.path.exists():
            try:
                config = json.loads(self.path.read_text())
                self._validate_config(config)
                self.config = config
            except (ValueError, TypeError, KeyError):
                # Fail closed instead of unexpectedly falling back to another account.
                logger.error(
                    "Saved cookie settings are invalid; downloads will not use cookies until reconfigured"
                )
                self.config = {"mode": "none"}

    @staticmethod
    def _validate_config(config):
        if not isinstance(config, dict) or config.get("mode") not in {
            "none",
            "browser",
            "file",
        }:
            raise CookieError("Invalid cookie settings.")
        if any(
            not isinstance(config[key], str)
            for key in ("browser", "profile", "container", "content", "filename")
            if key in config
        ):
            raise CookieError("Invalid cookie settings.")
        if config["mode"] == "browser":
            browser_spec(
                config["browser"],
                config.get("profile", ""),
                config.get("container", ""),
            )
        if config["mode"] == "file":
            validate_cookies(config["content"].encode("utf-8"))

    def status(self):
        with self.lock:
            config = self.config or {"mode": "environment"}
            mode = config["mode"]
            env_label = "No cookies configured"
            if self.settings.cookies_browser:
                env_label = f"Browser: {self.settings.cookies_browser}"
            elif self.settings.cookies_file:
                env_label = f"File: {Path(self.settings.cookies_file).name}"
            active = mode in {"file", "browser"} or (
                mode == "environment"
                and bool(self.settings.cookies_browser or self.settings.cookies_file)
            )
            # Never serialize the saved cookie text, even after a successful upload.
            return {
                "mode": mode,
                "active": active,
                "browser": config.get("browser", "firefox"),
                "profile": config.get("profile", ""),
                "container": config.get("container", ""),
                "filename": config.get("filename", ""),
                "browsers": list(BROWSERS),
                "environment_label": env_label,
            }

    def _save(self, config):
        with self.lock:
            self.settings.data_dir.mkdir(parents=True, exist_ok=True)
            if config is None:
                self.path.unlink(missing_ok=True)
            else:
                # Persist preferences and uploaded contents in one atomic replacement.
                # Separate files would allow a failed save to change an active source.
                fd, temporary = tempfile.mkstemp(
                    prefix=".cookies-", dir=self.settings.data_dir
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        json.dump(config, handle)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, self.path)
                finally:
                    Path(temporary).unlink(missing_ok=True)
            self.config = config
            logger.info(
                "Download cookie source changed to %s",
                config["mode"] if config else "environment",
            )
            return self.status()

    def configure(self, mode, browser="", profile="", container=""):
        if mode == "environment":
            return self._save(None)
        if mode == "none":
            return self._save({"mode": "none"})
        if mode != "browser":
            raise CookieError("Choose a browser or upload a cookies.txt file.")
        browser_spec(browser, profile, container)
        return self._save(
            {
                "mode": mode,
                "browser": browser,
                "profile": profile.strip(),
                "container": container.strip(),
            }
        )

    def upload(self, content: bytes, filename: str):
        text = validate_cookies(content)
        # Only a display name is retained. A filename can never choose a disk path.
        name = Path(filename.replace("\\", "/")).name[:200]
        name = "".join(c for c in name if ord(c) >= 32) or "cookies.txt"
        return self._save({"mode": "file", "filename": name, "content": text})

    def cleanup_sessions(self):
        # Called only after obtaining the app's exclusive server lock, before jobs
        # resume. Remove sensitive working copies left by a hard-killed process.
        if self.sessions.exists():
            shutil.rmtree(self.sessions)

    @contextmanager
    def snapshot(self):
        with self.lock:
            config = self.config
            mode = config["mode"] if config else "environment"
            browser, content = "", None
            if mode == "browser":
                browser = browser_spec(
                    config["browser"],
                    config.get("profile", ""),
                    config.get("container", ""),
                )
            elif mode == "file":
                content = config["content"]
            elif mode == "environment":
                browser = self.settings.cookies_browser
                if self.settings.cookies_file:
                    try:
                        with (
                            Path(self.settings.cookies_file)
                            .expanduser()
                            .open("rb") as handle
                        ):
                            content = validate_cookies(
                                handle.read(MAX_COOKIE_BYTES + 1)
                            )
                    except OSError:
                        raise CookieError(
                            "Cannot read the cookies file. Choose a file in Account & setup."
                        ) from None
        current = replace(self.settings, cookies_browser=browser, cookies_file="")
        if content is None:
            yield current
            return
        self.sessions.mkdir(mode=0o700, parents=True, exist_ok=True)
        # yt-dlp saves its cookie jar back to --cookies. A private copy prevents
        # in-flight jobs overwriting a newly uploaded source or the user's export.
        with tempfile.TemporaryDirectory(
            prefix="download-", dir=self.sessions
        ) as directory:
            path = Path(directory) / "cookies.txt"
            with open(
                path,
                "x",
                encoding="utf-8",
                opener=lambda p, flags: os.open(p, flags, 0o600),
            ) as handle:
                handle.write(content)
            yield replace(current, cookies_file=str(path), cookies_browser="")
