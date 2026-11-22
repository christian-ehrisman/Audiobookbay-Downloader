import pytest

from abb.models import TorrentClientType, User
from abb.premiumize import PremiumizeClient, PremiumizeTransferStore
from abb.torrent import create_torrent_client

MAGNET = "magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01&dn=Some+Audiobook"
ADMIN = User(username="admin", role="admin", id="admin")
ALICE = User(username="alice", role="user", id="alice")
BOB = User(username="bob", role="user", id="bob")


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    """Routes (method, endpoint) to canned Premiumize responses and records calls."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.headers = {}

    def request(self, method, url, timeout=None, **kwargs):
        endpoint = url.split("/api", 1)[1]
        self.calls.append((method, endpoint, kwargs))
        payload = self.routes.get((method, endpoint), {"status": "error", "message": "unrouted"})
        return FakeResponse(payload)


@pytest.fixture
def store(tmp_path):
    return PremiumizeTransferStore(str(tmp_path / "premiumize.json"))


def make_client(store, routes, folder_id=""):
    client = PremiumizeClient(api_key="key", folder_id=folder_id, store=store)
    client.session = FakeSession(routes)
    return client


def test_factory_creates_premiumize_client_with_bearer_auth(store):
    client = create_torrent_client(TorrentClientType.premiumize, api_key="secret", store=store)
    assert isinstance(client, PremiumizeClient)
    assert client.session.headers["Authorization"] == "Bearer secret"


def test_add_magnet_creates_transfer_and_tracks_owner(store):
    client = make_client(store, {
        ("POST", "/transfer/create"): {"status": "success", "id": "t1", "name": "Some Audiobook"},
    }, folder_id="f-target")

    assert client.add_torrent(MAGNET, ALICE, label="audiobook") is True

    method, endpoint, kwargs = client.session.calls[0]
    assert (method, endpoint) == ("POST", "/transfer/create")
    assert kwargs["data"] == {"src": MAGNET, "folder_id": "f-target"}
    entry = store.get("t1")
    assert entry["user_id"] == "alice"
    assert entry["hash_string"] == "abcdef0123456789abcdef0123456789abcdef01"
    assert set(entry["labels"]) == {"audiobook", "alice", "username:alice"}


def test_add_returns_false_on_api_error(store):
    client = make_client(store, {
        ("POST", "/transfer/create"): {"status": "error", "message": "not premium"},
    })
    assert client.add_torrent(MAGNET, ALICE) is False
    assert store.all() == {}


def test_get_torrents_filters_untracked_and_other_users(store):
    store.add("t1", ALICE, ["audiobook", "alice"], hash_string="h1")
    store.add("t2", BOB, ["audiobook", "bob"], hash_string="h2")
    client = make_client(store, {
        ("GET", "/transfer/list"): {"status": "success", "transfers": [
            {"id": "t1", "name": "Book.One", "status": "running", "progress": 0.42, "created_at": 100},
            {"id": "t2", "name": "Book Two", "status": "finished", "progress": 1.0, "created_at": 200},
            {"id": "other", "name": "Not ours", "status": "finished", "progress": 1.0},
        ]},
    })

    alice_view = client.get_torrents(ALICE)
    assert [t["id"] for t in alice_view] == ["t1"]
    assert alice_view[0]["status"] == "Downloading"
    assert alice_view[0]["percent_done"] == pytest.approx(42.0)
    assert alice_view[0]["name"] == "Book One"
    assert alice_view[0]["hash_string"] == "h1"

    admin_view = client.get_torrents(ADMIN)
    assert [t["id"] for t in admin_view] == ["t2", "t1"]
    assert admin_view[0]["status"] == "Complete"
    assert admin_view[0]["added_by"] == "bob"


def test_delete_single_file_transfer_deletes_item_not_parent_folder(store):
    store.add("t1", ALICE, ["audiobook", "alice"])
    client = make_client(store, {
        ("GET", "/transfer/list"): {"status": "success", "transfers": [
            {"id": "t1", "status": "finished", "folder_id": "parent", "file_id": "file1"},
        ]},
        ("POST", "/item/delete"): {"status": "success"},
        ("POST", "/transfer/delete"): {"status": "success"},
    })

    assert client.delete_torrent("t1", ALICE, delete_data=True) is True
    endpoints = [(m, e, k.get("data")) for m, e, k in client.session.calls]
    assert ("POST", "/item/delete", {"id": "file1"}) in endpoints
    assert not any(e == "/folder/delete" for _, e, _ in endpoints)
    assert store.get("t1") is None


def test_delete_never_removes_configured_target_folder(store):
    store.add("t1", ADMIN, ["audiobook", "admin"])
    client = make_client(store, {
        ("GET", "/transfer/list"): {"status": "success", "transfers": [
            {"id": "t1", "status": "finished", "folder_id": "f-target", "file_id": None},
        ]},
        ("POST", "/transfer/delete"): {"status": "success"},
    }, folder_id="f-target")

    assert client.delete_torrent("t1", ADMIN, delete_data=True) is True
    assert not any(e == "/folder/delete" for _, e, _ in client.session.calls)


def test_delete_multi_file_transfer_deletes_created_folder(store):
    store.add("t1", ADMIN, ["audiobook", "admin"])
    client = make_client(store, {
        ("GET", "/transfer/list"): {"status": "success", "transfers": [
            {"id": "t1", "status": "finished", "folder_id": "created", "file_id": None},
        ]},
        ("POST", "/folder/delete"): {"status": "success"},
        ("POST", "/transfer/delete"): {"status": "success"},
    })

    assert client.delete_torrent("t1", ADMIN, delete_data=True) is True
    assert ("POST", "/folder/delete") in [(m, e) for m, e, _ in client.session.calls]


def test_user_cannot_delete_someone_elses_transfer(store):
    store.add("t1", BOB, ["audiobook", "bob"])
    client = make_client(store, {})
    assert client.delete_torrent("t1", ALICE) is False
    assert client.session.calls == []


def test_labels_are_kept_in_local_store(store):
    store.add("t1", ALICE, ["audiobook", "alice"])
    client = make_client(store, {})
    assert client.add_label_to_torrent("t1", ALICE, "beets") is True
    assert "beets" in store.get("t1")["labels"]
    assert client.remove_label_from_torrent("t1", ALICE, "beets") is True
    assert "beets" not in store.get("t1")["labels"]


def test_api_key_is_read_from_config_when_not_passed(store, monkeypatch):
    import abb.premiumize as pm
    config = {"premiumize_api_key": ""}
    monkeypatch.setattr(pm, "get_effective_config", lambda k: config.get(k))
    client = PremiumizeClient(store=store)
    client.session = FakeSession({("GET", "/transfer/list"): {"status": "success", "transfers": []}})

    assert client.get_torrents(ADMIN) == []
    assert client.session.calls == []  # no key yet: no request sent

    config["premiumize_api_key"] = "entered-in-settings"
    client.get_torrents(ADMIN)
    assert client.session.headers["Authorization"] == "Bearer entered-in-settings"
