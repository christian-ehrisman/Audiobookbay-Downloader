from enum import Enum
from pydantic import BaseModel

class TorrentClientType(str, Enum):
    transmission = "transmission"
    decypharr = "decypharr"
    qbittorrent = "qbittorrent"
    premiumize = "premiumize"

class User(BaseModel):
    username: str = "default"
    role: str = "user" 
    id: str = "default"

class AddTorrentError(Exception):
    """Adding a torrent failed for a reason worth showing to the user."""


class TorrentRequest(BaseModel):
    url: str
