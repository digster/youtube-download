from unittest.mock import Mock

from youtube_download.account import Account


def test_account_normalizes_owned_and_liked_playlists(tmp_path, monkeypatch):
    api = Mock()
    api.list_playlists.return_value = [
        {
            "id": "PLowned",
            "snippet": {"title": "Owned playlist", "channelTitle": "My channel"},
            "contentDetails": {"itemCount": 27},
            "status": {"privacyStatus": "private"},
        }
    ]
    api.get_channel_info.return_value = {
        "contentDetails": {"relatedPlaylists": {"likes": "LL"}}
    }
    account = Account(tmp_path)
    monkeypatch.setattr(account, "_api", lambda: api)
    playlists = account.playlists()
    assert [p["id"] for p in playlists] == ["PLowned", "LL"]
    assert playlists[0]["item_count"] == 27
    assert playlists[0]["privacy"] == "private"
    api._service.close.assert_called_once()


def test_account_preserves_unavailable_ids_and_skips_invalid_ids(tmp_path, monkeypatch):
    api = Mock()
    api.list_playlist_items.return_value = [
        {
            "snippet": {"title": "Deleted video", "position": 4},
            "contentDetails": {"videoId": "dQw4w9WgXcQ"},
        },
        {
            "snippet": {"title": "Invalid"},
            "contentDetails": {"videoId": "../../etc/passwd"},
        },
    ]
    account = Account(tmp_path)
    monkeypatch.setattr(account, "_api", lambda: api)
    videos = account.videos("PLowned")
    assert len(videos) == 1
    assert videos[0]["availability"] == "unavailable"
    assert videos[0]["position"] == 4


def test_account_closes_service_after_partial_page_failure(tmp_path, monkeypatch):
    import pytest

    api = Mock()

    def broken_page():
        yield {"id": "PLowned", "snippet": {"title": "First page"}}
        raise RuntimeError("Second page failed")

    api.list_playlists.return_value = broken_page()
    account = Account(tmp_path)
    monkeypatch.setattr(account, "_api", lambda: api)
    with pytest.raises(RuntimeError):
        account.playlists()
    api._service.close.assert_called_once()
