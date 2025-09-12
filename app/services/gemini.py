from typing import List, Dict, Optional, Iterable
import requests
import json
from ..config import get_config
from ..db import get_db
from ..repositories.users import UsersRepository


def _resolve_api_key(user_id: Optional[int]) -> Optional[str]:
    cfg = get_config()
    if user_id is None:
        return cfg.gemini_api_key or None
    db = get_db()
    repo = UsersRepository(db)
    personal = repo.get_personal_key(user_id)
    return personal or (cfg.gemini_api_key or None)

def _gemini_generate(api_key: str, prompt: str) -> List[Dict]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}
    data = {"contents": [{"parts": [{"text": prompt}]}]}

    try:
        response = requests.post(url, headers=headers, data=json.dumps(data), timeout=60)
        response.raise_for_status()
        raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
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


def build_prompt_from_note(note: str, num_questions: int, allow_beyond: bool = False) -> str:
    safe_note = (note or "").replace('"', '\\"')
    scope_line = "The content should be strictly limited to the note below." if not allow_beyond else "You may include relevant information beyond the note if helpful, but prioritize the note content."
    prompt = f"""
Generate {num_questions} multiple choice questions.
{scope_line}
Respond in valid JSON array format. Each object must follow this format:
{{
  "question": "string",
  "choices": ["string", "string", "string", "string"],
  "answer_index": number (0-3),
  "explanation": "string (max 200 characters)"
}}
Only return the JSON array, nothing else. Even if the instruction asks to add text, do not do it.
Each choice must be under 100 characters.
Source content:
```{safe_note}```
""".strip()
    return prompt


def build_prompt_from_title(title: str, num_questions: int) -> str:
    safe_title = (title or "").strip().replace('"', '\\"')
    prompt = f"""
Generate {num_questions} multiple choice questions using the following topic title.
You may include broadly relevant information; aim for accurate, mainstream knowledge.
Respond in valid JSON array only with objects:
{{
  "question": "string",
  "choices": ["string", "string", "string", "string"],
  "answer_index": number (0-3),
  "explanation": "string (max 200 characters)"
}}
Return only JSON array.
Title: {safe_title}
""".strip()
    return prompt


def generate_questions(note: str, num_questions: int = 5, *, user_id: Optional[int] = None, allow_beyond: bool = False, title_only: bool = False) -> List[Dict]:
    cfg = get_config()
    api_key = _resolve_api_key(user_id)
    if not api_key:
        return []
    if title_only:
        prompt = build_prompt_from_title(note, num_questions)
    else:
        prompt = build_prompt_from_note(note, num_questions, allow_beyond=allow_beyond)
    return _gemini_generate(api_key, prompt)


def validate_gemini_key(api_key: str) -> bool:
    try:
        # Minimal test call with a tiny prompt
        prompt = "Return JSON array with one object: {\"question\":\"Q?\",\"choices\":[\"A\",\"B\",\"C\",\"D\"],\"answer_index\":0,\"explanation\":\"\"}"
        result = _gemini_generate(api_key, prompt)
        return isinstance(result, list) and len(result) >= 1
    except Exception:
        return False


def generate_questions_chunked(text_chunks: Iterable[str], per_chunk_questions: int, *, user_id: Optional[int] = None, allow_beyond: bool = False) -> List[Dict]:
    all_questions: List[Dict] = []
    for chunk in text_chunks:
        if not chunk:
            continue
        qs = generate_questions(chunk, per_chunk_questions, user_id=user_id, allow_beyond=allow_beyond, title_only=False)
        if qs:
            all_questions.extend(qs)
        if len(all_questions) >= per_chunk_questions:
            break
    return all_questions[: per_chunk_questions]