from typing import Any, Dict
from pymongo.database import Database


class StatsRepository:
    def __init__(self, db: Database) -> None:
        self.collection = db["stats"]

    def get(self, key: str, default: Any | None = None) -> Any:
        doc = self.collection.find_one({"key": key})
        if not doc:
            return default
        return doc.get("value", default)

    def set(self, key: str, value: Any) -> None:
        self.collection.update_one({"key": key}, {"$set": {"value": value}}, upsert=True)

    def incr(self, key: str, amount: int = 1) -> None:
        self.collection.update_one({"key": key}, {"$inc": {"value": amount}}, upsert=True)

