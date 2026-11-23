import pytest
import requests

from abb import audiobookbay
from abb.audiobookbay import get_jackett_magnet
from abb.models import AddTorrentError

PROWLARR_LINK = "http://tiny:9696/13/download?apikey=SECRET&link=abc&file=Book"


class Raw:
    def __init__(self, body):
        self.body = body

    def read(self, n, decode_content=True):
        return self.body[:n]


class Resp:
    def __init__(self, status, headers=None, body=b""):
        self.status_code, self.headers, self.raw = status, headers or {}, Raw(body)
        self.closed = False

    def close(self):
        self.closed = True


def patch_get(monkeypatch, result):
    seen = {}

    def fake_get(url, **kwargs):
        seen.update(kwargs, url=url)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(audiobookbay.requests, "get", fake_get)
    return seen


def test_magnet_passes_through_without_request(monkeypatch):
    seen = patch_get(monkeypatch, AssertionError("should not be called"))
    assert get_jackett_magnet("magnet:?xt=urn:btih:abc") == "magnet:?xt=urn:btih:abc"
    assert seen == {}


def test_redirect_to_magnet_is_followed_with_timeout(monkeypatch):
    seen = patch_get(monkeypatch, Resp(301, {"Location": "magnet:?xt=urn:btih:abc"}))
    assert get_jackett_magnet(PROWLARR_LINK) == "magnet:?xt=urn:btih:abc"
    assert seen["timeout"] and seen["allow_redirects"] is False


def test_indexer_error_is_reported_with_body(monkeypatch):
    patch_get(monkeypatch, Resp(500, body=b'{"message":"Unable to find infohash on details page"}'))
    with pytest.raises(AddTorrentError, match="HTTP 500.*infohash"):
        get_jackett_magnet(PROWLARR_LINK)


def test_timeout_is_reported_instead_of_hanging(monkeypatch):
    patch_get(monkeypatch, requests.exceptions.ReadTimeout())
    with pytest.raises(AddTorrentError, match="Timed out"):
        get_jackett_magnet(PROWLARR_LINK)


def test_torrent_file_response_returns_original_url(monkeypatch):
    patch_get(monkeypatch, Resp(200, {"Content-Type": "application/x-bittorrent"}, b"d8:announce"))
    assert get_jackett_magnet(PROWLARR_LINK) == PROWLARR_LINK
