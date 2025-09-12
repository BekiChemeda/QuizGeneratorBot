from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime
from telebot import TeleBot
import time
from pymongo.database import Database
from ..services.gemini import generate_questions
from ..repositories.users import UsersRepository


class QuizScheduler:
    def __init__(self, db: Database, bot: TeleBot) -> None:
        self.db = db
        self.bot = bot
        self.schedules = db["schedules"]
        self.scheduler = BackgroundScheduler()

    def start(self) -> None:
        self.scheduler.add_job(self._tick, IntervalTrigger(seconds=5), max_instances=1, coalesce=True)
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def _tick(self) -> None:
        now = datetime.utcnow()
        due = list(self.schedules.find({"status": "pending", "scheduled_at": {"$lte": now}}).sort("scheduled_at", 1))
        for sched in due:
            try:
                note = sched.get("note", "")
                num = int(sched.get("num_questions", 5))
                qtype = (sched.get("question_type") or "text").lower()
                delay = max(5, min(60, int(sched.get("delay_seconds", 5))))
                target = sched.get("target_chat_id")
                user_id = int(sched.get("user_id"))

                questions = generate_questions(note, num, user_id=user_id)
                if not questions:
                    self.schedules.update_one({"_id": sched["_id"]}, {"$set": {"status": "failed"}})
                    continue

                letters = ["A", "B", "C", "D"]
                for idx, q in enumerate(questions, start=1):
                    time.sleep(delay)
                    if qtype == "text":
                        text = f"{idx}. {q['question']}\n"
                        for i, c in enumerate(q["choices"]):
                            prefix = letters[i] if i < len(letters) else str(i + 1)
                            text += f"{prefix}. {c}\n"
                        text += f"\nCorrect Answer: {letters[q['answer_index']]} - {q['choices'][q['answer_index']]}"
                        explanation = (q.get("explanation") or "")
                        if explanation:
                            text += f"\nExplanation: {explanation[:195]}"
                        self.bot.send_message(target, text)
                    else:
                        self.bot.send_poll(
                            target,
                            q["question"],
                            q["choices"],
                            type="quiz",
                            correct_option_id=q["answer_index"],
                            explanation=(q.get("explanation") or "")[:195],
                        )

                UsersRepository(self.db).bump_generations(user_id, 1)
                UsersRepository(self.db).bump_questions_generated(user_id, len(questions))
                self.schedules.update_one({"_id": sched["_id"]}, {"$set": {"status": "sent"}})
            except Exception:
                self.schedules.update_one({"_id": sched["_id"]}, {"$set": {"status": "failed"}})