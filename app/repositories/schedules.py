from typing import Dict, Any, List, Optional
from pymongo.database import Database
from datetime import datetime
from bson import ObjectId


class SchedulesRepository:
    def __init__(self, db: Database) -> None:
        self.collection = db["schedules"]

    def create(self, schedule: Dict[str, Any]) -> str:
        res = self.collection.insert_one(schedule)
        return str(res.inserted_id)

    def set_status(self, schedule_id: Any, status: str) -> None:
        self.collection.update_one({"_id": schedule_id}, {"$set": {"status": status}})

    def delete(self, user_id: int, sched_id: str) -> bool:
        try:
            oid = ObjectId(sched_id)
        except Exception:
            return False
        res = self.collection.delete_one({"_id": oid, "user_id": user_id})
        return res.deleted_count > 0

    def due_schedules(self, now: datetime) -> List[Dict[str, Any]]:
        return list(
            self.collection.find({"status": "pending", "scheduled_at": {"$lte": now}}).sort("scheduled_at", 1)
        )

    def get_user_schedules(self, user_id: int) -> List[Dict[str, Any]]:
        # Only show upcoming (pending and future) schedules
        now = datetime.now()
        return list(
            self.collection.find({
                "user_id": user_id, 
                "status": "pending",
                "scheduled_at": {"$gt": now}
            }).sort("scheduled_at", 1)
        )
