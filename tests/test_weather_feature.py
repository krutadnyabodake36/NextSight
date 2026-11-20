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
        time.sleep(0.3)
    return None

def test_weather_suite():
    print("--- 🧪 Starting On-Demand Live Weather & Forecast Validation Suite ---")
    requests.post(f"{BASE_URL}/api/memory/clear")

    # 1. Test REST endpoint: Local Default Weather
    print("\n1. Testing GET /api/weather (Local Default)...")
    res = requests.get(f"{BASE_URL}/api/weather")
    assert res.status_code == 200, f"Expected 200, got {res.status_code}"
    data = res.json()
    assert data.get("status") == "success", f"Failed: {data}"
    w = data.get("weather", {})
    print(f"   City: {w.get('city')}, {w.get('country')}")
    print(f"   Temp: {w.get('temperature')}°C (Feels like: {w.get('apparent_temperature')}°C)")
    print(f"   Condition: {w.get('condition')} (Icon: {w.get('icon')})")
    print(f"   High: {w.get('temp_max')}°C | Low: {w.get('temp_min')}°C | Rain: {w.get('rain_probability')}%")
    print(f"   Spoken Summary: \"{w.get('summary')}\"")
    assert w.get("city") is not None
    assert isinstance(w.get("temperature"), (int, float))

    # 2. Test REST endpoint: Specific City (Tokyo)
    print("\n2. Testing GET /api/weather?city=Tokyo...")
    res = requests.get(f"{BASE_URL}/api/weather?city=Tokyo")
    assert res.status_code == 200
    w_tokyo = res.json().get("weather", {})
    print(f"   Tokyo: {w_tokyo.get('temperature')}°C, {w_tokyo.get('condition')} (Rain: {w_tokyo.get('rain_probability')}%)")
    assert "tokyo" in w_tokyo.get("city", "").lower()

    # 3. Simulate Voice Query: General Local Weather
    print("\n3. Testing Voice Query: 'What is the weather like?'...")
    c_len = get_chat_len()
    res = requests.post(f"{BASE_URL}/api/simulate/text-query", json={"text": "What is the weather like?"})
    assert res.status_code == 200

    ai_msg = wait_for_assistant_response("degrees", start_index=c_len)
    assert ai_msg is not None, "Did not receive spoken weather response"
    print(f"   Assistant Spoke Aloud: \"{ai_msg['text']}\"")

    # 4. Verify Dashboard State has latest_weather populated
    res = requests.get(f"{BASE_URL}/api/state").json()
    state_weather = res.get("latest_weather")
    assert state_weather is not None, "latest_weather not populated in dashboard_state"
    print(f"   Dashboard Weather Pill Synced: {state_weather.get('city')} {state_weather.get('temperature')}°C • {state_weather.get('condition')}")

    # 5. Simulate Voice Query: Rain Specific Inquiry
    print("\n5. Testing Voice Query: 'Will it rain today?'...")
    time.sleep(1.0) # Brief spacing between speech playbacks
    c_len = get_chat_len()
    res = requests.post(f"{BASE_URL}/api/simulate/text-query", json={"text": "Will it rain today?"})
    assert res.status_code == 200

    ai_msg = wait_for_assistant_response("rain", start_index=c_len)
    assert ai_msg is not None, "Did not receive spoken rain forecast response"
    print(f"   Assistant Spoke Aloud: \"{ai_msg['text']}\"")

    # 6. Simulate Voice Query: City-Specific Weather ("What is the weather in Tokyo?")
    print("\n6. Testing Voice Query: 'What is the weather in Tokyo?'...")
    time.sleep(1.0)
    c_len = get_chat_len()
    res = requests.post(f"{BASE_URL}/api/simulate/text-query", json={"text": "What is the weather in Tokyo?"})
    assert res.status_code == 200

    ai_msg = wait_for_assistant_response("tokyo", start_index=c_len)
    assert ai_msg is not None, "Did not receive spoken response for Tokyo weather"
    print(f"   Assistant Spoke Aloud: \"{ai_msg['text']}\"")

    # Verify latest_weather updated to Tokyo
    res = requests.get(f"{BASE_URL}/api/state").json()
    assert "tokyo" in res.get("latest_weather", {}).get("city", "").lower()
    print(f"   Dashboard Updated to Tokyo: {res['latest_weather']['city']} {res['latest_weather']['temperature']}°C")

    # 7. Simulate Voice Query: Temperature Inquiry ("What is the temperature outside?")
    print("\n7. Testing Voice Query: 'What is the temperature outside?'...")
    time.sleep(1.0)
    c_len = get_chat_len()
    res = requests.post(f"{BASE_URL}/api/simulate/text-query", json={"text": "What is the temperature outside?"})
    assert res.status_code == 200

    ai_msg = wait_for_assistant_response("temperature", start_index=c_len)
    assert ai_msg is not None, "Did not receive spoken temperature response"
    print(f"   Assistant Spoke Aloud: \"{ai_msg['text']}\"")

    print("\n🎉 ALL ON-DEMAND LIVE WEATHER & FORECAST TESTS PASSED!")

if __name__ == "__main__":
    test_weather_suite()
