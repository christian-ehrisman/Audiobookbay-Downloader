
import os
import uvicorn
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Query, HTTPException, Depends, status as httpstatus, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from pydantic import BaseModel

from .models import TorrentRequest, User, TorrentClientType
from .torrent_service import (
    init_torrent_service, get_torrents, add_torrent, delete_torrent, 
    pause_torrent, resume_torrent, remove_label_from_torrent_with_hash, delete_old_torrents,
    pause_stale_torrents, set_category
)
from .audiobookbay import search_audiobook
from .beetsapi import autoimport
from .constants import BEETS_ERROR_LABEL, TRANSMISSION_URL, TRANSMISSION_USER, TRANSMISSION_PASS, DECYPHARR_URL, DECYPHARR_API_KEY, QBITTORRENT_URL, QBITTORRENT_USERNAME, QBITTORRENT_PASSWORD, PREMIUMIZE_API_KEY, PREMIUMIZE_FOLDER_ID, TORRENT_CLIENT_TYPE, SESSION_KEY, TITLE, AUTH_MODE, GOODREADS_ENABLED
from .db import select_candidate
from .utils import custom_logger
from .goodreads import poll_and_download, poll_and_download_single_user, validate_goodreads_config
from .goodreads_db import get_config as get_goodreads_config, save_config as save_goodreads_config, get_all_processed_books, delete_processed_book, clear_all_processed_books, get_enabled_configs, migrate_legacy_data_for_user
from .config_db import get_all_effective_configs, get_config_schema, set_config, get_effective_config, CONFIG_SCHEMA
from .torrent_service import get_torrent_service
from .constants import ADMIN_USER_DICT
from . import overcast

logger = custom_logger(__name__)

scheduler = BackgroundScheduler()


class GoodreadsConfigRequest(BaseModel):
    goodreads_user_id: str
    shelf: str = "to-read"
    poll_interval: int = 60
    enabled: bool = False

class CategoryRequest(BaseModel):
    category: str

class AppConfigUpdate(BaseModel):
    configs: dict

def setup_goodreads_scheduler():
    """Setup or update the Goodreads polling scheduler based on current config."""
    enabled_configs = get_enabled_configs()
    
    if scheduler.get_job('goodreads_poll'):
        scheduler.remove_job('goodreads_poll')
    
    if enabled_configs:
        min_poll_interval = min(config.get("poll_interval", 60) for config in enabled_configs)
        scheduler.add_job(
            poll_and_download,
            'interval',
            minutes=min_poll_interval,
            id='goodreads_poll',
            replace_existing=True
        )
        
        if not scheduler.running:
            scheduler.start()
            logger.info("Started Goodreads scheduler")
        
        logger.info(f"Goodreads scheduler configured with {min_poll_interval} minute interval for {len(enabled_configs)} users")
    else:
        logger.info("Goodreads scheduler disabled (no enabled configurations)")

def run_overcast_auto_upload():
    try:
        service = get_torrent_service()
        overcast.auto_upload(service.get_torrents(ADMIN_USER_DICT), service.client)
    except Exception as e:
        logger.exception(f"Overcast auto-upload failed: {e}")

def setup_overcast_scheduler():
    """Add/replace/remove the Overcast auto-upload job based on current config."""
    if overcast.is_enabled() and get_effective_config("overcast_auto_upload"):
        minutes = max(1, int(get_effective_config("overcast_poll_interval") or 15))
        scheduler.add_job(run_overcast_auto_upload, 'interval', minutes=minutes,
                          id='overcast_upload', replace_existing=True, max_instances=1, coalesce=True)
        if not scheduler.running:
            scheduler.start()
        logger.info(f"Overcast auto-upload every {minutes} minutes")
    elif scheduler.get_job('overcast_upload'):
        scheduler.remove_job('overcast_upload')
        logger.info("Overcast auto-upload disabled")

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        client_type = TorrentClientType(TORRENT_CLIENT_TYPE)

        if client_type == TorrentClientType.transmission:
            init_torrent_service(
                client_type=client_type,
                url=TRANSMISSION_URL,
                username=TRANSMISSION_USER,
                password=TRANSMISSION_PASS
            )
        elif client_type == TorrentClientType.decypharr:
            init_torrent_service(
                client_type=client_type,
                url=DECYPHARR_URL,
                api_key=DECYPHARR_API_KEY
            )
        elif client_type == TorrentClientType.qbittorrent:
            init_torrent_service(
                client_type=client_type,
                url=QBITTORRENT_URL,
                username=QBITTORRENT_USERNAME,
                password=QBITTORRENT_PASSWORD
            )
        elif client_type == TorrentClientType.premiumize:
            init_torrent_service(
                client_type=client_type,
                api_key=PREMIUMIZE_API_KEY,
                folder_id=PREMIUMIZE_FOLDER_ID
            )

        logger.info(f"Initialized torrent service with {client_type.value} client")
    except ValueError as e:
        logger.error(f"Invalid torrent client type: {TORRENT_CLIENT_TYPE}")
        raise RuntimeError(f"Invalid torrent client type: {TORRENT_CLIENT_TYPE}")
    except Exception as e:
        logger.error(f"Failed to initialize torrent service: {e}")
        raise RuntimeError(f"Failed to initialize torrent service: {e}")
    
    if GOODREADS_ENABLED:
        setup_goodreads_scheduler()
        logger.info("Goodreads integration enabled")
    setup_overcast_scheduler()
    yield
    
    if scheduler.running:
        scheduler.shutdown()
    logger.info("Application shutdown")

