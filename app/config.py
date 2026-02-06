from pydantic import BaseModel
from pydantic import Field
from pydantic import ValidationError
from pydantic import field_validator
from typing import List
import os
from dotenv import load_dotenv


load_dotenv()


class Config(BaseModel):
    bot_token: str = Field(default_factory=lambda: os.getenv("BOT_TOKEN", ""))
    mongo_uri: str = Field(default_factory=lambda: os.getenv("MONGO_URI", "mongodb://localhost:27017"))
    mongo_db: str = Field(default_factory=lambda: os.getenv("MONGO_DB", "quizbot"))
    gemini_api_key: str = Field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    owner_id: int = Field(default=1263404935)

    force_subscription: bool = Field(default_factory=lambda: os.getenv("FORCE_SUBSCRIPTION", "false").lower() == "true")
    force_channels: List[str] = Field(default_factory=lambda: [c.strip() for c in os.getenv("FORCE_CHANNELS", "").split(",") if c.strip()])

    max_notes_regular: int = Field(default_factory=lambda: int(os.getenv("MAX_NOTES_REGULAR", "5")))
    max_notes_premium: int = Field(default_factory=lambda: int(os.getenv("MAX_NOTES_PREMIUM", "10")))
    max_notes_custom_key: int = Field(default_factory=lambda: int(os.getenv("MAX_NOTES_CUSTOM_KEY", "50")))
    max_questions_regular: int = Field(default_factory=lambda: int(os.getenv("MAX_QUESTIONS_REGULAR", "100")))
    max_questions_premium: int = Field(default_factory=lambda: int(os.getenv("MAX_QUESTIONS_PREMIUM", "150")))
    max_questions_custom_key: int = Field(default_factory=lambda: int(os.getenv("MAX_QUESTIONS_CUSTOM_KEY", "300")))

    question_type_default: str = Field(default_factory=lambda: os.getenv("QUESTION_TYPE_DEFAULT", "text"))
    maintenance_mode: bool = Field(default_factory=lambda: os.getenv("MAINTENANCE_MODE", "false").lower() == "true")

    premium_price: int = Field(default_factory=lambda: int(os.getenv("PREMIUM_PRICE", "30")))
    payment_channel: str = Field(default_factory=lambda: os.getenv("PAYMENT_CHANNEL", ""))
    telebirr_numbers: List[str] = Field(default_factory=lambda: [c.strip() for c in os.getenv("TELEBIRR_NUMBERS", "").split(",") if c.strip()])
    cbe_numbers: List[str] = Field(default_factory=lambda: [c.strip() for c in os.getenv("CBE_NUMBERS", "").split(",") if c.strip()])

    @field_validator("question_type_default")
    @classmethod
    def validate_qtype(cls, v: str) -> str:
        v = (v or "text").lower()
        if v not in ("text", "poll"):
            return "text"
        return v


def get_config() -> Config:
    try:
        return Config()
    except ValidationError as exc:
        raise RuntimeError(f"Invalid configuration: {exc}")