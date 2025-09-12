from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import ConnectionFailure
from typing import Tuple
from .config import get_config


_client: MongoClient | None = None
_db: Database | None = None


def init_db() -> Tuple[MongoClient, Database]:
    global _client, _db
    if _client is not None and _db is not None:
        return _client, _db

    cfg = get_config()
    _client = MongoClient(cfg.mongo_uri)
    try:
        _client.admin.command("ping")
    except ConnectionFailure as exc:
        raise RuntimeError(f"Failed to connect to MongoDB: {exc}")

    _db = _client[cfg.mongo_db]

    # Ensure indexes
    _db["users"].create_index("id", unique=True)
    _db["users"].create_index("gemini_api_key_hash", unique=True, sparse=True)
    _db["settings"].create_index("key", unique=True)
    _db["channels"].create_index([("user_id", 1), ("chat_id", 1)], unique=True)
    _db["payments"].create_index([("user_id", 1), ("time", 1)])
    _db["schedules"].create_index([("user_id", 1), ("scheduled_at", 1)])
    _db["uploads"].create_index([("user_id", 1), ("created_at", 1)])

    return _client, _db


def get_db() -> Database:
    if _db is None:
        init_db()
    assert _db is not None
    return _db