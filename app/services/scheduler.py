from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime
from telebot import TeleBot
import time
from pymongo.database import Database
from ..services.gemini import generate_questions
from ..services.file_parser import chunk_text


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
        # Claim in small batches to avoid race conditions
        due = list(self.schedules.find({"status": "pending", "scheduled_at": {"$lte": now}}).sort("scheduled_at", 1).limit(10))
        for sched in due:
            # Try to atomically claim this schedule
            res = self.schedules.update_one({"_id": sched["_id"], "status": "pending"}, {"$set": {"status": "processing"}})
            if res.modified_count == 0:
                continue
            try:
                note = sched.get("note", "")
                title = sched.get("title")
                file_content = sched.get("file_content")
                num = int(sched.get("num_questions", 5))
                qtype = (sched.get("question_type") or "text").lower()
                delay = max(5, min(60, int(sched.get("delay_seconds", 5))))
                target = sched.get("target_chat_id")

                allow_beyond = bool(sched.get("allow_beyond", False))
                user_id = int(sched.get("user_id"))
                if file_content:
                    chunks = chunk_text(file_content, max_chars=3500)
                    per_chunk = max(1, num // max(1, len(chunks)))
                    questions = []
                    for idx, ch in enumerate(chunks):
                        if len(questions) >= num:
                            break
                        qbatch = generate_questions(ch, per_chunk, user_id=user_id, title_only=False, allow_beyond=True)
                        questions.extend(qbatch)
                    questions = questions[:num]
                elif title:
                    questions = generate_questions("", num, user_id=user_id, title_only=True, allow_beyond=True, topic_title=title)
                else:
                    questions = generate_questions(note, num, user_id=user_id, title_only=False, allow_beyond=allow_beyond)
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

                self.schedules.update_one({"_id": sched["_id"]}, {"$set": {"status": "sent"}})
                # Send summary to user
                try:
                    self.bot.send_message(user_id, f"✅ Scheduled quiz posted: {len(questions)} questions to {sched.get('target_label','PM')}")
                except Exception:
                    pass
            except Exception:
                self.schedules.update_one({"_id": sched["_id"]}, {"$set": {"status": "failed"}})