from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from pymongo.database import Database


class UsersRepository:
    def __init__(self, db: Database) -> None:
        self.collection = db["users"]

    def get(self, user_id: int) -> Optional[Dict[str, Any]]:
        return self.collection.find_one({"id": user_id})

    def upsert_user(self, user_id: int, username: Optional[str]) -> Dict[str, Any]:
        now = datetime.now()
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
                "invited_by": None,
                "referral_count": 0,
            },
            "$set": {"username": username} if username else {},
        }
        # If username is not provided, ensure it gets set to None on insert
        if not username:
             update["$setOnInsert"]["username"] = None

        self.collection.update_one({"id": user_id}, update, upsert=True)
        return self.get(user_id) or {}

    def set_referrer(self, user_id: int, referrer_id: int) -> bool:
        """Sets the referrer for a user if not already set. Returns True if successful."""
        # Prevent self-referral
        if user_id == referrer_id:
            return False
            
        # Check if user already has a referrer
        user = self.get(user_id)
        if user and user.get("invited_by"):
            return False

        # Set referrer
        res = self.collection.update_one(
            {"id": user_id, "invited_by": None},
            {"$set": {"invited_by": referrer_id}}
        )
        
        if res.modified_count > 0:
            # Increment referrer's count
            self.collection.update_one({"id": referrer_id}, {"$inc": {"referral_count": 1}})
            return True
        return False
    
    def check_and_reward_referral_milestone(self, user_id: int, bot, settings_repo) -> bool:
        """
        Check if user reached a referral milestone and award premium.
        Returns True if milestone was reached and reward given.
        """
        user = self.get(user_id)
        if not user:
            return False
        
        referral_count = user.get("referral_count", 0)
        milestones_reached = user.get("referral_milestones_reached", [])
        
        # Get settings from DB or use defaults
        referral_target = settings_repo.get("referral_target", 10) if settings_repo else 10
        referral_reward_days = settings_repo.get("referral_reward_days", 15) if settings_repo else 15
        
        # Calculate current milestone (e.g., if target is 10: milestone 1 = 10, milestone 2 = 20, etc.)
        current_milestone = (referral_count // referral_target)
        
        # Check if this milestone hasn't been rewarded yet
        if current_milestone > 0 and current_milestone not in milestones_reached:
            # Award premium
            self.set_premium(user_id, referral_reward_days)
            
            # Mark milestone as reached
            milestones_reached.append(current_milestone)
            self.collection.update_one(
                {"id": user_id},
                {"$set": {"referral_milestones_reached": milestones_reached}}
            )
            
            # Notify user
            try:
                bot.send_message(
                    user_id,
                    f"🎉 **Congratulations!**\n\n"
                    f"You've invited {referral_count} users and reached milestone {current_milestone}!\n"
                    f"You've been awarded **{referral_reward_days} days of Premium**! 🌟\n\n"
                    f"Keep inviting to earn more rewards!",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
            
            return True
        
        return False

    def get_referral_count(self, user_id: int) -> int:
        user = self.get(user_id)
        return user.get("referral_count", 0) if user else 0

    def set_premium(self, user_id: int, duration_days: int | None = None) -> None:
        now = datetime.now()
        update: Dict[str, Any] = {"type": "premium", "premium_since": now}
        
        if duration_days:
            expiry = now + timedelta(days=duration_days)
            update["premium_until"] = expiry
        else:
            update["premium_until"] = None  # Permanent

        self.collection.update_one(
            {"id": user_id},
            {"$set": update},
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
        self.collection.update_one({"id": user_id}, {"$set": {"last_note_time": when or datetime.now()}})

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
        now = datetime.now()
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

    def set_admin(self, user_id: int) -> None:
        self.collection.update_one({"id": user_id}, {"$set": {"role": "admin"}})

    def revoke_admin(self, user_id: int) -> None:
        self.collection.update_one({"id": user_id}, {"$set": {"role": "user"}})

    # --- Pending Referral ---
    def set_pending_referrer(self, user_id: int, referrer_id: int) -> None:
        self.collection.update_one({"id": user_id}, {"$set": {"pending_referrer": referrer_id}}, upsert=True)

    def get_pending_referrer(self, user_id: int) -> Optional[int]:
        user = self.get(user_id)
        return user.get("pending_referrer") if user else None

    def clear_pending_referrer(self, user_id: int) -> None:
        self.collection.update_one({"id": user_id}, {"$unset": {"pending_referrer": ""}})

    # --- Analytics Aggregation ---
    def count_all(self) -> int:
        return self.collection.count_documents({})

    def count_premium(self) -> int:
        now = datetime.now()
        return self.collection.count_documents({
            "type": "premium",
            "$or": [
                {"premium_until": None},
                {"premium_until": {"$gt": now}},
                {"premium_until": {"$exists": False}},
            ]
        })

    def count_with_api_key(self) -> int:
        return self.collection.count_documents({
            "gemini_api_key": {"$exists": True, "$ne": None, "$ne": ""}
        })

    def count_admins(self) -> int:
        return self.collection.count_documents({"role": "admin"})

    def get_top_inviters(self, limit: int = 5) -> list:
        return list(
            self.collection.find(
                {"referral_count": {"$gt": 0}},
                {"id": 1, "username": 1, "referral_count": 1, "_id": 0}
            ).sort("referral_count", -1).limit(limit)
        )

    def count_active_today(self) -> int:
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return self.collection.count_documents({"last_note_time": {"$gte": today_start}})

    def count_active_week(self) -> int:
        week_ago = datetime.now() - timedelta(days=7)
        return self.collection.count_documents({"last_note_time": {"$gte": week_ago}})

    def count_new_today(self) -> int:
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return self.collection.count_documents({"registered_at": {"$gte": today_start}})

    def count_new_week(self) -> int:
        week_ago = datetime.now() - timedelta(days=7)
        return self.collection.count_documents({"registered_at": {"$gte": week_ago}})

    # --- Streak Management ---
    def update_streak(self, user_id: int) -> dict:
        user = self.get(user_id) or {}
        today = datetime.now().date()
        last_date = user.get("streak_last_date")

        current = user.get("streak_current", 0)
        best = user.get("streak_best", 0)

        if last_date:
            if isinstance(last_date, datetime):
                last_date = last_date.date()
            elif isinstance(last_date, str):
                try:
                    last_date = datetime.fromisoformat(last_date).date()
                except Exception:
                    last_date = None

        if last_date == today:
            return {"current": current, "best": best}
        elif last_date and (today - last_date).days == 1:
            current += 1
        else:
            current = 1

        best = max(best, current)
        self.collection.update_one({"id": user_id}, {"$set": {
            "streak_current": current,
            "streak_best": best,
            "streak_last_date": datetime.now(),
        }})
        return {"current": current, "best": best}

    def get_streak_info(self, user_id: int) -> dict:
        user = self.get(user_id) or {}
        return {
            "current": user.get("streak_current", 0),
            "best": user.get("streak_best", 0),
        }