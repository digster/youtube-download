import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from conftest import PID, VID
from yt_dlp import parse_options
from yt_dlp.cookies import YoutubeDLCookieJar

from youtube_download.cookies import (
    BROWSERS,
    MAX_COOKIE_BYTES,
    CookieError,
    CookieSettings,
    browser_spec,
)
from youtube_download.downloads import DownloadManager, build_command

COOKIE_TEXT = "# Netscape HTTP Cookie File\n#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tfixture-secret\n"


@pytest.mark.parametrize("browser", BROWSERS)
def test_supported_browser_settings_become_real_yt_dlp_arguments(settings, browser):
    source = CookieSettings(settings)
    source.configure("browser", browser)
    with source.snapshot() as current:
        command = build_command(current, PID, VID)
        spec = command[command.index("--cookies-from-browser") + 1]
        parsed = parse_options(["--ignore-config", "--cookies-from-browser", spec])
        assert parsed.ydl_opts["cookiesfrombrowser"][0] == browser
        assert "--cookies" not in command
    assert CookieSettings(settings).status()["browser"] == browser
    assert source.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "profile,container",
    [("", "Work"), ("default", "none"), ("/Users/me/Firefox Profile", "")],
)
def test_firefox_profile_and_container_match_yt_dlp_grammar(profile, container):
    parsed = parse_options(
        [
            "--ignore-config",
            "--cookies-from-browser",
            browser_spec("firefox", profile, container),
        ]
    )
    assert parsed.ydl_opts["cookiesfrombrowser"] == (
        "firefox",
        profile or None,
        None,
        container or None,
    )


@pytest.mark.parametrize(
    "browser,profile,container",
    [
        ("unknown", "", ""),
        ("chrome", "", "Work"),
        ("firefox", "x::y", ""),
        ("firefox", "x\nfoo", ""),
        ("firefox", "", "Work:other"),
    ],
)
def test_invalid_browser_settings_preserve_previous_choice(
    settings, browser, profile, container
):
    source = CookieSettings(settings)
    previous = source.configure("browser", "safari")
    with pytest.raises(CookieError):
        source.configure("browser", browser, profile, container)
    assert source.status() == previous


def test_cookie_upload_is_private_persistent_and_compatible_with_yt_dlp(settings):
    source = CookieSettings(settings)
    status = source.upload(
        COOKIE_TEXT.replace("\n", "\r\n").encode(), "../../cookies.txt"
    )
    assert status["filename"] == "cookies.txt"
    assert "fixture-secret" not in json.dumps(status)
    source = CookieSettings(settings)
    with source.snapshot() as current:
        path = Path(current.cookies_file)
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        jar = YoutubeDLCookieJar(str(path))
        jar.load()
        assert len(jar) == 1
        assert next(iter(jar)).value == "fixture-secret"
        assert next(iter(jar)).expires is None
    assert not path.exists()


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"[]",
        b"Safari binarycookies\x00",
        b"# Netscape HTTP Cookie File\n",
        COOKIE_TEXT.replace("\tTRUE\t/", "\tFALSE\t/").encode(),
        COOKIE_TEXT.replace("\t0\t", "\tsecret-invalid-expiry\t").encode(),
        COOKIE_TEXT.replace("\tSID\t", "\tSID\x00\t").encode(),
        COOKIE_TEXT.encode() + b"invalid secret-record\n",
        b"\xff",
    ],
)
def test_invalid_upload_preserves_settings_and_never_logs_contents(
    settings, content, capsys, caplog
):
    source = CookieSettings(settings)
    previous = source.configure("browser", "chrome")
    with pytest.raises(CookieError) as error:
        source.upload(content, "cookies.txt")
    assert "secret" not in str(error.value)
    assert "secret" not in caplog.text + capsys.readouterr().err
    assert source.status() == previous
    assert CookieSettings(settings).status() == previous


def test_file_snapshot_is_unchanged_by_switching_or_yt_dlp_writeback(settings):
    source = CookieSettings(settings)
    source.upload(COOKIE_TEXT.encode(), "old.txt")
    with source.snapshot() as first:
        old_path = Path(first.cookies_file)
        source.upload(
            COOKIE_TEXT.replace("fixture-secret", "new-secret").encode(), "new.txt"
        )
        assert "fixture-secret" in old_path.read_text()
        # Simulate yt-dlp saving its jar after a new upload has completed.
        old_path.write_text("overwritten by in-flight job")
        with source.snapshot() as second:
            assert "new-secret" in Path(second.cookies_file).read_text()
            assert first.cookies_file != second.cookies_file
        source.configure("browser", "firefox")
        with source.snapshot() as third:
            assert third.cookies_browser == "firefox" and not third.cookies_file
    assert not old_path.exists()
    assert "new-secret" not in source.path.read_text()


def test_off_overrides_environment_and_reset_restores_it(settings):
    source = CookieSettings(replace(settings, cookies_browser="chrome:Default"))
    assert source.status()["active"]
    source.configure("none")
    with CookieSettings(source.settings).snapshot() as current:
        assert not current.cookies_browser and not current.cookies_file
    source.configure("environment")
    assert not source.path.exists()
    with source.snapshot() as current:
        assert current.cookies_browser == "chrome:Default"


