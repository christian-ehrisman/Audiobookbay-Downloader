import email
import io
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests

from abb import overcast
from abb.overcast import (
    OvercastClient, OvercastError, SourceFile, UploadLedger, _MultipartStream,
    auto_upload, local_source_files, parse_upload_form, upload_filename, upload_torrent,
)
from abb.premiumize import PremiumizeClient, PremiumizeTransferStore

UPLOADS_HTML = """
<html><body>
<form id="upload_form" action="https://uploads-bucket.s3.amazonaws.com/" method="post"
      enctype="multipart/form-data" data-key-prefix="uploads/u123/">
  <input type="hidden" name="key" value="uploads/u123/${filename}">
  <input type="hidden" name="AWSAccessKeyId" value="AKIA">
  <input type="hidden" name="acl" value="private">
  <input type="hidden" name="policy" value="cG9saWN5">
  <input type="hidden" name="signature" value="c2ln">
  <input type="hidden" name="Content-Type" value="audio/mpeg">
  <input type="file" name="file" accept=".mp3,.m4a, .m4b">
</form>
<form id="other"><input type="hidden" name="ignored" value="x"></form>
</body></html>
"""


def parse_multipart(body: bytes, content_type: str):
    msg = email.message_from_bytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    parts = []
    for part in msg.get_payload():
        name = part.get_param("name", header="content-disposition")
        parts.append((name, part.get_filename(), part.get_payload(decode=True)))
    return parts


# ------------------------------------------------------------------ form + body


def test_parse_upload_form_reads_action_prefix_fields_and_accept():
    form = parse_upload_form(UPLOADS_HTML)
    assert form.action == "https://uploads-bucket.s3.amazonaws.com/"
    assert form.key_prefix == "uploads/u123/"
    assert form.fields["key"] == "uploads/u123/${filename}"
    assert form.fields["policy"] == "cG9saWN5"
    assert "ignored" not in form.fields
    assert form.accept == [".mp3", ".m4a", ".m4b"]


def test_parse_upload_form_returns_none_without_form():
    assert parse_upload_form("<html>Log in</html>") is None


def test_multipart_stream_is_valid_and_puts_file_last():
    payload = b"ID3" + bytes(range(256)) * 50
    body = _MultipartStream({"key": "k/${filename}", "policy": "p"}, "Book - 01.mp3",
                            io.BytesIO(payload), len(payload), "audio/mpeg")
    data = b"".join(iter(body))
    assert len(data) == len(body)
    parts = parse_multipart(data, body.content_type)
    assert [p[0] for p in parts] == ["key", "policy", "file"]
    assert parts[-1][1] == "Book - 01.mp3"
    assert parts[-1][2] == payload


def test_multipart_stream_errors_if_source_is_short():
    body = _MultipartStream({}, "a.mp3", io.BytesIO(b"abc"), 10, "audio/mpeg")
    with pytest.raises(OvercastError):
        body.read()


def test_requests_streams_body_with_content_length():
    """S3 POST rejects chunked encoding: check requests sends Content-Length."""
    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received["length"] = self.headers.get("Content-Length")
            received["chunked"] = self.headers.get("Transfer-Encoding")
            received["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            received["ctype"] = self.headers["Content-Type"]
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    payload = b"x" * 300_000
    body = _MultipartStream({"key": "k"}, "f.mp3", io.BytesIO(payload), len(payload), "audio/mpeg")
    response = requests.post(f"http://127.0.0.1:{server.server_port}/", data=body,
                             headers={"Content-Type": body.content_type})
    thread.join(5)
    server.server_close()

    assert response.status_code == 204
    assert received["chunked"] is None
    assert int(received["length"]) == len(body)
    assert parse_multipart(received["body"], received["ctype"])[-1][2] == payload


# ----------------------------------------------------------------- client flow


class FakeResp:
    def __init__(self, status_code=200, text=""):
        self.status_code, self.text = status_code, text


class FakeOvercastSession:
    def __init__(self, uploads_html=UPLOADS_HTML, s3_status=204):
        self.headers = {}
        self.calls = []
        self.uploads_html = uploads_html
        self.s3_status = s3_status
        self.s3_bodies = []

    def post(self, url, data=None, headers=None, **kwargs):
        if isinstance(data, _MultipartStream):
            raw = b"".join(iter(data))
            self.s3_bodies.append(parse_multipart(raw, headers["Content-Type"]))
            self.calls.append(("POST", url, None))
            return FakeResp(self.s3_status)
        self.calls.append(("POST", url, data))
        return FakeResp(200)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, None))
        return FakeResp(200, self.uploads_html)


