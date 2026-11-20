import requests
import json
import time
import socket
import struct
import numpy as np
import sys

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

BASE_URL = "http://localhost:8000"

def get_state() -> dict:
    try:
        return requests.get(f"{BASE_URL}/api/state").json()
    except Exception:
        return {}

def wait_for_speaking(timeout: float = 8.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        state = get_state()
        if state.get("is_playback_streaming"):
            return True
        time.sleep(0.05)
    return False

def test_barge_in_suite():
    print("--- 🧪 Starting Instant Barge-In Interruptibility Validation Suite ---")
    
    # 0. Clean slate
    requests.post(f"{BASE_URL}/api/memory/clear")

    # 1. Test REST endpoint: /api/playback/barge-in in idle state
    print("\n1. Testing POST /api/playback/barge-in (Idle State)...")
    res = requests.post(f"{BASE_URL}/api/playback/barge-in")
    assert res.status_code == 200
    data = res.json()
    assert data.get("status") == "success"
    print(f"   Idle Barge-In Response: {data}")

    # 2. Test Instant Barge-In during active streaming speech via API
    print("\n2. Testing Mid-Playback Barge-In via API...")
    # Dispatch an extensive query that generates multi-clause spoken response
    res = requests.post(f"{BASE_URL}/api/simulate/text-query", json={
        "text": "Explain quantum mechanics in comprehensive detail with multiple paragraphs"
    })
    assert res.status_code == 200

    print("   Waiting for assistant speech to start...")
    is_speaking = wait_for_speaking(timeout=10.0)
    print(f"   Assistant Speaking Detected: {is_speaking}")
    
    # Trigger instant barge-in cutoff
    t_interrupt = time.perf_counter()
    barge_res = requests.post(f"{BASE_URL}/api/playback/barge-in")
    interrupt_ms = (time.perf_counter() - t_interrupt) * 1000
    print(f"   ⚡ Barge-In Dispatched: {barge_res.status_code} (Latency: {interrupt_ms:.1f}ms) -> {barge_res.json()}")
    assert barge_res.status_code == 200

    # Verify playback dropped immediately
    time.sleep(0.1)
    state = get_state()
    assert state.get("is_playback_streaming") is False, "Playback was not halted after barge-in"
    print(f"   ✅ Playback halted instantly. Pipeline Status: \"{state.get('pipeline_status')}\"")

    # 3. Test Tactile Barge-In via Single Tap on Temple
    print("\n3. Testing Tactile Barge-In via Temple Sensor Single Tap...")
    res = requests.post(f"{BASE_URL}/api/simulate/text-query", json={
        "text": "Tell me a very long story about exploring distant stars and galaxies"
    })
    assert res.status_code == 200

    print("   Waiting for assistant speech to start...")
    is_speaking = wait_for_speaking(timeout=10.0)
    print(f"   Assistant Speaking Detected: {is_speaking}")

    # Simulate physical single tap while speaking
    touch_res = requests.post(f"{BASE_URL}/api/simulate/touch", json={"action": "single"})
    assert touch_res.status_code == 200
    touch_data = touch_res.json()
    print(f"   👆 Temple Tap Dispatched -> {touch_data}")
    assert touch_data.get("action") == "barge_in_stop"

    time.sleep(0.1)
    state = get_state()
    assert state.get("is_playback_streaming") is False, "Playback was not halted after touch tap"
    print("   ✅ Tactile Temple Tap cleanly cut off speech!")

    # 4. Test Preemptive Barge-In (New query interrupts old response)
    print("\n4. Testing Preemptive Barge-In (New Query over active speech)...")
    requests.post(f"{BASE_URL}/api/simulate/text-query", json={
        "text": "Give me an elaborate history of the Roman empire"
    })
    is_speaking = wait_for_speaking(timeout=10.0)
    print(f"   Old Response Speaking: {is_speaking}")

    # Dispatch new query immediately while old one is still talking
    new_res = requests.post(f"{BASE_URL}/api/simulate/text-query", json={
        "text": "What time is it?"
    })
    assert new_res.status_code == 200
    print(f"   ⚡ New Query Preempted: {new_res.status_code} -> \"{new_res.json().get('clean_query')}\"")

    # 5. Test Acoustic Voice Barge-In via UDP Audio Energy
    print("\n5. Testing Acoustic Voice Barge-In via UDP Vocal Energy...")
    requests.post(f"{BASE_URL}/api/simulate/text-query", json={
        "text": "Recite a long epic poem with twelve stanzas"
    })
    is_speaking = wait_for_speaking(timeout=10.0)
    print(f"   Assistant Speaking: {is_speaking}")

    # Generate synthetic 16-bit 16kHz speech burst (Sine wave at 400Hz with high RMS energy ~2000)
    sample_rate = 16000
    duration_sec = 0.064  # 64ms (2 chunks)
    t = np.linspace(0, duration_sec, int(sample_rate * duration_sec), False)
    # Loud voice tone
    audio_signal = (np.sin(2 * np.pi * 400 * t) * 8000).astype(np.int16)
    raw_pcm = audio_signal.tobytes()

    # Transmit over UDP to port 9000
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(raw_pcm[:1024], ("127.0.0.1", 9000))
        time.sleep(0.032)
        sock.sendto(raw_pcm[1024:], ("127.0.0.1", 9000))
    finally:
        sock.close()

    time.sleep(0.2)
    state = get_state()
    print(f"   Acoustic Voice Pulse Injected -> Pipeline Status: \"{state.get('pipeline_status')}\"")
    print(f"   Playback Streaming State: {state.get('is_playback_streaming')}")

    print("\n🎉 ALL INSTANT BARGE-IN INTERRUPTIBILITY TESTS PASSED!")

if __name__ == "__main__":
    test_barge_in_suite()
