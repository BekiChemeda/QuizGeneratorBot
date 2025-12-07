from typing import Dict, Any, List
from pymongo.database import Database
from datetime import datetime


class PaymentsRepository:
    def __init__(self, db: Database) -> None:
        self.collection = db["payments"]

    def insert(self, user_id: int, method: str, amount: int, photo_file_id: str | None) -> None:
        doc = {
            "user_id": user_id,
            "method": method,
            "amount": amount,
            "photo_file_id": photo_file_id,
            "status": "pending",
            "time": datetime.now(),
        }
        self.collection.insert_one(doc)

    def update_status(self, user_id: int, status: str) -> None:
        self.collection.update_many({"user_id": user_id, "status": "pending"}, {"$set": {"status": status}})

    def list_pending(self) -> List[Dict[str, Any]]:
        return list(self.collection.find({"status": "pending"}).sort("time", -1))