def test_environment_cookie_file_uses_working_copy(settings, tmp_path):
    export = tmp_path / "export.txt"
    export.write_text(COOKIE_TEXT)
    source = CookieSettings(replace(settings, cookies_file=str(export)))
    with source.snapshot() as current:
        assert current.cookies_file != str(export)
        Path(current.cookies_file).write_text("yt-dlp writeback")
    assert export.read_text() == COOKIE_TEXT


def test_failed_disk_save_preserves_previous_source(settings, monkeypatch):
    source = CookieSettings(settings)
    previous = source.configure("browser", "firefox")
    monkeypatch.setattr(
        "youtube_download.cookies.os.replace", Mock(side_effect=OSError("disk full"))
    )
    with pytest.raises(OSError):
        source.upload(COOKIE_TEXT.encode(), "new.txt")
    assert source.status() == previous
    assert CookieSettings(settings).status() == previous
    assert not list(settings.data_dir.glob(".cookies-*"))


def test_snapshot_cleans_up_even_when_launch_fails(store, settings, monkeypatch):
    source = CookieSettings(settings)
    source.upload(COOKIE_TEXT.encode(), "cookies.txt")
    manager = DownloadManager(store, settings, Mock(), source)
    store.update_playlist(PID, state="running")
    monkeypatch.setattr(
        "youtube_download.downloads.subprocess.Popen",
        Mock(side_effect=OSError("failed")),
    )
    with pytest.raises(OSError):
        manager._download_video(PID, VID)
    assert not list(source.sessions.iterdir())


def test_manager_observes_new_source_for_next_video(store, settings, monkeypatch):
    source = CookieSettings(settings)
    manager = DownloadManager(store, settings, Mock(), source)
    observed = []
    monkeypatch.setattr(
        manager,
        "_download_video_with_settings",
        lambda p, v, s: observed.append(s.cookies_browser),
    )
    source.configure("browser", "firefox")
    manager._download_video(PID, VID)
    source.configure("browser", "safari")
    manager._download_video(PID, VID)
    assert observed == ["firefox", "safari"]


def test_cookie_api_persists_and_returns_no_secrets(client):
    for browser in ("firefox", "safari", "chrome"):
        response = client.post(
            "/api/cookies", json={"mode": "browser", "browser": browser}
        )
        assert response.status_code == 200
        assert client.get("/api/cookies").json()["browser"] == browser
    response = client.post("/api/cookies/file?filename=export.txt", content=COOKIE_TEXT)
    assert response.status_code == 200 and response.json()["mode"] == "file"
    assert "fixture-secret" not in response.text + client.get("/api/status").text
    assert client.get("/api/status").json()["cookies"] is True
    assert client.post("/api/cookies", json={"mode": "none"}).status_code == 200
    assert client.get("/api/status").json()["cookies"] is False


def test_cookie_api_rejects_cross_origin_invalid_and_oversize_uploads(client):
    client.post("/api/cookies", json={"mode": "browser", "browser": "firefox"})
    assert (
        client.post(
            "/api/cookies", json={"mode": "browser", "browser": "bogus"}
        ).status_code
        == 400
    )
    assert client.post("/api/cookies/file", content=b"[]").status_code == 400
    assert (
        client.post(
            "/api/cookies/file", content=b"x" * (MAX_COOKIE_BYTES + 1)
        ).status_code
        == 413
    )
    for headers in ({"X-Local-App": ""}, {"Origin": "https://evil.example"}):
        assert (
            client.post(
                "/api/cookies/file", content=COOKIE_TEXT, headers=headers
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/cookies", json={"mode": "none"}, headers=headers
            ).status_code
            == 403
        )
    assert client.get("/api/cookies").json()["browser"] == "firefox"


def test_cookie_api_disk_failure_is_actionable_and_redacted(client, monkeypatch):
    monkeypatch.setattr(
        client.app.state.cookies, "upload", Mock(side_effect=OSError("secret"))
    )
    response = client.post("/api/cookies/file", content=COOKIE_TEXT)
    assert response.status_code == 500 and "permissions" in response.text
    assert "secret" not in response.text


@pytest.mark.parametrize(
    "config",
    [
        {"mode": "browser", "browser": "firefox", "profile": []},
        {"mode": "file", "content": []},
        {"mode": []},
    ],
)
def test_malformed_saved_preferences_fail_closed(settings, config):
    settings.data_dir.mkdir()
    (settings.data_dir / "cookies.json").write_text(json.dumps(config))
    source = CookieSettings(replace(settings, cookies_browser="chrome"))
    assert source.status()["mode"] == "none"
    with source.snapshot() as current:
        assert not current.cookies_browser and not current.cookies_file


def test_missing_environment_cookie_file_is_a_retryable_video_failure(store, settings):
    manager = DownloadManager(
        store, replace(settings, cookies_file="/no/such/cookies-file.txt"), Mock()
    )
    store.update_playlist(PID, state="running")
    manager._download_video(PID, VID)
    result = store.video(PID, VID)
    assert result["status"] == "failed" and "Account & setup" in result["error"]


def test_stale_cookie_working_copies_are_removed_on_startup(settings):
    source = CookieSettings(settings)
    stale = source.sessions / "download-stale" / "cookies.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text(COOKIE_TEXT)
    source.cleanup_sessions()
    assert not source.sessions.exists()
