from typing import Optional, Dict, Any
from datetime import datetime
from pymongo.database import Database


class UsersRepository:
    def __init__(self, db: Database) -> None:
        self.collection = db["users"]

    def get(self, user_id: int) -> Optional[Dict[str, Any]]:
        return self.collection.find_one({"id": user_id})

    def upsert_user(self, user_id: int, username: Optional[str]) -> Dict[str, Any]:
        now = datetime.utcnow()
        update = {
            "$setOnInsert": {
                "id": user_id,
                # "username": username,  <-- Removed to avoid conflict with $set
                "type": "regular",
                "role": "user",
                "registered_at": now,
                "total_notes": 0,
                "notes_today": 0,
                "last_note_time": None,
                "default_question_type": "text",
                "questions_per_note": 5,
            },
            "$set": {"username": username} if username else {},
        }
        # If username is not provided, ensure it gets set to None on insert
        if not username:
             update["$setOnInsert"]["username"] = None

        self.collection.update_one({"id": user_id}, update, upsert=True)
        return self.get(user_id) or {}

    def set_premium(self, user_id: int, days: int) -> None:
        now = datetime.utcnow()
        self.collection.update_one(
            {"id": user_id},
            {"$set": {"type": "premium", "premium_since": now}},
            upsert=True,
        )

    def set_user_type(self, user_id: int, user_type: str) -> None:
        self.collection.update_one({"id": user_id}, {"$set": {"type": user_type}})

    def set_role(self, user_id: int, role: str) -> None:
        self.collection.update_one({"id": user_id}, {"$set": {"role": role}})

    def bump_notes_today(self, user_id: int) -> None:
        self.collection.update_one({"id": user_id}, {"$inc": {"notes_today": 1}})

    def bump_total_notes(self, user_id: int) -> None:
        self.collection.update_one({"id": user_id}, {"$inc": {"total_notes": 1}})

    def set_last_note_time(self, user_id: int, when: datetime | None = None) -> None:
        self.collection.update_one({"id": user_id}, {"$set": {"last_note_time": when or datetime.utcnow()}})

    def set_questions_per_note(self, user_id: int, value: int) -> None:
        self.collection.update_one({"id": user_id}, {"$set": {"questions_per_note": value}})

    def set_default_qtype(self, user_id: int, qtype: str) -> None:
        self.collection.update_one({"id": user_id}, {"$set": {"default_question_type": qtype}})

    def reset_notes_if_new_day(self, user_id: int) -> None:
        user = self.get(user_id)
        if not user:
            return
        last = user.get("last_note_time")
        if not last:
            return
        if isinstance(last, str):
            try:
                last = datetime.fromisoformat(last)
            except Exception:
                last = None
        if not last:
            return
        now = datetime.utcnow()
        if last.date() != now.date():
            self.collection.update_one({"id": user_id}, {"$set": {"notes_today": 0}})

    # Gemini API key management
    def set_gemini_api_key(self, user_id: int, api_key: str | None) -> None:
        update = {"$unset": {"gemini_api_key": ""}} if not api_key else {"$set": {"gemini_api_key": api_key}}
        self.collection.update_one({"id": user_id}, update, upsert=True)

    def get_gemini_api_key(self, user_id: int) -> str | None:
        doc = self.get(user_id) or {}
        key = doc.get("gemini_api_key")
        return key if isinstance(key, str) and key.strip() else None