def test_upload_logs_in_posts_to_s3_and_confirms_key():
    session = FakeOvercastSession()
    client = OvercastClient("me@example.com", "pw", session=session)

    key = client.upload("Book - 01.mp3", io.BytesIO(b"audio"), 5)

    assert key == "uploads/u123/Book - 01.mp3"
    login = session.calls[0]
    assert login[1] == "https://overcast.fm/login" and login[2]["email"] == "me@example.com"
    fields = {name: value for name, _, value in session.s3_bodies[0]}
    assert fields["key"] == b"uploads/u123/${filename}"
    assert fields["signature"] == b"c2ln"
    assert fields["file"] == b"audio"
    assert session.calls[-1] == ("POST", "https://overcast.fm/podcasts/upload_succeeded",
                                 {"key": "uploads/u123/Book - 01.mp3"})


def test_upload_raises_when_form_missing():
    client = OvercastClient("me@example.com", "bad", session=FakeOvercastSession(uploads_html="<html>login</html>"))
    with pytest.raises(OvercastError, match="Premium"):
        client.upload("a.mp3", io.BytesIO(b"a"), 1)


def test_upload_raises_on_s3_rejection():
    client = OvercastClient("me@example.com", "pw", session=FakeOvercastSession(s3_status=403))
    with pytest.raises(OvercastError, match="S3 upload failed"):
        client.upload("a.mp3", io.BytesIO(b"a"), 1)


def test_missing_credentials_raise():
    with pytest.raises(OvercastError, match="not configured"):
        OvercastClient("", "", session=FakeOvercastSession()).login()


# --------------------------------------------------------------------- sources


def test_local_source_files_resolves_paths_and_blocks_traversal(tmp_path):
    (tmp_path / "Book").mkdir()
    (tmp_path / "Book" / "01.mp3").write_bytes(b"12345")
    torrent = {"files": [{"name": "Book/01.mp3"}, {"name": "../etc/passwd"}]}

    files = local_source_files(torrent, str(tmp_path))

    assert [(f.path, f.size) for f in files] == [("Book/01.mp3", 5)]
    stream, close = files[0].open()
    assert stream.read() == b"12345"
    close()


def test_local_source_files_reports_missing_mount(tmp_path):
    with pytest.raises(OvercastError, match="mounted"):
        local_source_files({"files": [{"name": "Book/01.mp3"}]}, str(tmp_path))


def test_upload_filename_prefixes_multi_file_books():
    f = SourceFile("Book/01.mp3", "01.mp3", 1, None)
    assert upload_filename("My Book", f, 12) == "My Book - 01.mp3"
    single = SourceFile("My Book.m4b", "My Book.m4b", 1, None)
    assert upload_filename("My Book", single, 1) == "My Book.m4b"
    assert "/" not in upload_filename("A/B", f, 2)


# --------------------------------------------------------------- orchestration


class FakeOvercast:
    def __init__(self, fail_on=None):
        self.uploaded = []
        self.fail_on = fail_on

    def upload(self, filename, stream, size):
        if filename == self.fail_on:
            raise OvercastError("boom")
        self.uploaded.append((filename, stream.read()))


class FakeTorrentClient:
    def __init__(self):
        self.labels = []

    def add_label_to_torrent(self, torrent_id, user, label):
        self.labels.append((torrent_id, label))
        return True


@pytest.fixture
def book(tmp_path, monkeypatch):
    root = tmp_path / "downloads"
    (root / "Book").mkdir(parents=True)
    for name in ("01.mp3", "02.mp3", "cover.jpg"):
        (root / "Book" / name).write_bytes(name.encode())
    config = {"overcast_input_path": str(root), "overcast_extensions": "mp3,m4b",
              "overcast_enabled": True, "overcast_auto_upload": True, "overcast_label": "overcast"}
    monkeypatch.setattr(overcast, "get_effective_config", lambda k: config.get(k))
    torrent = {"id": "7", "hash_string": "abc", "name": "Book", "labels": ["audiobook"],
               "percent_done": 100, "status": "Seeding",
               "files": [{"name": "Book/01.mp3"}, {"name": "Book/02.mp3"}, {"name": "Book/cover.jpg"}]}
    return torrent, UploadLedger(str(tmp_path / "overcast.json"))


