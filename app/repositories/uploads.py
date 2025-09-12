from __future__ import annotations

from typing import Any, Dict, Optional
from datetime import datetime
from bson import ObjectId
from pymongo.database import Database


class UploadsRepository:
    def __init__(self, db: Database) -> None:
        self.collection = db["uploads"]

    def create(self, user_id: int, file_id: str, file_name: str, size: int, mime_type: str, text: str) -> str:
        doc = {
            "user_id": user_id,
            "file_id": file_id,
            "file_name": file_name,
            "size": int(size),
            "mime_type": mime_type,
            "text": text,
            "num_questions_requested": 0,
            "num_questions_generated": 0,
            "created_at": datetime.utcnow(),
        }
        res = self.collection.insert_one(doc)
        return str(res.inserted_id)

    def get(self, upload_id: str) -> Optional[Dict[str, Any]]:
        try:
            oid = ObjectId(upload_id)
        except Exception:
            return None
        return self.collection.find_one({"_id": oid})

    def bump_requested(self, upload_id: str, qty: int) -> None:
        try:
            oid = ObjectId(upload_id)
        except Exception:
            return
        self.collection.update_one({"_id": oid}, {"$inc": {"num_questions_requested": int(qty)}})

    def bump_generated(self, upload_id: str, qty: int) -> None:
        try:
            oid = ObjectId(upload_id)
        except Exception:
            return
        self.collection.update_one({"_id": oid}, {"$inc": {"num_questions_generated": int(qty)}})

