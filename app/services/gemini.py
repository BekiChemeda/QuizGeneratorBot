from typing import List, Dict, Optional
import json
import base64
from google import genai
from google.genai import types
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

    try:
        client = genai.Client(api_key=api_key)
        
        safe_note = note or ""
        safe_title = topic_title or ""
        scope_hint = "You may use knowledge beyond the note if needed." if allow_beyond else "Use only the provided content; avoid unrelated facts."
        
        source_block = ""
        if title_only and safe_title:
            source_block = f"Title: {safe_title}\nNote:\n{safe_note}"
        elif safe_note:
            source_block = f"Note:\n{safe_note}"

        prompt_text = f"""
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

        contents = []
        if media_data and mime_type:
            # For media (audio/video), provide better context
            media_context = "Analyze the audio/video content below and generate questions based on what is discussed, explained, or presented in the media."
            if safe_title:
                media_context += f"\nContext: {safe_title}"
            
            contents.append(types.Content(
                parts=[
                    types.Part.from_bytes(data=media_data, mime_type=mime_type),
                    types.Part.from_text(text=f"{media_context}\n\n{prompt_text}")
                ]
            ))
        else:
             contents.append(prompt_text)

        # Use gemini-2.5-flash for all inputs (supports multimodal including audio)
        model_id = "gemini-2.5-flash"
        
        response = client.models.generate_content(
            model=model_id,
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        
        if not response.text:
            return []

        text = response.text.strip()
        
        # Robust JSON extraction
        try:
            start_idx = text.find('[')
            end_idx = text.rfind(']')
            if start_idx != -1 and end_idx != -1:
                cleaned = text[start_idx:end_idx+1]
                parsed = json.loads(cleaned)
            else:
                # Fallback to simple strip/replace
                cleaned = text.replace("```json", "").replace("```", "").strip()
                parsed = json.loads(cleaned)
        except (ValueError, json.JSONDecodeError):
            print(f"Failed to parse Gemini response: {text[:200]}")
            return []
        
        validated = [
            q
            for q in parsed
            if isinstance(q, dict)
            and all(k in q for k in ("question", "choices", "answer_index", "explanation"))
        ]
        return validated

    except Exception as e:
        print(f"Gemini API Error: {e}")
        return []

def validate_gemini_api_key(api_key: str) -> bool:
    """Validate a Gemini key with a minimal request."""
    api_key = (api_key or "").strip()
    if not api_key:
        return False
    
    try:
        client = genai.Client(api_key=api_key)
        client.models.generate_content(
            model="gemini-2.5-flash",
            contents="Return empty JSON array: []",
        )
        return True
    except Exception:
        return False