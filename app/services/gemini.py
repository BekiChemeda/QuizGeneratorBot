from typing import List, Dict, Optional
import requests
import json
from ..config import get_config
from ..repositories.users import UsersRepository
from ..db import get_db


def _choose_api_key(user_id: Optional[int]) -> Optional[str]:
    """Return user's own Gemini key if set; otherwise fallback to global, if any."""
    cfg = get_config()
    api_key = None
    try:
        if user_id is not None:
            db = get_db()
            api_key = UsersRepository(db).get_gemini_api_key(user_id)
    except Exception:
        api_key = None
    if not api_key:
        api_key = cfg.gemini_api_key or None
    return api_key

import base64

def generate_questions(
    note: str,
    num_questions: int = 5,
    *,
    user_id: Optional[int] = None,
    title_only: bool = False,
    allow_beyond: bool = False,
    topic_title: Optional[str] = None,
    difficulty: str = "Medium",
    media_data: Optional[bytes] = None,
    mime_type: Optional[str] = None,
    question_type: str = "multiple_choice"
) -> List[Dict]:
    api_key = _choose_api_key(user_id)
    if not api_key:
        return []

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}

    safe_note = (note or "").replace('"', '\\"')
    safe_title = (topic_title or "").replace('"', '\\"')
    scope_hint = "You may use knowledge beyond the note if needed." if allow_beyond else "Use only the provided content; avoid unrelated facts."
    
    source_block = ""
    if title_only and safe_title:
        source_block = f"Title: {safe_title}\nNote:\n```{safe_note}```"
    elif safe_note:
        source_block = f"Note:\n```{safe_note}```"

    prompt = f"""
Generate {num_questions} {question_type} questions from the provided study material.
Difficulty Level: {difficulty}
{scope_hint}

Respond in valid JSON array format. Each object must follow this format:
{{
  "question": "string",
  "choices": ["string", "string", "string", "string"],
  "answer_index": number (0-3),
  "explanation": "string (max 200 characters)"
}}
Rules:
- Only return the JSON array, with no surrounding text or code fences
- Each choice must be under 100 characters
- Ensure the correct answer index matches the choices array

Source:
{source_block}
""".strip()

    contents_parts = [{"text": prompt}]
    
    if media_data and mime_type:
        # Encode bytes to base64 string
        b64_data = base64.b64encode(media_data).decode("utf-8")
        contents_parts.append({
            "inline_data": {
                "mime_type": mime_type,
                "data": b64_data
            }
        })

    data = {"contents": [{"parts": contents_parts}]}

    try:
        response = requests.post(url, headers=headers, data=json.dumps(data), timeout=60)
        response.raise_for_status()
        payload = response.json()
        raw = payload["candidates"][0]["content"]["parts"][0]["text"]
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(cleaned)
        validated = [
            q
            for q in parsed
            if isinstance(q, dict)
            and all(k in q for k in ("question", "choices", "answer_index", "explanation"))
        ]
        return validated
    except Exception:
        return []


def validate_gemini_api_key(api_key: str) -> bool:
    """Validate a Gemini key with a minimal request. Avoid heavy usage."""
    api_key = (api_key or "").strip()
    if not api_key:
        return False
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}
    data = {"contents": [{"parts": [{"text": "Return JSON array: []"}]}]}
    try:
        r = requests.post(url, headers=headers, data=json.dumps(data), timeout=10)
        if r.status_code == 401 or r.status_code == 403:
            return False
        r.raise_for_status()
        return True
    except Exception:
        return False