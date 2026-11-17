"""Browser-initiated Google OAuth with one short-lived, in-memory PKCE attempt."""

import json
import logging
import os
from pathlib import Path
import secrets
import tempfile
from threading import RLock
import time
from urllib.parse import urlparse
import webbrowser

from google_auth_oauthlib.flow import InstalledAppFlow
from youtube_backup.auth import SCOPES

logger = logging.getLogger(__name__)
MAX_CONFIG_BYTES = 64 * 1024
LOGIN_SECONDS = 300


class LoginError(ValueError):
    """An actionable message safe to show in the local UI."""


def validate_config(config) -> dict:
    """Only accept Google's installed-app configuration, never arbitrary OAuth endpoints."""
    installed = config.get("installed") if isinstance(config, dict) else None
    if not isinstance(installed, dict):
        raise LoginError(
            "Choose the Google OAuth client JSON for a Desktop app, not a Web application or service account."
        )
    client_id, client_secret = (
        installed.get("client_id"),
        installed.get("client_secret"),
    )
    if (
        not isinstance(client_id, str)
        or not client_id.endswith(".apps.googleusercontent.com")
        or not isinstance(client_secret, str)
        or not client_secret
    ):
        raise LoginError(
            "The selected file is missing valid Google OAuth client credentials."
        )
    if (
        not isinstance(installed.get("auth_uri"), str)
        or not isinstance(installed.get("token_uri"), str)
        or installed.get("auth_uri")
        not in {
            "https://accounts.google.com/o/oauth2/auth",
            "https://accounts.google.com/o/oauth2/v2/auth",
        }
        or installed.get("token_uri")
        not in {
            "https://oauth2.googleapis.com/token",
            "https://accounts.google.com/o/oauth2/token",
        }
    ):
        raise LoginError(
            "The selected file must use Google's authorization and token endpoints."
        )
    return {"installed": installed}


