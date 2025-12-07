import json
from html import unescape

def mock_gemini_response():
    # Simulated response with HTML entities
    return [
        {
            "question": "What is the capital of France? &quot;Paris&quot;",
            "choices": ["London", "Berlin", "Paris &amp; suburbs", "Madrid"],
            "answer_index": 2,
            "explanation": "Paris is the capital. &lt;i&gt;It's beautiful&lt;/i&gt;"
        }
    ]

def clean_response(parsed):
    # This is what I plan to implement
    cleaned = []
    for q in parsed:
        new_q = q.copy()
        new_q["question"] = unescape(q["question"])
        new_q["choices"] = [unescape(c) for c in q["choices"]]
        new_q["explanation"] = unescape(q.get("explanation", ""))
        cleaned.append(new_q)
    return cleaned

def test():
    raw = mock_gemini_response()
    print("Raw:", raw)
    cleaned = clean_response(raw)
    print("Cleaned:", cleaned)
    
    assert cleaned[0]["question"] == 'What is the capital of France? "Paris"'
    assert cleaned[0]["choices"][2] == 'Paris & suburbs'
    assert cleaned[0]["explanation"] == "Paris is the capital. <i>It's beautiful</i>"
    print("Test passed!")

if __name__ == "__main__":
    test()