def test_upload_torrent_uploads_audio_records_and_labels(book):
    torrent, ledger = book
    oc, client = FakeOvercast(), FakeTorrentClient()

    result = upload_torrent(torrent, client, overcast=oc, ledger=ledger)

    assert result["state"] == "done"
    assert oc.uploaded == [("Book - 01.mp3", b"01.mp3"), ("Book - 02.mp3", b"02.mp3")]
    assert ledger.get("abc")["uploaded"] == ["Book/01.mp3", "Book/02.mp3"]
    assert client.labels == [("7", "overcast")]


def test_upload_torrent_resumes_after_failure(book):
    torrent, ledger = book
    result = upload_torrent(torrent, FakeTorrentClient(), overcast=FakeOvercast(fail_on="Book - 02.mp3"), ledger=ledger)
    assert result["state"] == "error" and result["message"] == "boom"
    assert ledger.get("abc")["uploaded"] == ["Book/01.mp3"]

    retry = FakeOvercast()
    assert upload_torrent(torrent, FakeTorrentClient(), overcast=retry, ledger=ledger)["state"] == "done"
    assert [name for name, _ in retry.uploaded] == ["Book - 02.mp3"]


def test_auto_upload_skips_done_error_and_unfinished(book, monkeypatch):
    torrent, ledger = book
    monkeypatch.setattr(overcast, "get_ledger", lambda: ledger)
    seen = []
    monkeypatch.setattr(overcast, "upload_torrent", lambda t, c: seen.append(t["hash_string"]))
    ledger.update("done1", state="done")
    ledger.update("err1", state="error")
    ledger.update("stuck", state="uploading")  # interrupted by a restart
    torrents = [
        dict(torrent, hash_string="done1"),
        dict(torrent, hash_string="err1"),
        dict(torrent, hash_string="stuck"),
        dict(torrent, hash_string="partial", percent_done=50, status="Downloading"),
        dict(torrent, hash_string="nolabel", labels=[]),
        dict(torrent, hash_string="new"),
    ]

    assert auto_upload(torrents, FakeTorrentClient()) == 2
    assert seen == ["stuck", "new"]


# ------------------------------------------------------------------ premiumize


class FakePmSession:
    def __init__(self, folders):
        self.folders = folders
        self.headers = {}

    def request(self, method, url, timeout=None, params=None, **kwargs):
        folder = (params or {}).get("id", "root")

        class R:
            status_code = 200
            text = ""

            def json(_):
                return {"status": "success", "content": self.folders.get(folder, [])}
        return R()


def test_premiumize_lists_cloud_files_for_folder_and_single_file(tmp_path):
    folders = {
        "book": [
            {"id": "f1", "name": "01.mp3", "type": "file", "size": 10, "link": "https://dl/1"},
            {"id": "cd2", "name": "CD2", "type": "folder"},
        ],
        "cd2": [{"id": "f2", "name": "02.mp3", "type": "file", "size": 20, "link": "https://dl/2"}],
        "parent": [
            {"id": "other", "name": "x.mp3", "type": "file", "size": 1, "link": "https://dl/x"},
            {"id": "single", "name": "Book.m4b", "type": "file", "size": 99, "link": "https://dl/b"},
        ],
    }
    client = PremiumizeClient(api_key="k", store=PremiumizeTransferStore(str(tmp_path / "p.json")))
    client.session = FakePmSession(folders)

    files = client.list_cloud_files({"folder_id": "book", "file_id": None})
    assert [(f["path"], f["link"]) for f in files] == [("01.mp3", "https://dl/1"), ("CD2/02.mp3", "https://dl/2")]

    single = client.list_cloud_files({"folder_id": "parent", "file_id": "single"})
    assert single == [{"path": "Book.m4b", "name": "Book.m4b", "size": 99, "link": "https://dl/b"}]