class GoogleLogin:
    def __init__(
        self, settings, save_credentials, *, clock=time.monotonic, browser_open=None
    ):
        self.settings = settings
        self.save_credentials = save_credentials
        self.clock = clock
        self.browser_open = browser_open or webbrowser.open
        self.lock = RLock()
        self.phase = "idle"
        self.message = "Connect Google to access your YouTube playlists."
        self.flow = None
        self.oauth_state = ""
        self.authorization_url = ""
        self.deadline = 0
        self.attempt = 0
        # OAuth libraries log request bodies at DEBUG, including tokens. Keep those
        # loggers quiet even when the app's diagnostic logging is enabled.
        logging.getLogger("requests_oauthlib").setLevel(logging.WARNING)
        logging.getLogger("oauthlib").setLevel(logging.WARNING)

    def _config_path(self) -> Path | None:
        candidates = [self.settings.data_dir / "oauth-client.json"]
        if self.settings.client_secrets_file:
            candidates.append(Path(self.settings.client_secrets_file).expanduser())
        else:
            candidates.extend(
                [
                    Path.cwd() / "client_secrets.json",
                    Path.cwd().parent / "youtube-backup" / "client_secrets.json",
                ]
            )
        return next((path for path in candidates if path.is_file()), None)

    def _finish(self, phase, message):
        self.phase, self.message = phase, message
        if self.flow is not None:
            self.flow.oauth2session.close()
        self.flow = None
        self.oauth_state = self.authorization_url = ""

    def _expire(self):
        if self.phase == "waiting" and self.clock() >= self.deadline:
            self._finish(
                "expired",
                "Google sign-in timed out. Choose Connect with Google to try again.",
            )

    def status(self) -> dict:
        with self.lock:
            self._expire()
            return {
                "phase": self.phase,
                "message": self.message,
                "configured": self._config_path() is not None,
                "authorization_url": self.authorization_url
                if self.phase == "waiting"
                else "",
                "expires_in": max(0, int(self.deadline - self.clock()))
                if self.phase == "waiting"
                else 0,
            }

    def configure(self, content: bytes) -> dict:
        if len(content) > MAX_CONFIG_BYTES:
            raise LoginError("Choose an OAuth client JSON smaller than 64 KB.")
        try:
            config = validate_config(json.loads(content))
        except (ValueError, UnicodeError) as exc:
            if isinstance(exc, LoginError):
                raise
            raise LoginError("The selected file is not valid JSON.") from exc
        with self.lock:
            self._expire()
            if self.phase in {"waiting", "exchanging"}:
                raise LoginError(
                    "Cancel the current sign-in before choosing a different client file."
                )
            destination = self.settings.data_dir / "oauth-client.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", dir=destination.parent, delete=False
                ) as file:
                    temporary = file.name
                    json.dump(config, file)
                os.replace(temporary, destination)
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)
            self._finish(
                "idle", "Google client file saved. You can connect your account now."
            )
            return self.status()

    def start(self, redirect_uri: str) -> dict:
        parsed = urlparse(redirect_uri)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.path != "/auth/callback"
        ):
            raise LoginError("Google sign-in requires the local Shelf server.")
        with self.lock:
            self._expire()
            if self.phase in {"waiting", "exchanging"}:
                return self.status()  # Duplicate clicks share one state/verifier pair.
            path = self._config_path()
            if path is None:
                raise LoginError(
                    "Choose your Google Desktop app client JSON below to connect your account."
                )
            try:
                if path.stat().st_size > MAX_CONFIG_BYTES:
                    raise LoginError("Choose an OAuth client JSON smaller than 64 KB.")
                config = validate_config(json.loads(path.read_bytes()))
                flow = InstalledAppFlow.from_client_config(
                    config, scopes=SCOPES, autogenerate_code_verifier=True
                )
                flow.redirect_uri = redirect_uri
                url, state = flow.authorization_url(
                    access_type="offline", prompt="consent"
                )
            except LoginError:
                raise
            except (OSError, ValueError, UnicodeError) as exc:
                raise LoginError(
                    "Could not read the Google client file. Choose a valid Desktop app JSON below."
                ) from exc
            self.attempt += 1
            self.flow, self.oauth_state, self.authorization_url = flow, state, url
            self.deadline = self.clock() + LOGIN_SECONDS
            self.phase = "waiting"
            self.message = (
                "Finish signing in with Google in your browser, then return here."
            )
        try:
            self.browser_open(url, new=1, autoraise=True)
        except Exception:
            # The returned link remains usable on systems without a default browser.
            logger.warning("Could not open the Google sign-in browser")
        return self.status()

    def cancel(self) -> dict:
        with self.lock:
            if self.phase in {"waiting", "exchanging"}:
                self.attempt += 1
                # The callback thread owns an exchanging session; let its finally
                # close it. Invalidate the attempt so it cannot save late credentials.
                if self.phase == "exchanging":
                    self.flow = None
                self._finish(
                    "cancelled",
                    "Sign-in cancelled. Your previous account login has been kept.",
                )
            return self.status()

    def complete(self, state: str, code: str, error: str) -> str:
        with self.lock:
            self._expire()
            if (
                self.phase != "waiting"
                or not state
                or not secrets.compare_digest(
                    state.encode("utf-8"), self.oauth_state.encode("utf-8")
                )
            ):
                return "invalid"
            if error:
                self._finish(
                    "error",
                    "Google sign-in was not approved. You can try again when you are ready.",
                )
                return "error"
            if not code:
                self._finish(
                    "error", "Google did not return a sign-in code. Please try again."
                )
                return "error"
            flow, attempt = self.flow, self.attempt
            self.phase = "exchanging"
            self.message = "Finishing your Google connection…"
            self.authorization_url = ""
        try:
            # State is checked once above, before exchanging the code. Passing the
            # code directly avoids disabling OAuthlib HTTPS checks for token traffic.
            flow.fetch_token(code=code, timeout=30)
            credentials = flow.credentials
            if (
                not credentials.valid
                or not credentials.refresh_token
                or not credentials.has_scopes(SCOPES)
            ):
                raise LoginError(
                    "Google did not grant the requested read-only playlist access."
                )
            with self.lock:
                if attempt != self.attempt:
                    return "cancelled"
                self.save_credentials(credentials)
                self._finish(
                    "connected", "Google connected. Sync your playlists to continue."
                )
            logger.info("Google account reconnected")
            return "connected"
        except Exception as exc:
            logger.warning("Google sign-in failed (%s)", type(exc).__name__)
            with self.lock:
                if attempt != self.attempt:
                    return "cancelled"
                self._finish(
                    "error",
                    "Could not finish Google sign-in. Check your connection and try again. Your previous login has been kept.",
                )
            return "error"
        finally:
            flow.oauth2session.close()
