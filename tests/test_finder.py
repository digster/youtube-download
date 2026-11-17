import httpx
import pytest

from conftest import PID, VID
from youtube_download.finder import Finder, parse_response


def response():
    return {
        "status": "ok",
        "keys": [
            {
                "name": "Internet Archive",
                "available": [
                    {
                        "url": "https://web.archive.org/example",
                        "title": "Saved page",
                        "contains": {"metadata": True},
                    },
                    {"url": "javascript:alert(1)", "title": "Unsafe"},
                    {"url": "https://web.archive.org/example", "title": "Duplicate"},
                ],
            }
        ],
        "verdict": {"human_friendly": "Archived with metadata only."},
    }


def test_v5_links_are_normalized_and_unsafe_urls_removed():
    result = parse_response(response())
    assert len(result["links"]) == 1
    assert result["links"][0]["kind"] == "metadata"


def test_lookup_is_cached_and_errors_are_retryable(store):
    calls = []

    def handle(request):
        calls.append(request.url)
        return httpx.Response(200, json=response())

    finder = Finder(
        store,
        "https://finder.test",
        httpx.Client(transport=httpx.MockTransport(handle)),
    )
    finder.lookup(PID, VID)
    finder.lookup(PID, VID)
    assert len(calls) == 1
    assert str(calls[0]).endswith(f"/api/v5/{VID}")


def test_finder_failure_keeps_previous_links(store):
    ok = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=response())
        )
    )
    finder = Finder(store, "https://finder.test", ok)
    finder.lookup(PID, VID)
    finder.client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    )
    result = finder.lookup(PID, VID, force=True)
    assert result["error"] and len(result["links"]) == 1


@pytest.mark.parametrize(
    "payload", [{}, {"status": "bad.id"}, {"status": "ok", "keys": None}]
)
def test_malformed_finder_response_is_rejected(payload):
    with pytest.raises(ValueError):
        parse_response(payload)
