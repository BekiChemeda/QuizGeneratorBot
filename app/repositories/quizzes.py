from typing import List, Dict, Any, Optional
from datetime import datetime
from pymongo.database import Database
from bson import ObjectId

class QuizzesRepository:
    def __init__(self, db: Database) -> None:
        self.collection = db["quizzes"]

    def create(self, quiz_data: Dict[str, Any]) -> str:
        """
        Saves a quiz.
        quiz_data should contain: user_id, title, questions (list), created_at
        """
        if "created_at" not in quiz_data:
            quiz_data["created_at"] = datetime.now()
        result = self.collection.insert_one(quiz_data)
        return str(result.inserted_id)

    def get_user_quizzes(self, user_id: int, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Retrieves quizzes for a user, sorted by creation date (newest first).
        """
        cursor = self.collection.find({"user_id": user_id}).sort("created_at", -1)
        if limit:
            cursor = cursor.limit(limit)
        return list(cursor)

    def get_quiz(self, quiz_id: str) -> Optional[Dict[str, Any]]:
        try:
            return self.collection.find_one({"_id": ObjectId(quiz_id)})
        except Exception:
            return None
