"""Upload finished audiobooks to Overcast (https://overcast.fm/uploads).

Overcast Premium lets you upload your own audio files. There is no public
API; the web flow (the same one used by pawelkami/overcast-uploader) is:

1. POST /login with email + password (session cookie)
2. GET /uploads and read the S3 POST form (#upload_form): its action URL,
   data-key-prefix and hidden fields (policy, signature, ...)
3. POST the file to S3 with those fields (file must be the last field)
4. POST /podcasts/upload_succeeded with the S3 key so Overcast picks it up

Files are streamed (local disk or a cloud link) into the S3 request without
being held in memory, so large .m4b files are fine. A TinyDB ledger tracks
which files of each torrent were uploaded, so retries skip finished files.
"""
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable, Dict, IO, List, Optional, Tuple

import requests
from tinydb import Query, TinyDB

from .config_db import get_effective_config
from .constants import ADMIN_USER_DICT, LABEL
from .utils import custom_logger

logger = custom_logger(__name__)

OVERCAST_URL = "https://overcast.fm"
USER_AGENT = "Mozilla/5.0 (compatible; audiobookbay-downloader)"


class OvercastError(Exception):
    pass


# --------------------------------------------------------------------------- form


@dataclass
class UploadForm:
    action: str
    key_prefix: str
    fields: Dict[str, str]
    accept: List[str] = field(default_factory=list)


