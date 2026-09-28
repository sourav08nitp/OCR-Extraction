"""Durable OCR-only sessions and project folders, stored outside ingest collections."""

from datetime import datetime, timezone
import copy
import os
import threading
import time


SESSIONS = "ocr_sessions"
PROJECTS = "ocr_projects"
_client = None
_client_lock = threading.Lock()
_tree_cache = None
_tree_cache_until = 0.0
_cache_lock = threading.Lock()
TREE_CACHE_SECONDS = 5.0


def _database():
    """Return a process-wide Mongo client. MongoClient already manages a connection pool."""
    global _client
    if not os.environ.get("MONGODB_URI"):
        raise RuntimeError("MONGODB_URI is not configured")
    from pymongo import MongoClient
    with _client_lock:
        if _client is None:
            _client = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=10_000,
                                  maxPoolSize=20, minPoolSize=1, connect=False)
    return _client[os.environ.get("MONGODB_DB") or "questionbank"]


def _invalidate_tree():
    global _tree_cache_until
    with _cache_lock:
        _tree_cache_until = 0.0


def create_session(session_id, label, *, project_id=None, drive_file_id=None):
    database = _database()
    now = datetime.now(timezone.utc)
    database[SESSIONS].insert_one({
        "_id": session_id, "label": label, "projectId": project_id or None,
        "driveFileId": drive_file_id or None, "status": "open",
        "createdAt": now, "updatedAt": now,
    })
    _invalidate_tree()


def update_session(session_id, **fields):
    database = _database()
    fields["updatedAt"] = datetime.now(timezone.utc)
    database[SESSIONS].update_one({"_id": session_id}, {"$set": fields})
    _invalidate_tree()


def delete_session(session_id):
    """Remove OCR-only session metadata; Drive source deletion is intentionally separate."""
    database = _database()
    removed = database[SESSIONS].delete_one({"_id": session_id}).deleted_count == 1
    if removed:
        _invalidate_tree()
    return removed


def create_project(label):
    from bson import ObjectId
    name = str(label or "").strip()
    if not name:
        raise ValueError("Project name is required")
    database = _database()
    now = datetime.now(timezone.utc)
    project_id = ObjectId()
    database[PROJECTS].insert_one({"_id": project_id, "label": name,
                                   "createdAt": now, "updatedAt": now})
    _invalidate_tree()
    return str(project_id)


def update_project(project_id, label):
    from bson import ObjectId
    name = str(label or "").strip()
    if not name:
        raise ValueError("Project name is required")
    database = _database()
    result = database[PROJECTS].update_one(
        {"_id": ObjectId(project_id)},
        {"$set": {"label": name, "updatedAt": datetime.now(timezone.utc)}},
    )
    if not result.matched_count:
        raise LookupError("Project not found")
    _invalidate_tree()


def delete_project(project_id):
    """Remove a folder and leave its sessions unfiled."""
    from bson import ObjectId
    database = _database()
    result = database[PROJECTS].delete_one({"_id": ObjectId(project_id)})
    if not result.deleted_count:
        return False
    database[SESSIONS].update_many({"projectId": project_id}, {"$set": {
        "projectId": None, "updatedAt": datetime.now(timezone.utc)}})
    _invalidate_tree()
    return True


def project_tree():
    global _tree_cache, _tree_cache_until
    now = time.monotonic()
    with _cache_lock:
        if _tree_cache is not None and now < _tree_cache_until:
            return copy.deepcopy(_tree_cache)
    database = _database()
    projects = list(database[PROJECTS].find({}, {"label": 1}).sort("updatedAt", -1))
    sessions = list(database[SESSIONS].find({}, {"label": 1, "projectId": 1, "status": 1,
                                                   "driveFileId": 1, "updatedAt": 1}).sort("updatedAt", -1))
    tree = {
        "projects": [{"id": str(p["_id"]), "label": p["label"]} for p in projects],
        "sessions": [{"id": str(s["_id"]), "label": s.get("label", "Untitled OCR session"),
                      "projectId": s.get("projectId"), "status": s.get("status", "open"),
                      "driveFileId": s.get("driveFileId")} for s in sessions],
    }
    with _cache_lock:
        _tree_cache = tree
        _tree_cache_until = time.monotonic() + TREE_CACHE_SECONDS
    return copy.deepcopy(tree)
