import requests
import json
import time
import sys

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

BASE_URL = "http://localhost:8000"

def get_chat_len() -> int:
    try:
        return len(requests.get(f"{BASE_URL}/api/state").json().get("chat_history", []))
    except Exception:
        return 0

def wait_for_assistant_response(expected_substring: str, start_index: int = 0, timeout: float = 15.0) -> dict | None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            res = requests.get(f"{BASE_URL}/api/state").json()
            chat = res.get("chat_history", [])
            for m in chat[start_index:]:
                if m.get("role") == "assistant" and expected_substring.lower() in m.get("text", "").lower():
                    return m
        except Exception:
            pass
        time.sleep(0.4)
    return None

def test_notes_suite():
    print("--- 🧪 Starting Wearable Notes & Scratchpad Validation Suite ---")

    # 1. Clear memory and notes to start from clean slate
    requests.post(f"{BASE_URL}/api/memory/clear")
    res = requests.post(f"{BASE_URL}/api/notes/clear")
    print(f"1. Clear Notes API: {res.status_code} -> {res.json()}")

    # 2. Simulate Voice Note Creation
    c_len = get_chat_len()
    voice_query = "Take a note buy 1.75mm PLA filament for smart glasses frame"
    res = requests.post(f"{BASE_URL}/api/simulate/text-query", json={"text": voice_query})
    print(f"2. Voice Command Dispatched: {res.status_code}")

    # Wait for assistant confirmation
    ai_msg = wait_for_assistant_response("note saved", start_index=c_len)
    assert ai_msg is not None, "Did not receive spoken confirmation for note saved"
    print(f"   Spoken Confirmation: \"{ai_msg['text']}\"")

    # 3. Verify Notes via GET /api/notes
    res = requests.get(f"{BASE_URL}/api/notes")
    notes = res.json().get("notes", [])
    print(f"3. Retrieved Notes from GET /api/notes: Count = {len(notes)}")
    assert len(notes) == 1, f"Expected 1 note, got {len(notes)}"
    saved_note = notes[0]
    print(f"   Note Entry: id={saved_note['id']} | date={saved_note['date']} | time={saved_note['time']}")
    print(f"   Note Content: \"{saved_note['text']}\"")
    assert "buy 1.75mm pla filament" in saved_note['text'].lower()

    # 4. Simulate Adding Note via Web UI Endpoint
    res = requests.post(f"{BASE_URL}/api/notes", json={"text": "Calibrate INMP441 microphone gain to 18"})
    print(f"4. UI Add Note: {res.status_code} -> {res.json().get('status')}")

    res = requests.get(f"{BASE_URL}/api/notes")
    notes = res.json().get("notes", [])
    assert len(notes) == 2, f"Expected 2 notes, got {len(notes)}"
    print(f"   Total notes now: {len(notes)}")

    # 5. Simulate Reading Notes by Voice
    c_len = get_chat_len()
    res = requests.post(f"{BASE_URL}/api/simulate/text-query", json={"text": "What are my notes?"})
    print(f"5. Voice Read Command Dispatched: {res.status_code}")

    ai_msg = wait_for_assistant_response("you have 2 notes", start_index=c_len)
    assert ai_msg is not None, "Did not receive spoken notes list"
    print(f"   Assistant Read Notes Aloud: \"{ai_msg['text']}\"")

    # 6. Simulate Deleting Single Note
    to_delete = notes[0]["id"]
    res = requests.delete(f"{BASE_URL}/api/notes/{to_delete}")
    print(f"6. Deleted Note {to_delete}: {res.status_code} -> {res.json().get('status')}")

    res = requests.get(f"{BASE_URL}/api/notes")
    notes_after = res.json().get("notes", [])
    assert len(notes_after) == 1, f"Expected 1 note remaining, got {len(notes_after)}"
    print(f"   Notes remaining: {len(notes_after)}")

    # 7. Simulate Voice Clear Notes Command
    c_len = get_chat_len()
    res = requests.post(f"{BASE_URL}/api/simulate/text-query", json={"text": "Clear all notes"})
    print(f"7. Voice Clear Command Dispatched: {res.status_code}")

    ai_msg = wait_for_assistant_response("cleared", start_index=c_len)
    assert ai_msg is not None, "Did not receive spoken confirmation for notes cleared"
    print(f"   Assistant Spoken Confirmation: \"{ai_msg['text']}\"")

    res = requests.get(f"{BASE_URL}/api/notes")
    notes_final = res.json().get("notes", [])
    assert len(notes_final) == 0, f"Expected 0 notes after voice clear, got {len(notes_final)}"
    print(f"   Notes after voice clear: {len(notes_final)} (Empty)")

    print("\n🎉 ALL WEARABLE NOTES & SCRATCHPAD SUITE TESTS PASSED!")

if __name__ == "__main__":
    test_notes_suite()