class _UploadFormParser(HTMLParser):
    """Extract the S3 upload form from overcast.fm/uploads."""

    def __init__(self):
        super().__init__()
        self.in_form = False
        self.action = None
        self.key_prefix = ""
        self.fields: Dict[str, str] = {}
        self.accept: List[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and a.get("id") == "upload_form":
            self.in_form = True
            self.action = a.get("action")
            self.key_prefix = a.get("data-key-prefix") or ""
        elif tag == "input" and self.in_form:
            if (a.get("type") or "").lower() == "file":
                self.accept = [x.strip().lower() for x in (a.get("accept") or "").split(",") if x.strip()]
            elif a.get("name"):
                self.fields[a["name"]] = a.get("value") or ""

    def handle_endtag(self, tag):
        if tag == "form" and self.in_form:
            self.in_form = False


def parse_upload_form(html: str) -> Optional[UploadForm]:
    parser = _UploadFormParser()
    parser.feed(html)
    if not parser.action:
        return None
    return UploadForm(parser.action, parser.key_prefix, parser.fields, parser.accept)


# ------------------------------------------------------------- streaming upload


class _MultipartStream:
    """File-like multipart/form-data body that streams the file part.

    requests sends a body with __len__ and read() with a Content-Length
    header (S3 POST rejects chunked encoding) and reads it in blocks.
    """

    def __init__(self, fields: Dict[str, str], filename: str, stream: IO[bytes], size: int, content_type: str):
        self.boundary = uuid.uuid4().hex
        head = b""
        for name, value in fields.items():
            head += (
                f"--{self.boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()
        safe_name = filename.replace('"', "'")
        head += (
            f"--{self.boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
        tail = f"\r\n--{self.boundary}--\r\n".encode()
        self._parts = [_BytesReader(head), _LimitedReader(stream, size), _BytesReader(tail)]
        self._len = len(head) + size + len(tail)

    @property
    def content_type(self) -> str:
        return f"multipart/form-data; boundary={self.boundary}"

    def __len__(self):
        return self._len

    def __iter__(self):
        while True:
            chunk = self.read(64 * 1024)
            if not chunk:
                return
            yield chunk

    def read(self, size: int = -1) -> bytes:
        out = b""
        while self._parts and (size < 0 or len(out) < size):
            want = -1 if size < 0 else size - len(out)
            chunk = self._parts[0].read(want)
            if not chunk:
                self._parts.pop(0)
                continue
            out += chunk
        return out


class _BytesReader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def read(self, size=-1):
        end = len(self.data) if size < 0 else self.pos + size
        chunk = self.data[self.pos:end]
        self.pos += len(chunk)
        return chunk


class _LimitedReader:
    """Reads exactly `size` bytes from a stream; errors if it ends early."""

    def __init__(self, stream, size: int):
        self.stream, self.remaining = stream, size

    def read(self, size=-1):
        if self.remaining <= 0:
            return b""
        want = self.remaining if size < 0 else min(size, self.remaining)
        chunk = self.stream.read(want)
        if not chunk:
            raise OvercastError(f"Source ended with {self.remaining} bytes left to send")
        self.remaining -= len(chunk)
        return chunk


CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".m4b": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".flac": "audio/flac",
}


class OvercastClient:
    def __init__(self, email: str, password: str, base_url: str = OVERCAST_URL, session: Optional[requests.Session] = None):
        self.email = email
        self.password = password
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self._logged_in = False

    def login(self) -> None:
        if not self.email or not self.password:
            raise OvercastError("Overcast email/password not configured")
        response = self.session.post(
            f"{self.base_url}/login",
            data={"email": self.email, "password": self.password, "then": "uploads"},
            timeout=30,
        )
        if response.status_code >= 400:
            raise OvercastError(f"Overcast login failed (HTTP {response.status_code})")
        self._logged_in = True

    def get_upload_form(self) -> UploadForm:
        if not self._logged_in:
            self.login()
        response = self.session.get(f"{self.base_url}/uploads", timeout=30)
        form = parse_upload_form(response.text) if response.status_code == 200 else None
        if form is None:
            raise OvercastError(
                "Overcast upload form not found: check the email/password and that the account has Overcast Premium"
            )
        return form

    def upload(self, filename: str, stream: IO[bytes], size: int) -> str:
        """Upload one file. Returns the S3 key Overcast registered."""
        form = self.get_upload_form()  # fresh policy/signature for each file
        fields = dict(form.fields)
        key_template = fields.get("key") or (form.key_prefix + "${filename}")
        if "${filename}" in key_template:
            key = key_template.replace("${filename}", filename)
        else:
            key = key_template
        fields["key"] = key_template

        ext = os.path.splitext(filename)[1].lower()
        file_type = CONTENT_TYPES.get(ext, "application/octet-stream")
        if "Content-Type" in fields and not fields["Content-Type"]:
            fields["Content-Type"] = file_type

        body = _MultipartStream(fields, filename, stream, size, file_type)
        response = self.session.post(
            form.action,
            data=body,
            headers={"Content-Type": body.content_type},
            allow_redirects=False,
            timeout=(30, 600),
        )
        if response.status_code not in (200, 201, 204, 303):
            raise OvercastError(f"S3 upload failed (HTTP {response.status_code}): {response.text[:300]}")

        done = self.session.post(f"{self.base_url}/podcasts/upload_succeeded", data={"key": key}, timeout=30)
        if done.status_code >= 400:
            raise OvercastError(f"Overcast did not accept the upload (HTTP {done.status_code})")
        logger.info(f"Uploaded {filename} ({size} bytes) to Overcast")
        return key


# --------------------------------------------------------------------- sources


@dataclass
class SourceFile:
    path: str                                  # stable id within the torrent
    name: str                                  # base file name
    size: int
    open: Callable[[], Tuple[IO[bytes], Callable[[], None]]]  # -> (stream, close)


def _allowed_extensions() -> List[str]:
    raw = get_effective_config("overcast_extensions") or "mp3,m4a,m4b"
    return ["." + e.strip().lower().lstrip(".") for e in raw.split(",") if e.strip()]


def local_source_files(torrent: Dict[str, Any], base_path: str) -> List[SourceFile]:
    base = os.path.realpath(base_path)
    result = []
    for f in torrent.get("files") or []:
        rel = f.get("name") or ""
        full = os.path.realpath(os.path.join(base, rel))
        if not full.startswith(base + os.sep):
            logger.warning(f"Skipping {rel}: outside {base}")
            continue
        if not os.path.isfile(full):
            raise OvercastError(f"File not found: {full} (is the downloads folder mounted at {base}?)")

        def opener(p=full):
            fh = open(p, "rb")
            return fh, fh.close

        result.append(SourceFile(rel, os.path.basename(rel), os.path.getsize(full), opener))
    return result


def remote_source_files(entries: List[Dict[str, Any]]) -> List[SourceFile]:
    """entries: [{"path", "name", "size", "link"}] (e.g. from Premiumize)."""
    result = []
    for e in entries:
        def opener(link=e["link"]):
            resp = requests.get(link, stream=True, timeout=(30, 600))
            resp.raise_for_status()
            resp.raw.decode_content = True
            return resp.raw, resp.close

        result.append(SourceFile(e["path"], e["name"], int(e.get("size") or 0), opener))
    return result


def source_files_for(torrent: Dict[str, Any], client) -> List[SourceFile]:
    if hasattr(client, "list_cloud_files"):
        files = remote_source_files(client.list_cloud_files(torrent))
    else:
        base = get_effective_config("overcast_input_path") or get_effective_config("beets_input_path")
        if not torrent.get("files"):
            raise OvercastError("This torrent client does not report file paths, so it cannot upload to Overcast")
        files = local_source_files(torrent, base)
    exts = _allowed_extensions()
    files = [f for f in files if os.path.splitext(f.name)[1].lower() in exts]
    files.sort(key=lambda f: f.path)
    return files


def upload_filename(book_name: str, file: SourceFile, file_count: int) -> str:
    """Name for Overcast. Prefix multi-file books so `01.mp3` from different
    books don't overwrite each other in the shared upload prefix."""
    base = file.name
    if file_count > 1 or not base.lower().startswith(book_name.lower()[:10]):
        base = f"{book_name} - {base}"
    base = re.sub(r"[^\w .,()\-\[\]&']+", "_", base).strip()
    return base[:200]


# ---------------------------------------------------------------------- ledger


class UploadLedger:
    def __init__(self, db_path: Optional[str] = None):
        path = db_path or os.path.join(os.getenv("DB_PATH", "/tmp"), "overcast.json")
        self._db = TinyDB(path)
        self._lock = threading.Lock()

    def get(self, key: str) -> Dict[str, Any]:
        with self._lock:
            return self._db.get(Query().key == key) or {}

    def all(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {d["key"]: d for d in self._db.all()}

    def update(self, key: str, **values) -> Dict[str, Any]:
        with self._lock:
            doc = self._db.get(Query().key == key) or {"key": key, "uploaded": []}
            doc.update(values, updated=int(time.time()))
            self._db.upsert(doc, Query().key == key)
            return doc

    def mark_file(self, key: str, path: str) -> None:
        with self._lock:
            doc = self._db.get(Query().key == key) or {"key": key, "uploaded": []}
            if path not in doc["uploaded"]:
                doc["uploaded"].append(path)
            doc["updated"] = int(time.time())
            self._db.upsert(doc, Query().key == key)


_ledger: Optional[UploadLedger] = None
_in_progress: set = set()
_in_progress_lock = threading.Lock()


def get_ledger() -> UploadLedger:
    global _ledger
    if _ledger is None:
        _ledger = UploadLedger()
    return _ledger


def torrent_key(torrent: Dict[str, Any]) -> str:
    return str(torrent.get("hash_string") or torrent.get("id"))


def is_complete(torrent: Dict[str, Any]) -> bool:
    return (torrent.get("percent_done") or 0) >= 100 and torrent.get("status") not in ("Error", "Downloading")


def is_enabled() -> bool:
    return bool(get_effective_config("overcast_enabled"))


def make_client() -> OvercastClient:
    return OvercastClient(get_effective_config("overcast_email"), get_effective_config("overcast_password"))


# ---------------------------------------------------------------- orchestration


def upload_torrent(torrent: Dict[str, Any], torrent_client, overcast: Optional[OvercastClient] = None,
                   ledger: Optional[UploadLedger] = None) -> Dict[str, Any]:
    """Upload every eligible file of a finished torrent. Safe to re-run."""
    ledger = ledger or get_ledger()
    key = torrent_key(torrent)
    with _in_progress_lock:
        if key in _in_progress:
            return ledger.get(key)
        _in_progress.add(key)

    try:
        files = source_files_for(torrent, torrent_client)
        if not files:
            raise OvercastError(f"No files with extensions {', '.join(_allowed_extensions())} found")

        done = set(ledger.get(key).get("uploaded", []))
        ledger.update(key, state="uploading", total=len(files), message="", name=torrent.get("name"))
        overcast = overcast or make_client()
        for f in files:
            if f.path in done:
                continue
            stream, close = f.open()
            try:
                overcast.upload(upload_filename(torrent.get("name", "Audiobook"), f, len(files)), stream, f.size)
            finally:
                close()
            ledger.mark_file(key, f.path)

        result = ledger.update(key, state="done", message=f"Uploaded {len(files)} file(s)")
        label = get_effective_config("overcast_label") or "overcast"
        try:
            torrent_client.add_label_to_torrent(str(torrent["id"]), ADMIN_USER_DICT, label)
        except Exception as e:  # labels are a convenience; the ledger is the source of truth
            logger.debug(f"Could not label torrent as uploaded: {e}")
        return result
    except Exception as e:
        logger.exception(f"Overcast upload failed for {torrent.get('name')}: {e}")
        return ledger.update(key, state="error", message=str(e))
    finally:
        with _in_progress_lock:
            _in_progress.discard(key)


def start_upload(torrent: Dict[str, Any], torrent_client) -> bool:
    """Start an upload in a background thread. False if one is already running."""
    with _in_progress_lock:
        if torrent_key(torrent) in _in_progress:
            return False
    get_ledger().update(torrent_key(torrent), state="queued", message="", name=torrent.get("name"))
    threading.Thread(target=upload_torrent, args=(torrent, torrent_client), daemon=True).start()
    return True


def auto_upload(torrents: List[Dict[str, Any]], torrent_client) -> int:
    """Upload finished torrents that have not been uploaded or failed before."""
    if not is_enabled() or not get_effective_config("overcast_auto_upload"):
        return 0
    states = get_ledger().all()
    count = 0
    for t in torrents:
        if LABEL not in (t.get("labels") or []) or not is_complete(t):
            continue
        # Failed uploads wait for a manual retry. "uploading"/"queued" entries with
        # no live worker were interrupted (e.g. restart) and are resumed here.
        if states.get(torrent_key(t), {}).get("state") in ("done", "error"):
            continue
        with _in_progress_lock:
            if torrent_key(t) in _in_progress:
                continue
        upload_torrent(t, torrent_client)
        count += 1
    return count


def annotate(torrents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Attach Overcast upload state to torrents for the UI."""
    if not is_enabled():
        return torrents
    states = get_ledger().all()
    for t in torrents:
        s = states.get(torrent_key(t))
        t["overcast"] = {
            "state": s.get("state"),
            "message": s.get("message", ""),
            "uploaded": len(s.get("uploaded", [])),
            "total": s.get("total", 0),
        } if s else None
    return torrents
