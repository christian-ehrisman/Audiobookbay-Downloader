import pytest

from abb import audiobookbay
from abb.audiobookbay import audiobookbay_indexer_ids, search_audiobook, search_prowlarr

INDEXERS = [
    {"id": 1, "name": "1337x", "definitionName": "1337x", "enable": True},
    {"id": 7, "name": "ABB", "definitionName": "audiobookbay", "enable": True},
    {"id": 9, "name": "AudioBook Bay (old)", "definitionName": "audiobookbay", "enable": False},
]

RELEASES = [
    {"guid": "https://abb/x", "title": "Some Book - Author", "size": 123, "indexer": "ABB",
     "downloadUrl": "http://prowlarr:9696/7/download?link=abc", "magnetUrl": "magnet:?xt=urn:btih:AAA",
     "infoUrl": "https://abb/x", "seeders": 5, "leechers": 1, "protocol": "torrent", "infoHash": "aaa"},
    {"guid": "usenet-1", "title": "NZB thing", "protocol": "usenet"},
]


class Resp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.text = payload, status, str(payload)

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


@pytest.fixture
def prowlarr(monkeypatch):
    config = {"search_provider": "prowlarr", "prowlarr_url": "http://prowlarr:9696/",
              "prowlarr_api_key": "k", "prowlarr_indexer_ids": "", "prowlarr_categories": "3030"}
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append((url, params, headers))
        if url.endswith("/api/v1/indexer"):
            return Resp(INDEXERS)
        return Resp(RELEASES)

    monkeypatch.setattr(audiobookbay, "get_effective_config", lambda k: config.get(k))
    monkeypatch.setattr(audiobookbay.requests, "get", fake_get)
    return config, calls


def test_audiobookbay_indexer_detection_skips_disabled():
    assert audiobookbay_indexer_ids(INDEXERS) == [7]


def test_search_audiobook_routes_to_prowlarr_and_maps_fields(prowlarr):
    config, calls = prowlarr
    results = search_audiobook("some book")

    search_url, params, headers = calls[-1]
    assert search_url == "http://prowlarr:9696/api/v1/search"
    assert headers == {"X-Api-Key": "k"}
    assert ("indexerIds", "7") in params and ("categories", "3030") in params
    assert ("query", "some book") in params
    assert len(results) == 1  # usenet result dropped
    r = results[0]
    assert r["Title"] == "Some Book - Author"
    assert r["MagnetUri"] == "magnet:?xt=urn:btih:AAA"
    assert r["Link"].startswith("http://prowlarr:9696/7/download")
    assert r["Size"] == 123 and r["Guid"] == "https://abb/x"


def test_explicit_indexer_ids_and_blank_categories(prowlarr):
    config, calls = prowlarr
    config.update(prowlarr_indexer_ids="3, 4", prowlarr_categories="")
    search_prowlarr("x")
    assert all(not u.endswith("/indexer") for u, _, _ in calls)  # no auto-detect call
    params = calls[-1][1]
    assert [v for k, v in params if k == "indexerIds"] == ["3", "4"]
    assert not any(k == "categories" for k, _ in params)


def test_missing_prowlarr_config_raises(prowlarr):
    config, _ = prowlarr
    config["prowlarr_api_key"] = ""
    with pytest.raises(ValueError):
        search_prowlarr("x")
