from dataclasses import replace
from unittest.mock import Mock

import pytest
from conftest import PID, VID
from pydantic import ValidationError
from yt_dlp import parse_options

from youtube_download.cookies import CookieSettings
from youtube_download.download_settings import DownloadOptions, DownloadSettings
from youtube_download.downloads import build_command


def test_defaults_saved_override_restart_and_reset(settings):
    settings = replace(settings, format="best")
    source = DownloadSettings(settings)
    assert source.status()["options"]["format"] == "best"
    assert not source.status()["overridden"]
    before = source.snapshot()
    result = source.configure(
        {
            "format": "bv*[height<=720]+ba/b[height<=720]",
            "limit_rate": " 5m ",
            "retries": 7,
        }
    )
    assert result["options"]["limit_rate"] == "5M"
    assert source.path.stat().st_mode & 0o777 == 0o600
    assert before.format == "best" and before.retries == 3
    assert DownloadSettings(settings).status() == result
    source.reset()
    assert not source.path.exists()
    assert DownloadSettings(settings).snapshot() == before


@pytest.mark.parametrize(
    "values",
    [
        {"format": ""},
        {"format": "best["},
        {"format": "all"},
        {"format": "best,worst"},
        {"format": "--exec=echo"},
        {"format": "best\n--exec touch /tmp/no"},
        {"limit_rate": "0"},
        {"limit_rate": "0.0001"},
        {"limit_rate": "-1M"},
        {"limit_rate": "NaN"},
        {"limit_rate": "5M --exec=echo"},
        {"merge_output_format": "exe"},
        {"retries": -1},
        {"retries": True},
        {"retries": "3"},
        {"fragment_retries": 101},
        {"socket_timeout": 0},
        {"sleep_interval": 3601},
        {"concurrent_fragments": 17},
        {"write_subs": "false"},
        {"embed_subs": True},
        {"sub_langs": "en,(a+)+$"},
        {"sub_langs": "en,,ja"},
        {"exec": "touch /tmp/no"},
        {"output": "/tmp/no"},
        {"cookies_file": "/tmp/no"},
        {"continue": False},
    ],
)
def test_invalid_or_reserved_options_cannot_replace_saved_preferences(settings, values):
    source = DownloadSettings(settings)
    previous = source.configure({"retries": 8})
    with pytest.raises(ValidationError):
        source.configure(values)
    assert source.status() == previous
    assert DownloadSettings(settings).status() == previous


def test_failed_save_or_reset_keeps_active_options(settings, monkeypatch):
    source = DownloadSettings(settings)
    original = source.configure({"retries": 9})
    disk = source.path.read_bytes()
    monkeypatch.setattr(
        "youtube_download.download_settings.os.replace",
        Mock(side_effect=PermissionError),
    )
    with pytest.raises(PermissionError):
        source.configure({"retries": 1})
    assert source.path.read_bytes() == disk
    assert source.status() == original
    assert not list(source.path.parent.glob(".download-settings-*"))
    monkeypatch.setattr(type(source.path), "unlink", Mock(side_effect=PermissionError))
    with pytest.raises(PermissionError):
        source.reset()
    assert source.status() == original


@pytest.mark.parametrize(
    "content", ["not json", "[]", '{"retries": -1}', '"' + "a" * 70_000 + '"']
)
def test_bad_saved_file_uses_defaults_with_visible_warning(settings, content):
    source = DownloadSettings(settings)
    source.path.parent.mkdir(parents=True, exist_ok=True)
    source.path.write_text(content)
    reopened = DownloadSettings(settings)
    assert reopened.snapshot() == source.defaults
    assert reopened.status()["warning"]
    assert reopened.configure({"retries": 5})["warning"] == ""


@pytest.mark.parametrize("height", [480, 720, 1080, 1440, 2160])
def test_resolution_presets_select_lower_available_quality(height):
    from yt_dlp import YoutubeDL

    options = DownloadOptions(format=f"bv*[height<={height}]+ba/b[height<={height}]")
    with YoutubeDL({"quiet": True}, auto_init=False) as ydl:
        selector = ydl.build_format_selector(options.format)
        # Sorted muxed formats exercise the fallback when separate audio isn't
        # available; a quality limit must never quietly choose a higher stream.
        formats = [
            {
                "format_id": str(h),
                "height": h,
                "ext": "mp4",
                "vcodec": "h264",
                "acodec": "aac",
                "url": f"https://example.test/{h}",
            }
            for h in [360, 720, 1080, 2160, 4320]
        ]
        selected = list(
            selector(
                {
                    "formats": formats,
                    "has_merged_format": True,
                    "incomplete_formats": False,
                }
            )
        )
        assert selected and selected[0]["height"] <= height


def test_saved_flags_parse_in_real_yt_dlp_and_keep_cookie_and_receipt_contract(
    settings,
):
    options = DownloadOptions(
        format="bv*[height<=1080]+ba/b[height<=1080]",
        merge_output_format="mkv",
        write_subs=True,
        write_auto_subs=True,
        sub_langs="all, -live_chat",
        embed_subs=True,
        embed_metadata=True,
        write_thumbnail=True,
        limit_rate="2.5M",
        concurrent_fragments=4,
        retries=7,
        fragment_retries=8,
        socket_timeout=45,
        sleep_interval=2,
    )
    cookies = CookieSettings(settings)
    cookies.configure("browser", "firefox", "/tmp/fixture-profile")
    with cookies.snapshot() as current:
        command = build_command(current, PID, VID, options=options)
    parsed = parse_options(command[command.index("--ignore-config") :]).ydl_opts
    assert parsed["format"] == options.format
    assert parsed["merge_output_format"] == "mkv"
    assert parsed["writesubtitles"] and parsed["writeautomaticsub"]
    assert parsed["subtitleslangs"] == ["all", "-live_chat"]
    assert parsed["concurrent_fragment_downloads"] == 4
    assert parsed["retries"] == 7 and parsed["fragment_retries"] == 8
    assert parsed["socket_timeout"] == 45 and parsed["sleep_interval"] == 2
    assert parsed["ratelimit"] == int(2.5 * 1024 * 1024)
    assert parsed["cookiesfrombrowser"][0] == "firefox"
    assert "--continue" in command and "--no-overwrites" in command
    assert "--print-to-file" in command and "--no-plugin-dirs" in command
    assert command[-1] == f"https://www.youtube.com/watch?v={VID}"
    assert {item["key"] for item in parsed["postprocessors"]} >= {
        "FFmpegEmbedSubtitle",
        "FFmpegMetadata",
    }


def test_settings_api_validation_reset_and_local_origin(client, monkeypatch):
    previous = client.get("/api/download-settings").json()
    assert (
        client.post("/api/download-settings", json={"retries": -1}).status_code == 400
    )
    assert client.get("/api/download-settings").json() == previous
    saved = client.post("/api/download-settings", json={"retries": 7})
    assert saved.status_code == 200 and saved.json()["options"]["retries"] == 7
    for route in ["/api/download-settings", "/api/download-settings/reset"]:
        assert (
            client.post(
                route, json={}, headers={"Origin": "https://evil.test"}
            ).status_code
            == 403
        )
        assert (
            client.post(route, json={}, headers={"X-Local-App": ""}).status_code == 403
        )
    monkeypatch.setattr(
        "youtube_download.download_settings.os.replace",
        Mock(side_effect=PermissionError),
    )
    failure = client.post("/api/download-settings", json={"retries": 5})
    assert failure.status_code == 500 and "permissions" in failure.json()["detail"]
    assert client.get("/api/download-settings").json() == saved.json()
    assert client.post("/api/download-settings/reset").json() == previous
