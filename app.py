from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Optional
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pymongo import MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.errors import PyMongoError


ROOT = Path(__file__).resolve().parent
DEFAULT_REFRESH_AFTER = 86400
MAX_REFRESH_AFTER = 365 * 86400
_client = None
_collection = None
_mongo_lock = Lock()

app = FastAPI(title="PinFrames API", docs_url=None, redoc_url=None)


def get_collection() -> Collection:
    global _client, _collection
    if _collection is not None:
        return _collection

    uri = os.getenv("MONGODB_URI")
    if not uri:
        raise HTTPException(status_code=503, detail="MongoDB is not configured")

    with _mongo_lock:
        if _collection is None:
            try:
                _client = MongoClient(uri, serverSelectionTimeoutMS=5000, appname="PinFrames")
                _client.admin.command("ping")
                database = _client[os.getenv("MONGODB_DATABASE", "pinframes")]
                _collection = database["personal_feed"]
            except PyMongoError as error:
                _client = None
                raise HTTPException(status_code=503, detail="Could not connect to MongoDB") from error
    return _collection


def authorize_feed(authorization: Optional[str] = Header(default=None)) -> None:
    expected_token = os.getenv("FEED_TOKEN")
    if not expected_token:
        raise HTTPException(status_code=503, detail="Private feed is not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid feed token")
    supplied = authorization[len("Bearer "):]
    if not hmac.compare_digest(supplied, expected_token):
        raise HTTPException(status_code=401, detail="Invalid feed token")


def validate_images(images):
    if not isinstance(images, list) or not images or len(images) > 10000:
        raise HTTPException(status_code=400, detail="Expected 1 to 10000 image URLs")

    for image in images:
        if not isinstance(image, str) or len(image) > 2048:
            raise HTTPException(status_code=400, detail="Invalid image URL")
        parsed = urlsplit(image)
        if parsed.scheme != "https" or parsed.hostname != "i.pinimg.com":
            raise HTTPException(status_code=400, detail="Only Pinterest image URLs are accepted")
    return list(dict.fromkeys(images))


def get_feed_document():
    document = get_collection().find_one({"_id": "personal_feed"})
    if document is None:
        return {
            "_id": "personal_feed",
            "images": [],
            "refresh_after_seconds": DEFAULT_REFRESH_AFTER,
            "last_scraped_at": None,
        }
    return document


@app.get("/api/health")
def health():
    try:
        get_collection().database.client.admin.command("ping")
    except HTTPException as error:
        raise HTTPException(status_code=503, detail=error.detail) from error
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")
    return {"ok": True}


@app.get("/api/config")
def config():
    return {"requires_feed_token": True}


@app.get("/api/auth")
def check_feed_token(_authorized: None = Depends(authorize_feed)):
    return {"signed_in": True}


@app.get("/api/settings")
def get_settings(_authorized: None = Depends(authorize_feed)):
    document = get_feed_document()
    last_scraped_at = document.get("last_scraped_at")
    return {
        "refresh_after_seconds": document.get("refresh_after_seconds", DEFAULT_REFRESH_AFTER),
        "last_scraped_at": last_scraped_at.isoformat() if last_scraped_at else None,
    }


@app.post("/api/settings")
def update_settings(payload: dict, _authorized: None = Depends(authorize_feed)):
    refresh_after = payload.get("refresh_after_seconds")
    if isinstance(refresh_after, bool) or not isinstance(refresh_after, int):
        raise HTTPException(status_code=400, detail="Refresh age must be an integer number of seconds")
    if not 60 <= refresh_after <= MAX_REFRESH_AFTER:
        raise HTTPException(status_code=400, detail="Refresh age must be between 1 minute and 365 days")

    get_collection().update_one(
        {"_id": "personal_feed"},
        {"$set": {"refresh_after_seconds": refresh_after}},
        upsert=True,
    )
    return {"ok": True, "refresh_after_seconds": refresh_after}


@app.get("/api/images")
def get_images(_authorized: None = Depends(authorize_feed)):
    document = get_feed_document()
    last_scraped_at = document.get("last_scraped_at")
    return {
        "images": document.get("images", []),
        "last_scraped_at": last_scraped_at.isoformat() if last_scraped_at else None,
    }


@app.post("/api/collect")
def collect_images(payload: dict, _authorized: None = Depends(authorize_feed)):
    images = validate_images(payload.get("images"))
    scraped_at = datetime.now(timezone.utc)
    get_collection().update_one(
        {"_id": "personal_feed"},
        {"$set": {"images": images, "last_scraped_at": scraped_at}},
        upsert=True,
    )
    return {"ok": True, "count": len(images), "last_scraped_at": scraped_at.isoformat()}


@app.post("/api/consume")
def consume_image(payload: dict, _authorized: None = Depends(authorize_feed)):
    image = payload.get("image")
    validate_images([image])
    collection = get_collection()
    updated = collection.find_one_and_update(
        {"_id": "personal_feed", "images": image},
        {"$pull": {"images": image}},
        return_document=ReturnDocument.AFTER,
    )
    remaining = updated.get("images", []) if updated else get_feed_document().get("images", [])
    return {"removed": updated is not None, "remaining": len(remaining)}


@app.post("/api/clear")
def clear_images(_authorized: None = Depends(authorize_feed)):
    get_collection().update_one(
        {"_id": "personal_feed"},
        {"$set": {"images": [], "last_scraped_at": None}},
        upsert=True,
    )
    return {"ok": True, "remaining": 0}


@app.get("/")
def home():
    return FileResponse(ROOT / "display" / "index.html")