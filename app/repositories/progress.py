from typing import List, Dict, Any, Optional
from datetime import datetime
from pymongo.database import Database


class ProgressRepository:
    def __init__(self, db: Database) -> None:
        self.collection = db["progress"]

    def record_quiz_attempt(self, user_id: int, quiz_id: str, score: int, total: int, topic: str = "") -> str:
        accuracy = round((score / total) * 100, 1) if total > 0 else 0
        doc = {
            "user_id": user_id,
            "quiz_id": quiz_id,
            "score": score,
            "total": total,
            "accuracy": accuracy,
            "topic": topic,
            "created_at": datetime.now(),
        }
        result = self.collection.insert_one(doc)
        return str(result.inserted_id)

    def get_user_stats(self, user_id: int) -> Dict[str, Any]:
        attempts = list(self.collection.find({"user_id": user_id}))
        if not attempts:
            return {
                "total_quizzes": 0,
                "total_questions": 0,
                "total_correct": 0,
                "avg_accuracy": 0,
                "best_accuracy": 0,
                "best_topic": "N/A",
            }

        total_quizzes = len(attempts)
        total_questions = sum(a.get("total", 0) for a in attempts)
        total_correct = sum(a.get("score", 0) for a in attempts)
        avg_accuracy = round((total_correct / total_questions) * 100, 1) if total_questions > 0 else 0

        best_attempt = max(attempts, key=lambda x: x.get("accuracy", 0))
        best_accuracy = best_attempt.get("accuracy", 0)

        # Best topic by average accuracy
        topic_scores: Dict[str, list] = {}
        for a in attempts:
            t = a.get("topic", "").strip()
            if t:
                topic_scores.setdefault(t, []).append(a.get("accuracy", 0))

        best_topic = "N/A"
        if topic_scores:
            best_topic = max(topic_scores, key=lambda t: sum(topic_scores[t]) / len(topic_scores[t]))

        return {
            "total_quizzes": total_quizzes,
            "total_questions": total_questions,
            "total_correct": total_correct,
            "avg_accuracy": avg_accuracy,
            "best_accuracy": best_accuracy,
            "best_topic": best_topic,
        }
