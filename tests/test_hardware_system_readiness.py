import socket
import struct
import time
import json
import requests
import asyncio
import websockets
import numpy as np
import sys
from PIL import Image
import io

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

BASE_HTTP_URL = "http://localhost:8000"
WS_VISION_URL = "ws://localhost:8000/ws/vision"
UDP_MIC_PORT = 9000
UDP_PLAYBACK_PORT = 9001

def create_synthetic_jpeg() -> bytes:
    """Generates a small valid JPEG in memory to simulate ESP32 camera frame."""
    img = Image.new("RGB", (320, 240), color=(30, 45, 75))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()

async def test_websocket_camera_stream():
    """Simulates ESP32 sending binary JPEG frames over WebSocket /ws/vision."""
    print("\n--- 1. Testing ESP32 WebSocket Camera Uplink (/ws/vision) ---")
    jpeg_data = create_synthetic_jpeg()
    print(f"   Simulated Camera Frame Size: {len(jpeg_data)} bytes")
    
    async with websockets.connect(WS_VISION_URL) as ws:
        print("   ✅ WebSocket connected successfully to /ws/vision")
        # Send binary frame exactly like firmware.ino: webSocket.sendBIN(fb->buf, fb->len)
        await ws.send(jpeg_data)
        print("   ✅ Sent binary JPEG frame over WebSocket")
        await asyncio.sleep(0.3)
        
    # Check server state
    state = requests.get(f"{BASE_HTTP_URL}/api/state").json()
    assert state.get("last_image_bytes", 0) > 0, "Server did not register incoming camera frame"
    print(f"   ✅ Server Dashboard Synced: last_image_bytes={state.get('last_image_bytes')}, last_image_time={state.get('last_image_time')}")

def test_touch_sensor_events():
    """Simulates physical TTP223 touch sensor taps from ESP32 OneButton."""
    print("\n--- 2. Testing ESP32 Touch Sensor HTTP Webhooks ---")
    
    # 1. Single Tap (Toggle Vision Mode)
    res_single = requests.post(f"{BASE_HTTP_URL}/api/touch/single")
    assert res_single.status_code == 200
    print(f"   Single Tap -> {res_single.json()}")
    
    # 2. Double Tap (Snapshot)
    res_double = requests.post(f"{BASE_HTTP_URL}/api/touch/double")
    assert res_double.status_code == 200
    print(f"   Double Tap -> {res_double.json()}")
    
    # 3. Long Press (Clear Memory)
    res_long = requests.post(f"{BASE_HTTP_URL}/api/touch/long")
    assert res_long.status_code == 200
    print(f"   Long Press -> {res_long.json()}")

def test_udp_mic_and_speaker_downlink():
    """Simulates full-duplex ESP32: sending UDP mic packets and receiving UDP speaker packets."""
    print("\n--- 3. Testing Full-Duplex UDP Audio Channels (Mic: 9000, Speaker: 9001) ---")
    
    # 1. Start a local UDP socket on 9001 pretending to be the ESP32 MAX98357A speaker receiver
    speaker_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    speaker_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    speaker_sock.bind(("0.0.0.0", UDP_PLAYBACK_PORT))
    speaker_sock.settimeout(4.0)

    # 2. Simulate ESP32 sending a UDP audio packet to port 9000 to register its IP address
    mic_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    
    # Generate 512 samples of 16-bit PCM silence/ambient
    pcm_ambient = (np.zeros(512, dtype=np.int16)).tobytes()
    mic_sock.sendto(pcm_ambient, ("127.0.0.1", UDP_MIC_PORT))
    print("   ✅ Sent registration UDP packet from simulated glasses to port 9000")

    # 3. Trigger a speech response on the server
    print("   Triggering assistant audio response...")
    requests.post(f"{BASE_HTTP_URL}/api/simulate/text-query", json={"text": "Say ready"})
    
    # 4. Listen on port 9001 for streamed audio packets from the server
    received_packets = 0
    total_bytes = 0
    t0 = time.time()
    try:
        while time.time() - t0 < 15.0:
            try:
                data, addr = speaker_sock.recvfrom(2048)
                if data:
                    received_packets += 1
                    total_bytes += len(data)
                    if received_packets >= 5:
                        break
            except socket.timeout:
                break
    finally:
        speaker_sock.close()
        mic_sock.close()

    print(f"   ✅ ESP32 Speaker UDP Channel verified: Received {received_packets} audio packets ({total_bytes} bytes) on port 9001!")
    assert received_packets > 0, "No UDP audio packets were received on port 9001 from server"

def test_ip_binding_and_lan_reachability():
    """Checks if server is bound to 0.0.0.0 so physical Wi-Fi devices can reach it."""
    print("\n--- 4. Checking Network Interface & Port Bindings ---")
    import subprocess
    
    output = subprocess.check_output(["netstat", "-an"], text=True)
    found_8000 = False
    found_9000 = False
    found_9001 = False

    for line in output.splitlines():
        if "0.0.0.0:8000" in line and "LISTENING" in line:
            found_8000 = True
        elif "0.0.0.0:9000" in line:
            found_9000 = True
        elif "0.0.0.0:9001" in line:
            found_9001 = True

    print(f"   Port 8000 (HTTP/WS): {'✅ Bound to 0.0.0.0 (LAN Accessible)' if found_8000 else '❌ Not found'}")
    print(f"   Port 9000 (UDP Mic): {'✅ Bound to 0.0.0.0 (LAN Accessible)' if found_9000 else '❌ Not found'}")
    print(f"   Port 9001 (Playback): {'✅ Bound to 0.0.0.0 (LAN Accessible)' if found_9001 else '❌ Not found'}")

    assert found_8000, "Port 8000 is not listening on 0.0.0.0"
    assert found_9000, "Port 9000 UDP is not bound"

def run_hardware_readiness_suite():
    print("===================================================================")
    print("🚀 NEXTSIGHT HARDWARE PRE-CONNECTION SYSTEM READINESS SUITE")
    print("===================================================================")
    
    # 1. WebSocket Vision Stream
    asyncio.run(test_websocket_camera_stream())
    
    # 2. Touch Sensor Endpoints
    test_touch_sensor_events()
    
    # 3. Full Duplex UDP Audio
    test_udp_mic_and_speaker_downlink()
    
    # 4. Port Bindings
    test_ip_binding_and_lan_reachability()
    
    print("\n===================================================================")
    print("🎉 ALL PRE-HARDWARE SYSTEM FLOW TESTS PASSED SUCCESSFULLY!")
    print("===================================================================")

if __name__ == "__main__":
    run_hardware_readiness_suite()
