from typing import List, Dict, Any, Optional
from datetime import datetime
from pymongo.database import Database
from bson import ObjectId


class BattlesRepository:
    def __init__(self, db: Database) -> None:
        self.collection = db["battles"]

    def create_battle(self, challenger_id: int, quiz_id: str, challenger_score: int, challenger_total: int) -> str:
        doc = {
            "challenger_id": challenger_id,
            "opponent_id": None,
            "quiz_id": quiz_id,
            "challenger_score": challenger_score,
            "challenger_total": challenger_total,
            "opponent_score": None,
            "opponent_total": None,
            "status": "waiting",
            "created_at": datetime.now(),
        }
        result = self.collection.insert_one(doc)
        return str(result.inserted_id)

    def get_battle(self, battle_id: str) -> Optional[Dict[str, Any]]:
        try:
            return self.collection.find_one({"_id": ObjectId(battle_id)})
        except Exception:
            return None

    def set_opponent_score(self, battle_id: str, opponent_id: int, score: int, total: int) -> None:
        try:
            self.collection.update_one(
                {"_id": ObjectId(battle_id)},
                {"$set": {
                    "opponent_id": opponent_id,
                    "opponent_score": score,
                    "opponent_total": total,
                    "status": "completed",
                }}
            )
        except Exception:
            pass

    def get_user_battles(self, user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        return list(
            self.collection.find(
                {"$or": [{"challenger_id": user_id}, {"opponent_id": user_id}]}
            ).sort("created_at", -1).limit(limit)
        )

    def count_user_wins(self, user_id: int) -> int:
        # Won as challenger
        wins_c = self.collection.count_documents({
            "challenger_id": user_id,
            "status": "completed",
            "$expr": {"$gt": ["$challenger_score", "$opponent_score"]}
        })
        # Won as opponent
        wins_o = self.collection.count_documents({
            "opponent_id": user_id,
            "status": "completed",
            "$expr": {"$gt": ["$opponent_score", "$challenger_score"]}
        })
        return wins_c + wins_o
