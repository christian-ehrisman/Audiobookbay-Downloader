"""Premiumize.me cloud downloader client.

Premiumize (https://www.premiumize.me) downloads torrents/magnets into the
user's cloud storage. It has no concept of labels, users, pausing or seeding
ratios, so this client keeps a small local TinyDB ledger of the transfers it
created. The ledger is what gives us:

* "label" filtering - only transfers added through ABB are shown
* per-user ownership - non-admin users only see their own transfers
* labels (e.g. beets labels) on a per-transfer basis

API reference: https://www.premiumize.me/api
"""
import os
import re
import time
from typing import Any, Dict, List, Optional

import requests
from tinydb import Query, TinyDB

from .constants import BEETS_COMPLETE_LABEL, BEETS_ERROR_LABEL, LABEL
from .models import User
from .torrent import TorrentClientInterface
from .utils import custom_logger

logger = custom_logger(__name__)

PREMIUMIZE_API_URL = "https://www.premiumize.me/api"

_BTIH_RE = re.compile(r"xt=urn:btih:([a-zA-Z0-9]+)", re.IGNORECASE)


class PremiumizeTransferStore:
    """Local ledger of Premiumize transfers created by ABB."""

    def __init__(self, db_path: Optional[str] = None):
        path = db_path or os.path.join(os.getenv("DB_PATH", "/tmp"), "premiumize.json")
        self._db = TinyDB(path)

    def add(self, transfer_id: str, user: User, labels: List[str], name: str = "", hash_string: str = "") -> None:
        q = Query()
        self._db.upsert(
            {
                "transfer_id": str(transfer_id),
                "user_id": user.id if user else None,
                "username": user.username if user else None,
                "labels": sorted(set(labels)),
                "name": name,
                "hash_string": hash_string,
                "added_date": int(time.time()),
            },
            q.transfer_id == str(transfer_id),
        )

    def get(self, transfer_id: str) -> Optional[Dict[str, Any]]:
        q = Query()
        return self._db.get(q.transfer_id == str(transfer_id))

    def all(self) -> Dict[str, Dict[str, Any]]:
        return {entry["transfer_id"]: entry for entry in self._db.all()}

    def remove(self, transfer_id: str) -> None:
        q = Query()
        self._db.remove(q.transfer_id == str(transfer_id))

    def set_labels(self, transfer_id: str, labels: List[str]) -> bool:
        q = Query()
        updated = self._db.update({"labels": sorted(set(labels))}, q.transfer_id == str(transfer_id))
        return len(updated) > 0