app = FastAPI(lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.add_middleware(SessionMiddleware, secret_key=SESSION_KEY)

def authenticate_authentik(request: Request):
    username = request.headers.get("X-authentik-username")
    id = request.headers.get("X-authentik-uid")
    role = "admin" if request.headers.get("X-authentik-role") == "admin" else "user"
    if not username:
        raise HTTPException(status_code=httpstatus.HTTP_401_UNAUTHORIZED, detail="Missing username header")
    logger.info(f"Authenticating user: {username}, role: {role}, id: {id}")
    return User(username=username, role=role, id=username)

def authenticate(request: Request):
    if AUTH_MODE == "none":
        return User(username="admin", role="admin", id="admin")
    elif AUTH_MODE == "authentik":
        return authenticate_authentik(request)
    else:
        raise HTTPException(status_code=500, detail="Invalid auth mode. Only 'authentik' and 'none' are supported.")

def authenticate_userpass_authentik(request: Request):
    username = request.headers.get("X-authentik-username")
    id = request.headers.get("X-authentik-uid")
    role = "admin" if request.headers.get("X-authentik-role") == "admin" else "user"
    if not username:
        raise HTTPException(status_code=httpstatus.HTTP_401_UNAUTHORIZED, detail="Missing username header")
    request.session["user_id"] = username
    request.session["username"] = username
    request.session["role"] = role
    logger.info(f"Authenticating user: {username}, role: {role}, id: {id}")
    return User(username=username, role=role, id=username)

def validate_admin(request: Request):
    if AUTH_MODE == "none":
        return User(username="admin", role="admin", id="admin")
    elif AUTH_MODE == "authentik":
        username_header = request.headers.get("X-authentik-username")
        role_header = request.headers.get("X-authentik-role")
        role = "admin" if role_header == "admin" else request.session.get("role")
        id = request.session.get("user_id") or username_header
        username = request.session.get("username") or username_header

        if username_header:
            request.session["user_id"] = username_header
            request.session["username"] = username_header
            request.session["role"] = role

        if role != "admin":
            raise HTTPException(status_code=403, detail="Access forbidden: Admins only")
        return User(
            username=username,
            role=role,
            id=id
        )
    else:
        raise HTTPException(status_code=500, detail="Invalid auth mode. Only 'authentik' and 'none' are supported.")

@app.get("/")
def root(request: Request):
    if AUTH_MODE == "none":
        return FileResponse(os.path.join("static", "index.html"))
    elif AUTH_MODE == "authentik":
        username = request.headers.get("X-authentik-username")
        if not username and "user_id" not in request.session:
            return RedirectResponse("/login")
        return FileResponse(os.path.join("static", "index.html"))
    else:
        return RedirectResponse("/login")

@app.get("/title")
def title():
    return {"title": TITLE}

@app.get("/torrent-client-type")
def get_torrent_client_type():
    return {"torrent_client_type": TORRENT_CLIENT_TYPE}

@app.get("/overcast-enabled")
def get_overcast_enabled():
    return {"enabled": overcast.is_enabled()}

@app.get("/goodreads-enabled")
def get_goodreads_enabled():
    return {"enabled": GOODREADS_ENABLED}

@app.get("/role")
def role(request: Request, user: User = Depends(authenticate)):
    if user:
        return {"role": user.role}
    else:
        return "Invalid credentials"

@app.get("/login")
async def login(request: Request):
    if AUTH_MODE == "none":
        request.session["user_id"] = "admin"
        request.session["username"] = "admin"
        request.session["role"] = "admin"
        return RedirectResponse("/")
    elif AUTH_MODE == "authentik":
        user = authenticate_userpass_authentik(request)
        if user:
            request.session["user_id"] = user.id
            request.session["username"] = user.username
            request.session["role"] = user.role
            return RedirectResponse("/")
        else:
            return "Invalid credentials"
    else:
        raise HTTPException(status_code=500, detail="Invalid auth mode. Only 'authentik' and 'none' are supported.")

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    response = RedirectResponse("/login")
    response.delete_cookie("user_id")
    return response

def status():
    now = datetime.utcnow().isoformat() + "Z"
    return {"status": "ok", "timestamp": now}

@app.get("/search")
def search(
    query: str = Query(..., description="Search query"),
    user: User = Depends(authenticate)
):
    try:
        results = search_audiobook(query)
        return {"results": results}
    except Exception as e:
        logger.error(f"Search failed: {e}")
        raise HTTPException(status_code=500, detail="Search failed")

@app.post("/add")
def add(
    torrent: TorrentRequest,
    user: User = Depends(authenticate)
):
    try:
        success = add_torrent(torrent.url, user)
        if success:
            return {"status": "ok", "message": "Torrent added successfully"}
        else:
            raise HTTPException(status_code=500, detail="Failed to add torrent")
    except Exception as e:
        logger.error(f"Add failed: {e}")
        raise HTTPException(status_code=500, detail="Add failed")

@app.get("/list")
def list_torrents(user: User = Depends(authenticate)):
    try:
        return overcast.annotate(get_torrents(user))
    except Exception as e:
        logger.error(f"List torrents failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to list torrents")

@app.delete("/torrent/{torrent_id}")
def delete_torrent_endpoint(
    torrent_id: str,
    delete_data: bool = Query(True, description="Delete downloaded data as well"),
    user: User = Depends(authenticate)
):
    try:
        if delete_torrent(torrent_id, user, delete_data=delete_data):
            return {"status": "ok", "message": f"Torrent {torrent_id} deleted successfully."}
        else:
            raise HTTPException(status_code=500, detail=f"Failed to delete torrent {torrent_id}")
    except Exception as e:
        logger.error(f"Delete torrent failed: {e}")
        raise HTTPException(status_code=500, detail=f"Delete torrent failed: {e}")

@app.post("/torrent/{torrent_id}/pause")
def pause_torrent_endpoint(
    torrent_id: str,
    user: User = Depends(authenticate)
):
    try:
        if pause_torrent(torrent_id, user):
            return {"status": "ok", "message": f"Torrent {torrent_id} paused successfully."}
        else:
            raise HTTPException(status_code=500, detail=f"Failed to pause torrent {torrent_id}")
    except Exception as e:
        logger.error(f"Pause torrent failed: {e}")
        raise HTTPException(status_code=500, detail=f"Pause torrent failed: {e}")

@app.post("/torrent/{torrent_id}/play")
def play_torrent_endpoint(
    torrent_id: str,
    user: User = Depends(authenticate)
):
    try:
        if resume_torrent(torrent_id, user):
            return {"status": "ok", "message": f"Torrent {torrent_id} resumed successfully."}
        else:
            raise HTTPException(status_code=500, detail=f"Failed to resume torrent {torrent_id}")
    except Exception as e:
        logger.error(f"Resume torrent failed: {e}")
        raise HTTPException(status_code=500, detail=f"Resume torrent failed: {e}")

@app.post("/torrent/{torrent_id}/category")
def set_category_endpoint(
    torrent_id: str,
    category_request: CategoryRequest,
    user: User = Depends(authenticate)
):
    """Set category for a torrent (qBittorrent only)"""
    try:
        if set_category(torrent_id, user, category_request.category):
            return {"status": "ok", "message": f"Category set to '{category_request.category}' for torrent {torrent_id}"}
        else:
            raise HTTPException(status_code=500, detail=f"Failed to set category for torrent {torrent_id}")
    except Exception as e:
        logger.error(f"Set category failed: {e}")
        raise HTTPException(status_code=500, detail=f"Set category failed: {e}")
@app.post("/torrent/{torrent_id}/overcast")
def upload_to_overcast_endpoint(torrent_id: str, user: User = Depends(authenticate)):
    """Upload a finished torrent's audio files to Overcast (runs in the background)."""
    if not overcast.is_enabled():
        raise HTTPException(status_code=400, detail="Overcast upload is not enabled")
    torrent = next((t for t in get_torrents(user) if str(t["id"]) == torrent_id), None)
    if torrent is None:
        raise HTTPException(status_code=404, detail=f"Torrent {torrent_id} not found")
    if not overcast.is_complete(torrent):
        raise HTTPException(status_code=409, detail="Torrent has not finished downloading")
    if not overcast.start_upload(torrent, get_torrent_service().client):
        raise HTTPException(status_code=409, detail="Upload already in progress")
    return {"status": "ok", "message": f"Uploading {torrent['name']} to Overcast"}

@app.post("/selectCandidate/{hash_string}/{candidate_id}")
def select_candidate_endpoint(hash_string: str, candidate_id: str, user: User = Depends(authenticate)):
    try:
        select_candidate(hash_string, candidate_id)
        remove_label_from_torrent_with_hash(hash_string, user, BEETS_ERROR_LABEL)
        return {"status": "ok", "message": f"Candidate {candidate_id} selected for torrent {hash_string}"}
    except Exception as e:
        logger.error(f"Select candidate failed: {e}")
        raise HTTPException(status_code=500, detail=f"Select candidate failed: {e}")

@app.post("/autoimport")
def autoimport_endpoint():
    try:
        autoimport()
        run_overcast_auto_upload()
        delete_old_torrents()
        pause_stale_torrents()
        return {"status": "ok", "message": "Auto-import completed successfully"}
    except Exception as e:
        logger.error(f"Auto-import failed: {e}")
        raise HTTPException(status_code=500, detail=f"Auto-import failed: {e}")


@app.get("/goodreads/config")
def get_goodreads_config_endpoint(user: User = Depends(authenticate)):
    if not GOODREADS_ENABLED:
        raise HTTPException(status_code=404, detail="Goodreads integration is not enabled")
    migrate_legacy_data_for_user(user.id, user.role == "admin")
    return get_goodreads_config(user.id)

@app.post("/goodreads/config")
def save_goodreads_config_endpoint(config: GoodreadsConfigRequest, user: User = Depends(authenticate)):
    if not GOODREADS_ENABLED:
        raise HTTPException(status_code=404, detail="Goodreads integration is not enabled")
    
    try:
        result = save_goodreads_config(
            user_id=user.id,
            goodreads_user_id=config.goodreads_user_id,
            shelf=config.shelf,
            poll_interval=config.poll_interval,
            enabled=config.enabled
        )
        
        setup_goodreads_scheduler()
        
        return result
    except Exception as e:
        logger.error(f"Failed to save Goodreads config: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save config: {e}")

@app.post("/goodreads/validate")
def validate_goodreads_endpoint(config: GoodreadsConfigRequest, user: User = Depends(authenticate)):
    if not GOODREADS_ENABLED:
        raise HTTPException(status_code=404, detail="Goodreads integration is not enabled")
    
    return validate_goodreads_config(config.goodreads_user_id, config.shelf)

@app.post("/goodreads/poll")
def trigger_goodreads_poll(user: User = Depends(authenticate)):
    if not GOODREADS_ENABLED:
        raise HTTPException(status_code=404, detail="Goodreads integration is not enabled")
    
    try:
        result = poll_and_download_single_user(user.id)
        return result
    except Exception as e:
        logger.error(f"Manual poll failed: {e}")
        raise HTTPException(status_code=500, detail=f"Poll failed: {e}")

@app.get("/goodreads/books")
def get_processed_books(user: User = Depends(authenticate)):
    if not GOODREADS_ENABLED:
        raise HTTPException(status_code=404, detail="Goodreads integration is not enabled")
    
    return get_all_processed_books(user.id)

@app.delete("/goodreads/books/{book_id}")
def delete_processed_book_endpoint(book_id: str, user: User = Depends(authenticate)):
    if not GOODREADS_ENABLED:
        raise HTTPException(status_code=404, detail="Goodreads integration is not enabled")
    
    if delete_processed_book(user.id, book_id):
        return {"status": "ok", "message": f"Book {book_id} removed from processed list"}
    else:
        raise HTTPException(status_code=404, detail=f"Book {book_id} not found")

@app.delete("/goodreads/books")
def clear_all_processed_books_endpoint(user: User = Depends(authenticate)):
    if not GOODREADS_ENABLED:
        raise HTTPException(status_code=404, detail="Goodreads integration is not enabled")
    
    count = clear_all_processed_books(user.id)
    return {"status": "ok", "message": f"Cleared {count} processed books"}


@app.get("/config")
def get_app_config(user: User = Depends(validate_admin)):
    configs = get_all_effective_configs()
    schema = get_config_schema()
    result = {}
    for key, value in configs.items():
        s = schema.get(key, {})
        is_sensitive = s.get("sensitive", False)
        # Filter out non-serializable fields (like 'type' which is a Python type)
        result[key] = {
            "value": "********" if is_sensitive and value else value,
            "has_value": bool(value) if is_sensitive else None,
            "label": s.get("label", key),
            "group": s.get("group", "app"),
            "sensitive": is_sensitive,
            "options": s.get("options"),
        }
    return result


@app.post("/config")
def save_app_config(update: AppConfigUpdate, user: User = Depends(validate_admin)):
    saved_keys = []
    for key, value in update.configs.items():
        if key not in CONFIG_SCHEMA:
            continue
        if CONFIG_SCHEMA[key].get("sensitive") and value == "********":
            continue
        set_config(key, value)
        saved_keys.append(key)
    
    if any(k.startswith("overcast_") for k in saved_keys):
        setup_overcast_scheduler()
    return {"status": "ok", "saved": saved_keys, "message": "Configuration saved. Restart may be required for some changes to take effect."}


@app.post("/config/test-torrent")
def test_torrent_connection(user: User = Depends(validate_admin)):
    try:
        from .torrent import create_torrent_client
        client_type = get_effective_config("torrent_client_type")
        
        if client_type == "transmission":
            url = get_effective_config("transmission_url")
            username = get_effective_config("transmission_user")
            password = get_effective_config("transmission_pass")
            if not url:
                return {"success": False, "message": "Transmission URL not configured"}
            client = create_torrent_client("transmission", url=url, username=username, password=password)
        elif client_type == "qbittorrent":
            url = get_effective_config("qbittorrent_url")
            username = get_effective_config("qbittorrent_username")
            password = get_effective_config("qbittorrent_password")
            if not url:
                return {"success": False, "message": "qBittorrent URL not configured"}
            client = create_torrent_client("qbittorrent", url=url, username=username, password=password)
        elif client_type == "decypharr":
            url = get_effective_config("decypharr_url")
            api_key = get_effective_config("decypharr_api_key")
            if not url:
                return {"success": False, "message": "Decypharr URL not configured"}
            client = create_torrent_client("decypharr", url=url, api_key=api_key)
        elif client_type == "premiumize":
            api_key = get_effective_config("premiumize_api_key")
            if not api_key:
                return {"success": False, "message": "Premiumize API key not configured"}
            client = create_torrent_client("premiumize", api_key=api_key, folder_id=get_effective_config("premiumize_folder_id"))
            account = client.get_account_info()
            if not account:
                return {"success": False, "message": "Premiumize rejected the API key (see logs)"}
            if not account.get("premium_until"):
                return {"success": False, "message": "Connected, but this Premiumize account is not premium"}
            torrents = client.get_torrents(user)
            return {"success": True, "message": f"Connected to Premiumize (customer {account.get('customer_id')}). Found {len(torrents)} ABB transfers."}
        else:
            return {"success": False, "message": f"Unknown client type: {client_type}"}
        
        torrents = client.get_torrents(user)
        return {"success": True, "message": f"Connected successfully. Found {len(torrents)} torrents."}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.post("/config/test-overcast")
def test_overcast_connection(user: User = Depends(validate_admin)):
    """Log in and read the upload form without uploading anything."""
    try:
        form = overcast.make_client().get_upload_form()
        accept = f" Overcast accepts: {', '.join(form.accept)}." if form.accept else ""
        return {"success": True, "message": f"Logged in to Overcast and found the upload form.{accept}"}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.post("/config/test-jackett")
def test_jackett_connection(user: User = Depends(validate_admin)):
    try:
        from .audiobookbay import search_audiobook
        url = get_effective_config("jackett_api_url")
        api_key = get_effective_config("jackett_api_key")
        if not url or not api_key:
            return {"success": False, "message": "Jackett URL or API key not configured"}
        
        results = search_audiobook("test", url, api_key)
        return {"success": True, "message": f"Connected successfully. Search returned {len(results)} results."}
    except Exception as e:
        return {"success": False, "message": str(e)}


if __name__ == "__main__":
    uvicorn.run("abb.main:app", host="0.0.0.0", port=9000, reload=True)
