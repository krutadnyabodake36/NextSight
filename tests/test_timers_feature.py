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
        time.sleep(0.2)
    return None

def test_timers_suite():
    print("--- 🧪 Starting Voice Reminders & Countdown Timers Validation Suite ---")
    
    # 0. Clean slate
    requests.post(f"{BASE_URL}/api/memory/clear")
    requests.post(f"{BASE_URL}/api/timers/clear")

    # 1. Test REST API: Create Timer
    print("\n1. Testing POST /api/timers (Create 15s Timer)...")
    res = requests.post(f"{BASE_URL}/api/timers", json={
        "duration_sec": 15,
        "label": "Espresso Shot",
        "is_reminder": False
    })
    assert res.status_code == 200, f"Expected 200, got {res.status_code}"
    data = res.json()
    assert data.get("status") == "success"
    t_obj = data.get("timer", {})
    t_id = t_obj.get("id")
    print(f"   Created Timer: ID={t_id} | Label={t_obj.get('label')} | TimeLeft={t_obj.get('time_left')}")
    assert t_id is not None
    assert t_obj.get("label") == "Espresso Shot"

    # 2. Test REST API: GET /api/timers
    print("\n2. Testing GET /api/timers...")
    res = requests.get(f"{BASE_URL}/api/timers")
    assert res.status_code == 200
    active = res.json().get("timers", [])
    assert len(active) == 1
    print(f"   Active Timer Found: {active[0]['label']} ({active[0]['time_left']} remaining)")

    # 3. Test REST API: DELETE /api/timers/{id}
    print(f"\n3. Testing DELETE /api/timers/{t_id} (Cancel Timer)...")
    res = requests.delete(f"{BASE_URL}/api/timers/{t_id}")
    assert res.status_code == 200
    active = res.json().get("timers", [])
    assert len(active) == 0
    print("   Timer cancelled successfully via REST API.")

    # 4. Test Voice Command: Create Countdown Timer
    print("\n4. Testing Voice Command: 'Set a timer for 25 seconds'...")
    c_len = get_chat_len()
    res = requests.post(f"{BASE_URL}/api/simulate/text-query", json={"text": "Set a timer for 25 seconds"})
    assert res.status_code == 200
    ai_msg = wait_for_assistant_response("timer set", start_index=c_len)
    assert ai_msg is not None, "Did not receive spoken confirmation for timer creation"
    print(f"   Spoken Confirmation: \"{ai_msg['text']}\"")

    # Verify Dashboard State has active_timers populated
    state = requests.get(f"{BASE_URL}/api/state").json()
    timers_in_state = state.get("active_timers", [])
    assert len(timers_in_state) >= 1
    print(f"   Dashboard State Synced: {len(timers_in_state)} active timer(s) in HUD state pill.")

    # 5. Test Voice Command: List Timers
    print("\n5. Testing Voice Command: 'What are my active timers?'...")
    time.sleep(0.5)
    c_len = get_chat_len()
    res = requests.post(f"{BASE_URL}/api/simulate/text-query", json={"text": "What are my active timers?"})
    assert res.status_code == 200
    ai_msg = wait_for_assistant_response("active", start_index=c_len)
    assert ai_msg is not None, "Did not receive spoken response for listing timers"
    print(f"   Spoken Response: \"{ai_msg['text']}\"")

    # 6. Test Voice Command: Reminder with Target Task and Automatic Expiration
    print("\n6. Testing Voice Command: 'Remind me in 3 seconds to drink water'...")
    time.sleep(0.5)
    c_len = get_chat_len()
    res = requests.post(f"{BASE_URL}/api/simulate/text-query", json={"text": "Remind me in 3 seconds to drink water"})
    assert res.status_code == 200
    ai_msg = wait_for_assistant_response("remind you", start_index=c_len)
    assert ai_msg is not None, "Did not receive spoken confirmation for reminder creation"
    print(f"   Spoken Confirmation: \"{ai_msg['text']}\"")

    # 7. Test Automatic Expiry & Spoken Alarm for the 3s Reminder
    print("\n7. Waiting for 3s reminder to expire and trigger spoken alarm...")
    reminder_fired_index = get_chat_len()
    alarm_msg = wait_for_assistant_response("Reminder:", start_index=c_len, timeout=6.0)
    assert alarm_msg is not None, "Did not receive automatic spoken reminder alert on expiry"
    print(f"   ⏰ Automatic Reminder Alarm Fired: \"{alarm_msg['text']}\"")

    # 8. Test Voice Command: Cancel Active Timer
    print("\n8. Testing Voice Command: 'Set a timer for 10 minutes' and 'Cancel all timers'...")
    time.sleep(0.5)
    c_len = get_chat_len()
    requests.post(f"{BASE_URL}/api/simulate/text-query", json={"text": "Set a timer for 10 minutes"})
    wait_for_assistant_response("timer set", start_index=c_len)

    # Cancel it while active
    c_len = get_chat_len()
    res = requests.post(f"{BASE_URL}/api/simulate/text-query", json={"text": "Cancel all timers"})
    assert res.status_code == 200
    ai_msg = wait_for_assistant_response("cancelled", start_index=c_len)
    assert ai_msg is not None, "Did not receive spoken confirmation for cancel timers"
    print(f"   Spoken Confirmation: \"{ai_msg['text']}\"")

    # Cancel again when empty
    c_len = get_chat_len()
    requests.post(f"{BASE_URL}/api/simulate/text-query", json={"text": "Cancel all timers"})
    empty_msg = wait_for_assistant_response("don't have any active timers", start_index=c_len)
    assert empty_msg is not None
    print(f"   Spoken Empty Confirmation: \"{empty_msg['text']}\"")

    # 9. Verify Empty State
    res = requests.get(f"{BASE_URL}/api/timers")
    assert len(res.json().get("timers", [])) == 0
    print("   Verified 0 timers remaining in system.")

    print("\n🎉 ALL VOICE REMINDERS & COUNTDOWN TIMERS TESTS PASSED!")

if __name__ == "__main__":
    test_timers_suite()