class PremiumizeClient(TorrentClientInterface):
    """Premiumize.me implementation of the torrent client interface."""

    def __init__(
        self,
        api_key: str = "",
        folder_id: str = "",
        api_url: str = PREMIUMIZE_API_URL,
        store: Optional[PremiumizeTransferStore] = None,
    ):
        self.api_key = api_key
        self.folder_id = folder_id or ""
        self.api_url = api_url.rstrip("/")
        self.session = requests.Session()
        if api_key:
            self.session.headers.update({"Authorization": f"Bearer {api_key}"})
        self._store = store

    # ------------------------------------------------------------------ helpers

    @property
    def store(self) -> PremiumizeTransferStore:
        if self._store is None:
            self._store = PremiumizeTransferStore()
        return self._store

    def _request(self, method: str, endpoint: str, **kwargs) -> Optional[Dict[str, Any]]:
        """Call the Premiumize API. Returns the JSON body on success, else None.

        Premiumize returns HTTP 200 for business errors, with
        {"status": "error", "message": ...}, so the body status is checked too.
        """
        url = f"{self.api_url}{endpoint}"
        try:
            response = self.session.request(method, url, timeout=30, **kwargs)
        except Exception as e:
            logger.error(f"Premiumize request to {endpoint} failed: {e}")
            return None

        if response.status_code != 200:
            logger.error(f"Premiumize {endpoint} HTTP {response.status_code}: {response.text[:200]}")
            return None

        try:
            data = response.json()
        except ValueError:
            logger.error(f"Premiumize {endpoint} returned non-JSON response")
            return None

        if data.get("status") != "success":
            logger.error(f"Premiumize {endpoint} error: {data.get('message') or data}")
            return None
        return data

    def _list_transfers(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "/transfer/list")
        if not data:
            return []
        return data.get("transfers", []) or []

    def _get_transfer(self, transfer_id: str) -> Optional[Dict[str, Any]]:
        for transfer in self._list_transfers():
            if str(transfer.get("id")) == str(transfer_id):
                return transfer
        return None

    def _check_user_access(self, user: User, transfer_id: str) -> bool:
        entry = self.store.get(transfer_id)
        if entry is None:
            logger.warning(f"Premiumize transfer {transfer_id} was not added through ABB")
            return False
        if user and user.role == "admin":
            return True
        if user and entry.get("user_id") == user.id:
            return True
        logger.warning(f"User {user.id if user else None} tried to access transfer {transfer_id} without permission.")
        return False

    @staticmethod
    def _hash_from_magnet(url: str) -> str:
        match = _BTIH_RE.search(url or "")
        return match.group(1).lower() if match else ""

    @staticmethod
    def _magnet_name(url: str) -> str:
        match = re.search(r"[?&]dn=([^&]+)", url or "")
        if not match:
            return ""
        return requests.utils.unquote(match.group(1)).replace("+", " ")

    @staticmethod
    def _map_status(status: str) -> str:
        return {
            "queued": "Queued to download",
            "waiting": "Queued to download",
            "running": "Downloading",
            "seeding": "Seeding",
            "finished": "Complete",
            "error": "Error",
            "timeout": "Error",
            "banned": "Error",
            "deleted": "Deleted",
        }.get((status or "").lower(), status or "Unknown")

    def _resolve_source(self, torrent_url: str):
        """Work out how to hand a source to Premiumize.

        Magnets and public URLs are passed as `src`. Jackett download links
        usually point at a local Jackett instance Premiumize cannot reach,
        so if the URL serves a .torrent file directly we upload its bytes.
        Returns (data, files) for requests.
        """
        data: Dict[str, Any] = {}
        if self.folder_id:
            data["folder_id"] = self.folder_id

        if torrent_url.startswith("magnet:"):
            data["src"] = torrent_url
            return data, None

        try:
            response = requests.get(torrent_url, allow_redirects=False, timeout=30)
            location = response.headers.get("Location", "")
            if response.status_code in (301, 302, 303, 307, 308) and location:
                data["src"] = location
                return data, None
            content_type = response.headers.get("Content-Type", "")
            if response.status_code == 200 and (
                "bittorrent" in content_type or response.content[:1] == b"d"
            ):
                return data, {"src": ("download.torrent", response.content, "application/x-bittorrent")}
        except Exception as e:
            logger.warning(f"Could not fetch {torrent_url} locally, passing URL to Premiumize: {e}")

        data["src"] = torrent_url
        return data, None

    # ------------------------------------------------------------- interface

    def get_account_info(self) -> Optional[Dict[str, Any]]:
        return self._request("GET", "/account/info")

    def get_torrents(self, user: User) -> List[Dict[str, Any]]:
        tracked = self.store.all()
        results = []
        for transfer in self._list_transfers():
            transfer_id = str(transfer.get("id", ""))
            entry = tracked.get(transfer_id)
            if entry is None:
                continue  # not added via ABB
            labels = entry.get("labels", [])
            if LABEL not in labels:
                continue
            if user.role != "admin" and entry.get("user_id") != user.id:
                continue

            progress = transfer.get("progress") or 0
            raw_status = transfer.get("status", "")
            if raw_status in ("finished", "seeding"):
                progress = 1.0
            name = transfer.get("name") or entry.get("name") or transfer_id
            hash_string = entry.get("hash_string") or transfer_id

            results.append({
                "id": transfer_id,
                "labels": labels,
                "name": name.replace("_", " ").replace("+", " ").replace(".", " ").strip(),
                "status": self._map_status(raw_status),
                "message": transfer.get("message", ""),
                "total_size": 0,  # not exposed by /transfer/list
                "percent_done": float(progress) * 100,
                "downloaded_ever": 0,
                "uploaded_ever": 0,
                "added_date": transfer.get("created_at") or entry.get("added_date", 0),
                "activity_date": 0,
                "files": [],
                "use_beets_import": False,  # files live in the cloud, not on disk
                "imported": BEETS_COMPLETE_LABEL in labels,
                "importError": BEETS_ERROR_LABEL in labels,
                "eta": -1,
                "candidates": [],
                "hash_string": hash_string,
                "added_by": entry.get("username") if user.role == "admin" else None,
                "upload_ratio": 0.0,
                "folder_id": transfer.get("folder_id"),
                "file_id": transfer.get("file_id"),
            })

        results.sort(key=lambda t: t["added_date"] or 0, reverse=True)
        return results

    def add_torrent(self, torrent_url: str, user: User, label: str = None, category: str = None) -> bool:
        """Create a Premiumize transfer (category is ignored)."""
        if label is None:
            label = LABEL

        data, files = self._resolve_source(torrent_url)
        result = self._request("POST", "/transfer/create", data=data, files=files)
        if not result or not result.get("id"):
            logger.error(f"Failed to add transfer to Premiumize: {torrent_url}")
            return False

        labels = [label]
        if user:
            labels.append(user.id)
            if user.username:
                labels.append(f"username:{user.username}")
        source = data.get("src", "")
        self.store.add(
            transfer_id=str(result["id"]),
            user=user,
            labels=labels,
            name=result.get("name") or self._magnet_name(source),
            hash_string=self._hash_from_magnet(source),
        )
        logger.info(f"Added Premiumize transfer {result['id']} ({result.get('name', '')})")
        return True

    def delete_torrent(self, torrent_id: str, user: User, delete_data: bool = True) -> bool:
        """Remove a transfer. With delete_data, also delete the cloud file/folder it produced."""
        if not user or not self._check_user_access(user, torrent_id):
            return False

        transfer = self._get_transfer(torrent_id)
        if transfer is None:
            # Already gone on Premiumize's side; just forget it locally.
            self.store.remove(torrent_id)
            return True

        if delete_data:
            self._delete_transfer_output(transfer)

        if self._request("POST", "/transfer/delete", data={"id": torrent_id}) is None:
            return False
        self.store.remove(torrent_id)
        logger.info(f"Premiumize transfer {torrent_id} {'and its data ' if delete_data else ''}deleted.")
        return True

    def _delete_transfer_output(self, transfer: Dict[str, Any]) -> None:
        """Delete the cloud item a finished transfer produced.

        A single-file transfer sets file_id (folder_id is then the *parent*
        folder, which must not be touched). A multi-file transfer leaves
        file_id null and folder_id points at the folder it created. The
        configured target folder and the root are never deleted.
        """
        file_id = transfer.get("file_id")
        folder_id = transfer.get("folder_id")
        if file_id:
            self._request("POST", "/item/delete", data={"id": file_id})
        elif folder_id and str(folder_id) != str(self.folder_id):
            self._request("POST", "/folder/delete", data={"id": folder_id})

    def pause_torrent(self, torrent_id: str, user: User) -> bool:
        logger.warning("Premiumize does not support pausing transfers")
        return False

    def resume_torrent(self, torrent_id: str, user: User) -> bool:
        """Retry a failed transfer (Premiumize has no pause/resume)."""
        if not self._check_user_access(user, torrent_id):
            return False
        return self._request("POST", "/transfer/retry", data={"id": torrent_id}) is not None

    def add_label_to_torrent(self, torrent_id: str, user: User, label: str) -> bool:
        entry = self.store.get(torrent_id)
        if entry is None:
            return False
        labels = list(entry.get("labels", [])) + [label]
        if user:
            labels.append(user.id)
            if user.username:
                labels.append(f"username:{user.username}")
        return self.store.set_labels(torrent_id, labels)

    def remove_label_from_torrent(self, torrent_id: str, user: User, label: str) -> bool:
        entry = self.store.get(torrent_id)
        if entry is None:
            return False
        labels = [lbl for lbl in entry.get("labels", []) if lbl != label]
        return self.store.set_labels(torrent_id, labels)

    def set_category(self, torrent_id: str, user: User, category: str) -> bool:
        logger.warning("Premiumize does not support categories")
        return False

    def delete_old_torrents(self) -> None:
        # Cleanup in other clients is gated on the beets "imported" label, which
        # never applies here because Premiumize files live in the cloud.
        logger.debug("Premiumize: automatic cleanup not applicable")

    def pause_stale_torrents(self) -> None:
        logger.debug("Premiumize: stale transfer pausing not applicable")
