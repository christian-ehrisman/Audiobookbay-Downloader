import requests
from typing import Any, Dict, List, Optional
from .config_db import get_effective_config
from .constants import JACKETT_API_KEY, JACKETT_API_URL
from .utils import custom_logger

logger = custom_logger(__name__)

def get_jackett_magnet(url):
    try:
        if url.startswith("magnet:"):
            return url
        response = requests.get(url, allow_redirects=False)
        if response.status_code in [301, 302]:
            return response.headers.get("Location", url)
    except Exception as e:
        logger.error(f"Error fetching magnet URL: {e}")
        return url
    return url

def search_audiobook(query, api_url: Optional[str] = None, api_key: Optional[str] = None, provider: Optional[str] = None):
    """Search for audiobooks. Results use Jackett's field names (Title, Link,
    MagnetUri, Size, ...) whichever provider is configured."""
    provider = provider or get_effective_config("search_provider") or "jackett"
    if provider == "prowlarr":
        return search_prowlarr(query)
    url = api_url or JACKETT_API_URL
    key = api_key or JACKETT_API_KEY
    
    params = {
        "apikey": key,
        "Query": query,
        "Category": "audiobooks"
    }
    logger.info(f"Searching for {query}...")

    try:
        response = requests.get(str(url), params=params)
        if response.status_code != 200:
            logger.error("Error fetching results from Jackett:", response.text)
            return []

        results = response.json()
        logger.info(f"Found {len(results.get('Results', []))} results for {query}")
        return results.get("Results", [])
    except Exception as e:
        logger.error(f"Search failed: {e}")
        return []


# ------------------------------------------------------------------ Prowlarr


def _prowlarr_settings(url: Optional[str] = None, api_key: Optional[str] = None):
    url = (url or get_effective_config("prowlarr_url") or "").rstrip("/")
    api_key = api_key or get_effective_config("prowlarr_api_key")
    if not url or not api_key:
        raise ValueError("Prowlarr URL or API key not configured")
    return url, {"X-Api-Key": api_key}


def _csv_ints(value) -> List[int]:
    return [int(v) for v in str(value or "").replace(" ", "").split(",") if v.isdigit()]


def prowlarr_indexers(url: Optional[str] = None, api_key: Optional[str] = None) -> List[Dict[str, Any]]:
    url, headers = _prowlarr_settings(url, api_key)
    response = requests.get(f"{url}/api/v1/indexer", headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()


def audiobookbay_indexer_ids(indexers: List[Dict[str, Any]]) -> List[int]:
    """Enabled indexers that look like AudiobookBay (by name or definition)."""
    ids = []
    for ix in indexers:
        haystack = f"{ix.get('name', '')} {ix.get('definitionName', '')}".lower().replace(" ", "")
        if ix.get("enable", True) and "audiobookbay" in haystack:
            ids.append(ix["id"])
    return ids


def _to_jackett_shape(release: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "Title": release.get("title"),
        "Link": release.get("downloadUrl"),
        "MagnetUri": release.get("magnetUrl"),
        "Size": release.get("size"),
        "Guid": release.get("guid") or release.get("infoUrl"),
        "Details": release.get("infoUrl"),
        "Poster": release.get("posterUrl"),
        "Seeders": release.get("seeders"),
        "Peers": release.get("leechers"),
        "PublishDate": release.get("publishDate"),
        "Tracker": release.get("indexer"),
        "InfoHash": release.get("infoHash"),
    }


def search_prowlarr(query: str, url: Optional[str] = None, api_key: Optional[str] = None) -> List[Dict[str, Any]]:
    url, headers = _prowlarr_settings(url, api_key)
    indexer_ids = _csv_ints(get_effective_config("prowlarr_indexer_ids"))
    if not indexer_ids:
        try:
            indexer_ids = audiobookbay_indexer_ids(prowlarr_indexers(url, headers["X-Api-Key"]))
        except Exception as e:
            logger.warning(f"Could not list Prowlarr indexers, searching all: {e}")

    params: List = [("query", query), ("type", "search"), ("limit", "100")]
    params += [("indexerIds", str(i)) for i in indexer_ids]
    params += [("categories", str(c)) for c in _csv_ints(get_effective_config("prowlarr_categories"))]

    logger.info(f"Searching Prowlarr for {query} (indexers: {indexer_ids or 'all'})")
    try:
        response = requests.get(f"{url}/api/v1/search", params=params, headers=headers, timeout=60)
        if response.status_code != 200:
            logger.error(f"Prowlarr search failed: HTTP {response.status_code} {response.text[:200]}")
            return []
        releases = [r for r in response.json() if r.get("protocol", "torrent") == "torrent"]
        logger.info(f"Found {len(releases)} results for {query}")
        return [_to_jackett_shape(r) for r in releases]
    except Exception as e:
        logger.error(f"Prowlarr search failed: {e}")
        return []
