import asyncio
import io
import json
import os
import sys
import threading
import time
import wave
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
import urllib.parse
import requests

# Prevent Windows console charmap UnicodeEncodeErrors when printing emojis
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


# Persistent Server Log file capture (captures all prints & errors for diagnosis)
class TeeLogger:
    def __init__(self, original_stream, log_filepath):
        self.original_stream = original_stream
        self.log_filepath = log_filepath

    def write(self, data):
        self.original_stream.write(data)
        self.original_stream.flush()
        try:
            with open(self.log_filepath, "a", encoding="utf-8", errors="replace") as f:
                f.write(data)
        except Exception:
            pass

    def flush(self):
        self.original_stream.flush()

    def isatty(self):
        return getattr(self.original_stream, "isatty", lambda: False)()

    def reconfigure(self, **kwargs):
        if hasattr(self.original_stream, "reconfigure"):
            self.original_stream.reconfigure(**kwargs)


# Wrap streams safely once
if not isinstance(sys.stdout, TeeLogger):
    sys.stdout = TeeLogger(sys.stdout, "server.log")
if not isinstance(sys.stderr, TeeLogger):
    sys.stderr = TeeLogger(sys.stderr, "server.log")

import struct
import traceback
import av
import cv2
import edge_tts
import numpy as np
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from faster_whisper import WhisperModel
import google.generativeai as genai
from PIL import Image
import openwakeword
from openwakeword.model import Model

# Load environment variables from .env
load_dotenv()

# Ensure storage directories exist
os.makedirs("captured_frames", exist_ok=True)
os.makedirs("captured_frames/gallery", exist_ok=True)
os.makedirs("static", exist_ok=True)
os.makedirs("audio_utterances", exist_ok=True)
os.makedirs("audio_responses", exist_ok=True)
os.makedirs("cache", exist_ok=True)

# STT & TTS Configuration (Optimized base.en on CPU int8 with 6 threads & 2 workers)
WHISPER_MODEL_NAME = "base.en"
WHISPER_INITIAL_PROMPT = "NextSight smart glasses AI assistant voice query."
TTS_VOICE = "en-US-JennyNeural"  # Fast, natural neural voice

# Gemini LLM Configuration (Current generation Gemini 3.5 Flash Lite)
GEMINI_MODEL_NAME = os.getenv("GEMINI_MODEL_NAME", "gemini-3.5-flash-lite")
AI_SYSTEM_PROMPT = (
    "You are NextSight, an intelligent, up-to-date AI voice assistant built into wearable smart glasses. "
    "You answer questions directly, naturally, and concisely in 1 to 2 spoken sentences. "
    "Never use markdown formatting, bullet points, asterisks, emojis, or code blocks because "
    "your response will be spoken directly into the user's ear through their smart glasses speaker. "
    "If an image from the glasses camera is attached, answer with direct awareness of what the user is looking at."
)

VISION_TRIGGER_WORDS = [
    "look", "see", "front", "holding", "reading", "read", "sign", "what is this",
    "what are these", "who is", "color", "colour", "describe", "picture", "camera", "view"
]

# Initialize Gemini API Configuration
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if GEMINI_API_KEY:
    try:
        os.environ["GOOGLE_API_KEY"] = GEMINI_API_KEY
        genai.configure(api_key=GEMINI_API_KEY)
        print(f"✨ [LLM] Google Gemini API configured with model '{GEMINI_MODEL_NAME}'")
    except Exception as e:
        print(f"⚠️ [LLM] Failed to configure Gemini API: {e}")
else:
    print("ℹ️ [LLM] GEMINI_API_KEY not found in environment or .env. Add it to enable full intelligence.")

# Playback Configuration (Downlink to ESP32 MAX98357A Speaker)
PLAYBACK_TCP_PORT = 9001
PLAYBACK_UDP_PORT = 9001
active_playback_clients = set()
is_playback_streaming = False
pipeline_lock = asyncio.Lock()
last_esp32_ip = None

# Zero-Disk Vision RAM Buffer: maintains the most recent enhanced frame directly in RAM
latest_enhanced_frame_bytes: bytes | None = None
latest_raw_frame_bytes: bytes | None = None
latest_frame_timestamp: float = 0.0

# Unified Audio Interaction State Machine
audio_state: str = "IDLE"  # "IDLE" (monitoring openwakeword) or "ACTIVE_LISTENING" (recording speech for VAD/Whisper)
active_listening_start: float = 0.0
ACTIVE_LISTEN_TIMEOUT: float = 8.0  # Seconds to wait for speech before reverting to IDLE
vision_mode_active: bool = False
chat_history = []
latest_image_url = "/captured_frames/enhanced_frame.jpg"

latest_latency = {
    "vad_to_stt": 0.0,
    "llm_first_token": 0.0,
    "full_llm": 0.0,
    "tts_first_chunk": 0.0,
    "total": 0.0
}


def _load_existing_gallery() -> list:
    gallery_dir = os.path.join("captured_frames", "gallery")
    os.makedirs(gallery_dir, exist_ok=True)
    imgs = []
    for f in sorted(os.listdir(gallery_dir), reverse=True):
        if f.lower().endswith((".jpg", ".jpeg", ".png")):
            imgs.append(f"/captured_frames/gallery/{f}")
    return imgs


gallery_images = _load_existing_gallery()

NOTES_FILE = "notes.json"
notes_lock = threading.Lock()


def load_notes() -> list:
    """Thread-safe loading of persistent notes from disk."""
    if not os.path.exists(NOTES_FILE):
        return []
    try:
        with open(NOTES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ [Notes] Error reading {NOTES_FILE}: {e}")
        return []


def save_note_entry(text: str) -> dict | None:
    """Thread-safe creation of a new note."""
    cleaned = text.strip()
    if not cleaned:
        return None
    now = datetime.now()
    entry = {
        "id": f"note_{int(now.timestamp() * 1000)}",
        "text": cleaned,
        "time": now.strftime("%I:%M %p"),
        "date": now.strftime("%Y-%m-%d"),
    }
    with notes_lock:
        notes = load_notes()
        notes.insert(0, entry)  # Most recent first
        try:
            with open(NOTES_FILE, "w", encoding="utf-8") as f:
                json.dump(notes, f, indent=2, ensure_ascii=False)
            print(f"📝 [Notes] Saved note: \"{cleaned}\"")
        except Exception as e:
            print(f"⚠️ [Notes] Error saving note: {e}")
    return entry


def delete_note_entry(note_id: str) -> bool:
    """Thread-safe deletion of a note by id."""
    with notes_lock:
        notes = load_notes()
        initial_len = len(notes)
        notes = [n for n in notes if n.get("id") != note_id]
        if len(notes) < initial_len:
            try:
                with open(NOTES_FILE, "w", encoding="utf-8") as f:
                    json.dump(notes, f, indent=2, ensure_ascii=False)
                print(f"🗑️ [Notes] Deleted note {note_id}")
                return True
            except Exception as e:
                print(f"⚠️ [Notes] Error deleting note: {e}")
    return False


def clear_all_notes_entries() -> bool:
    """Thread-safe clearing of all notes."""
    with notes_lock:
        try:
            with open(NOTES_FILE, "w", encoding="utf-8") as f:
                json.dump([], f, indent=2)
            print("🧹 [Notes] All notes cleared.")
            return True
        except Exception as e:
            print(f"⚠️ [Notes] Error clearing notes: {e}")
            return False


# --- On-Demand Live Weather & Forecast Subsystem (Open-Meteo) ---

DEFAULT_LOCATION = {
    "city": "Mumbai",
    "country": "India",
    "lat": 19.0760,
    "lon": 72.8777
}

WMO_CODES = {
    0: ("Clear sky", "sun"),
    1: ("Mainly clear", "cloud-sun"),
    2: ("Partly cloudy", "cloud-sun"),
    3: ("Overcast", "cloud"),
    45: ("Foggy", "cloud-fog"),
    48: ("Foggy", "cloud-fog"),
    51: ("Light drizzle", "cloud-drizzle"),
    53: ("Drizzle", "cloud-drizzle"),
    55: ("Dense drizzle", "cloud-drizzle"),
    61: ("Slight rain", "cloud-rain"),
    63: ("Moderate rain", "cloud-rain"),
    65: ("Heavy rain", "cloud-rain"),
    71: ("Light snow", "snowflake"),
    73: ("Snow", "snowflake"),
    75: ("Heavy snow", "snowflake"),
    80: ("Rain showers", "cloud-rain"),
    81: ("Rain showers", "cloud-rain"),
    82: ("Heavy rain showers", "cloud-rain"),
    95: ("Thunderstorm", "cloud-lightning"),
    96: ("Thunderstorm with hail", "cloud-lightning"),
    99: ("Severe thunderstorm", "cloud-lightning"),
}


def init_default_location():
    """Initializes default coordinates from env or IP geocoding."""
    global DEFAULT_LOCATION
    custom_city = os.getenv("DEFAULT_CITY")
    if custom_city:
        try:
            geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(custom_city)}&count=1&language=en&format=json"
            geo_res = requests.get(geo_url, timeout=3).json()
            results = geo_res.get("results")
            if results:
                DEFAULT_LOCATION = {
                    "city": results[0]["name"],
                    "country": results[0].get("country", ""),
                    "lat": results[0]["latitude"],
                    "lon": results[0]["longitude"]
                }
                print(f"📍 [Weather] Default location configured: {DEFAULT_LOCATION['city']}, {DEFAULT_LOCATION['country']}")
                return
        except Exception:
            pass
    try:
        ip_res = requests.get("http://ip-api.com/json/", timeout=2).json()
        if ip_res.get("city") and ip_res.get("lat") and ip_res.get("lon"):
            DEFAULT_LOCATION = {
                "city": ip_res.get("city"),
                "country": ip_res.get("country", ""),
                "lat": ip_res.get("lat"),
                "lon": ip_res.get("lon")
            }
            print(f"📍 [Weather] Auto-detected local position: {DEFAULT_LOCATION['city']}, {DEFAULT_LOCATION['country']}")
    except Exception:
        print(f"ℹ️ [Weather] Default location set to: {DEFAULT_LOCATION['city']}")


init_default_location()


def fetch_live_weather(city: str | None = None) -> dict:
    """Thread-safe on-demand weather retrieval from Open-Meteo."""
    try:
        lat = DEFAULT_LOCATION["lat"]
        lon = DEFAULT_LOCATION["lon"]
        display_name = DEFAULT_LOCATION["city"]
        country = DEFAULT_LOCATION["country"]

        if city and city.strip():
            cleaned_city = city.strip()
            geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(cleaned_city)}&count=1&language=en&format=json"
            geo_res = requests.get(geo_url, timeout=4).json()
            results = geo_res.get("results")
            if results:
                lat = results[0]["latitude"]
                lon = results[0]["longitude"]
                display_name = results[0]["name"]
                country = results[0].get("country", "")
            else:
                return {"error": f"Could not find coordinates for '{cleaned_city}'."}

        forecast_url = (
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,weather_code,wind_speed_10m"
            f"&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=auto"
        )
        res = requests.get(forecast_url, timeout=5).json()
        current = res.get("current", {})
        daily = res.get("daily", {})

        w_code = current.get("weather_code", 0)
        cond_text, icon_name = WMO_CODES.get(w_code, ("Fair", "sun"))
        is_day = current.get("is_day", 1)
        if not is_day and icon_name == "sun":
            icon_name = "moon"
        elif not is_day and icon_name == "cloud-sun":
            icon_name = "cloud-moon"

        temp = round(current.get("temperature_2m", 0.0), 1)
        feels = round(current.get("apparent_temperature", temp), 1)
        humidity = current.get("relative_humidity_2m", 0)
        wind = round(current.get("wind_speed_10m", 0.0), 1)

        daily_max = daily.get("temperature_2m_max", [temp])
        daily_min = daily.get("temperature_2m_min", [temp])
        daily_rain = daily.get("precipitation_probability_max", [0])

        temp_max = round(daily_max[0], 1) if daily_max else temp
        temp_min = round(daily_min[0], 1) if daily_min else temp
        rain_prob = daily_rain[0] if daily_rain else 0

        summary = (
            f"Currently in {display_name}, it is {temp}°C and {cond_text.lower()} "
            f"with {humidity}% humidity. Today's high is {temp_max}°C with a {rain_prob}% chance of rain."
        )

        return {
            "city": display_name,
            "country": country,
            "latitude": lat,
            "longitude": lon,
            "temperature": temp,
            "apparent_temperature": feels,
            "humidity": humidity,
            "wind_speed": wind,
            "condition": cond_text,
            "icon": icon_name,
            "is_day": bool(is_day),
            "temp_max": temp_max,
            "temp_min": temp_min,
            "rain_probability": rain_prob,
            "updated_at": datetime.now().strftime("%I:%M %p"),
            "summary": summary
        }
    except Exception as e:
        print(f"⚠️ [Weather Error]: {e}")
        return {"error": str(e)}


# --- Active Voice Reminders & Countdown Timers Engine ---

active_timers: dict[str, dict] = {}
active_timer_tasks: dict[str, asyncio.Task] = {}
timers_lock = threading.RLock()


def get_active_timers_list() -> list:
    """Returns active timers sorted by earliest expiration, with updated remaining seconds."""
    now = time.time()
    res = []
    with timers_lock:
        for tid, t in list(active_timers.items()):
            remaining = max(0, int(t["expires_at"] - now))
            t["remaining_sec"] = remaining
            mins = remaining // 60
            secs = remaining % 60
            if remaining >= 60:
                t["time_left"] = f"{mins:02d}:{secs:02d}"
            else:
                t["time_left"] = f"{secs}s"
            res.append(dict(t))
    return sorted(res, key=lambda x: x["expires_at"])


async def _run_timer_task(timer_id: str, duration_sec: int, label: str, is_reminder: bool):
    """Background async worker that sleeps for duration_sec then triggers chime and spoken alert."""
    try:
        await asyncio.sleep(duration_sec)
        with timers_lock:
            if timer_id not in active_timers:
                return  # Cancelled
            del active_timers[timer_id]
            active_timer_tasks.pop(timer_id, None)
            dashboard_state["active_timers"] = get_active_timers_list()

        # Formulate natural spoken notification
        if is_reminder:
            announcement = f"Reminder: {label}."
        else:
            if duration_sec >= 60:
                mins = duration_sec // 60
                secs = duration_sec % 60
                dur_str = f"{mins} minute" if secs == 0 else f"{mins} minute {secs} second"
            else:
                dur_str = f"{duration_sec} second"
            announcement = f"Time's up! Your {dur_str} timer has finished."

        print(f"⏰ [Timer Alert]: \"{announcement}\"")

        # Update chat history immediately so HUD displays alert bubble
        ai_msg = {
            "id": f"msg_a_{int(datetime.now().timestamp()*1000)}",
            "role": "assistant",
            "text": f"⏰ {announcement}",
            "time": datetime.now().strftime("%H:%M:%S"),
            "audio_url": None,
            "has_vision": False
        }
        chat_history.append(ai_msg)
        dashboard_state["pipeline_status"] = f"Alert: {announcement}"

        pcm_bytes, wav_bytes = await synthesize_speech(announcement)
        if wav_bytes and pcm_bytes:
            await asyncio.gather(
                play_audio_locally(wav_bytes),
                stream_audio_to_glasses(pcm_bytes, last_esp32_ip)
            )
    except asyncio.CancelledError:
        print(f"⏹️ [Timer Task Cancelled]: {timer_id}")
    except Exception as e:
        print(f"❌ [Timer Error]: {e}")


def create_timer(duration_sec: int, label: str = "Timer", is_reminder: bool = False) -> dict:
    """Spawns an asynchronous timer task."""
    timer_id = f"timer_{int(time.time()*1000)}"
    now = time.time()
    t_info = {
        "id": timer_id,
        "type": "reminder" if is_reminder else "timer",
        "label": label,
        "duration_sec": duration_sec,
        "remaining_sec": duration_sec,
        "created_at": now,
        "expires_at": now + duration_sec,
        "time_left": f"{duration_sec // 60:02d}:{duration_sec % 60:02d}" if duration_sec >= 60 else f"{duration_sec}s"
    }
    with timers_lock:
        active_timers[timer_id] = t_info
        dashboard_state["active_timers"] = get_active_timers_list()

    loop = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        pass
    if loop and loop.is_running():
        task = loop.create_task(_run_timer_task(timer_id, duration_sec, label, is_reminder))
        active_timer_tasks[timer_id] = task

    print(f"⏳ [Timer Created]: {label} ({duration_sec}s, ID: {timer_id})")
    return t_info


def cancel_timer(timer_id: str) -> bool:
    """Cancels a specific active timer."""
    with timers_lock:
        if timer_id in active_timers:
            task = active_timer_tasks.pop(timer_id, None)
            if task:
                task.cancel()
            del active_timers[timer_id]
            dashboard_state["active_timers"] = get_active_timers_list()
            print(f"🗑️ [Timer Cancelled]: {timer_id}")
            return True
    return False


def cancel_all_timers() -> int:
    """Cancels all active timers."""
    with timers_lock:
        count = len(active_timers)
        for tid, task in list(active_timer_tasks.items()):
            task.cancel()
        active_timer_tasks.clear()
        active_timers.clear()
        dashboard_state["active_timers"] = []
        print(f"🧹 [All Timers Cancelled]: {count} timers cleared")
        return count


dashboard_state = {
    "audio_state": audio_state,
    "vision_mode_active": vision_mode_active,
    "latest_latency": latest_latency,
    "latest_image_url": "/captured_frames/enhanced_frame.jpg",
    "latest_frame_url": "/captured_frames/enhanced_frame.jpg",
    "latest_raw_url": "/captured_frames/raw_frame.jpg",
    "latest_raw_frame": "/captured_frames/raw_frame.jpg",
    "latest_enhanced_frame": "/captured_frames/enhanced_frame.jpg",
    "last_image_time": None,
    "last_image_bytes": 0,
    "chat_history": chat_history,
    "gallery_images": gallery_images,
    "notes": load_notes(),
    "latest_weather": None,
    "active_timers": [],
    "esp32_ip": None,
    "is_playback_streaming": False,
    "pipeline_status": "Ready",
    "model_name": GEMINI_MODEL_NAME,
    "noise_floor": 450.0,
}

# High-Efficiency Whisper Initialization: base.en, int8, 6 threads, 2 workers
print(f"⏳ Loading Faster-Whisper model ('{WHISPER_MODEL_NAME}' on CPU int8, 6 threads, 2 workers)...")
whisper_model = WhisperModel(WHISPER_MODEL_NAME, device="cpu", compute_type="int8", cpu_threads=6, num_workers=2)
print(f"✅ Faster-Whisper model ('{WHISPER_MODEL_NAME}') loaded successfully!")

# Dedicated OpenWakeWord Engine (Pre-trained "alexa", "hey_mycroft", "hey_jarvis")
print("⏳ Loading OpenWakeWord pre-trained models ('alexa', 'hey_mycroft', 'hey_jarvis')...")
try:
    wakeword_model = Model(wakeword_models=["alexa", "hey_mycroft", "hey_jarvis"], inference_framework="onnx")
    print("✅ OpenWakeWord models loaded successfully!")
except Exception as e:
    print(f"⚠️ Falling back to default framework for OpenWakeWord: {e}")
    wakeword_model = Model(wakeword_models=["alexa", "hey_mycroft", "hey_jarvis"])
print(f"✨ [Wake Word] Active models: {list(wakeword_model.models.keys())}")


# --- LLM Reasoning, Vision Support & Snapshots ---

def enhance_image_rgb(raw_image_path: str = "captured_frames/raw_frame.jpg") -> str | None:
    """Reads raw incoming JPEG from RAM/disk, applies an unsharp mask to sharpen it, slightly boosts
    contrast and saturation while strictly keeping it in full RGB color, and saves to gallery."""
    global latest_raw_frame_bytes
    try:
        img_bgr = None
        if latest_raw_frame_bytes:
            img_bgr = cv2.imdecode(np.frombuffer(latest_raw_frame_bytes, np.uint8), cv2.IMREAD_COLOR)

        if img_bgr is None:
            if not os.path.exists(raw_image_path):
                print(f"⚠️ [Snapshot] Raw image not found: {raw_image_path}")
                return None
            img_bgr = cv2.imread(raw_image_path)

        if img_bgr is None:
            print(f"⚠️ [Snapshot] Failed to decode raw image: {raw_image_path}")
            return None

        # 1. Unsharp Mask for Crisp Edges (strictly preserved in color)
        blurred = cv2.GaussianBlur(img_bgr, (0, 0), sigmaX=3.0)
        sharpened = cv2.addWeighted(img_bgr, 1.5, blurred, -0.5, 0)

        # 2. Boost Contrast & Saturation in HSV space
        hsv = cv2.cvtColor(sharpened, cv2.COLOR_BGR2HSV).astype(np.float32)
        hsv[:, :, 1] = np.clip(hsv[:, :, 1] * 1.25, 0, 255)  # 25% saturation boost
        hsv[:, :, 2] = np.clip(1.06 * hsv[:, :, 2] + 4, 0, 255)  # slight contrast & brightness boost
        enhanced_bgr = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)

        # 3. Save to captured_frames/gallery/
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        gallery_dir = os.path.join("captured_frames", "gallery")
        os.makedirs(gallery_dir, exist_ok=True)
        filename = f"snapshot_{timestamp}.jpg"
        save_path = os.path.join(gallery_dir, filename)
        cv2.imwrite(save_path, enhanced_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 95])

        web_url = f"/captured_frames/gallery/{filename}"
        print(f"📸 [Snapshot Gallery] Saved RGB enhanced photo: {save_path} -> {web_url}")
        return web_url
    except Exception as e:
        print(f"❌ [Snapshot Error]: {e}\n{traceback.format_exc()}")
        return None


def get_latest_camera_frame(max_age_seconds: float = 20.0) -> str | None:
    """Finds the most recent camera frame in captured_frames/ if taken recently, prioritizing OCR-enhanced frames."""
    try:
        enhanced = os.path.join("captured_frames", "enhanced_frame.jpg")
        if os.path.exists(enhanced):
            age = datetime.now().timestamp() - os.path.getmtime(enhanced)
            if age <= max_age_seconds:
                return enhanced

        frames = [
            os.path.join("captured_frames", f)
            for f in os.listdir("captured_frames")
            if f.endswith(".jpg")
        ]
        if not frames:
            return None
        latest = max(frames, key=os.path.getmtime)
        age = datetime.now().timestamp() - os.path.getmtime(latest)
        if age <= max_age_seconds:
            return latest
    except Exception:
        pass
    return None


def build_gemini_prompt(user_text: str, history: list, has_image: bool = False) -> str:
    """Builds a rich, temporally-grounded, multi-turn conversational prompt for Gemini."""
    now_dt = datetime.now()
    now_str = now_dt.strftime("%A, %B %d, %Y at %I:%M %p")

    system_instruction = (
        f"You are NextSight, an intelligent wearable AI voice assistant built into the user's smart glasses.\n"
        f"• Real-Time Grounding: Current local date and time is {now_str}.\n"
        f"• Hardware Context: You speak directly into the user's ear via a micro-speaker on the right temple of the glasses frame. "
        f"The glasses have an integrated camera facing forward and a directional microphone.\n"
        f"• Spoken Audio Rules: Answer directly, conversationally, and concisely in 1 to 2 spoken sentences. "
        f"Never use markdown formatting, bolding, bullet points, asterisks, emojis, URLs, or code blocks because your response is converted directly to speech.\n"
        f"• Conversational Memory & Continuity: Maintain smooth continuity across turns. If the user uses pronouns or asks follow-up questions ('it', 'he', 'that', 'why', 'who', 'how much'), reference the recent dialogue history to understand their intent."
    )

    if has_image:
        system_instruction += (
            "\n• Vision Context: A live camera frame from the glasses is attached. Answer with direct awareness of what the user is looking at from a first-person perspective."
        )

    # Multi-turn context: take up to last 6 turns (excluding the current user_text turn which is last)
    recent_turns = [m for m in history[:-1] if m.get("role") in ("user", "assistant") and m.get("text")][-6:]
    conversation_block = ""
    if recent_turns:
        history_lines = []
        for m in recent_turns:
            speaker = "User" if m["role"] == "user" else "NextSight"
            history_lines.append(f"{speaker}: {m['text']}")
        conversation_block = "Recent Conversation History:\n" + "\n".join(history_lines) + "\n\n"

    return f"{system_instruction}\n\n{conversation_block}User: {user_text}\nNextSight:"


async def stream_gemini_clauses_async(contents: list, timing_meta: dict | None = None):
    """Async generator streaming Gemini output, tracking first token arrival,
    and yielding complete clauses split on punctuation marks (., ?, !, ,)."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        yield "Please configure your Gemini API key in the dot env file."
        return

    # Punctuation marks requested: '.', '?', '!', ','
    PUNCTUATION = {'.', '?', '!', ',', ';', '\n'}
    buffer = ""
    first_token_marked = False
    t_start = timing_meta.get("t_llm_start", time.perf_counter()) if timing_meta else time.perf_counter()

    models_to_try = [GEMINI_MODEL_NAME, "gemini-3.5-flash-lite", "gemini-3.8-flash"]
    seen = set()

    for m_name in models_to_try:
        if m_name in seen:
            continue
        seen.add(m_name)
        try:
            os.environ["GOOGLE_API_KEY"] = api_key
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(m_name)
            response = await asyncio.wait_for(
                model.generate_content_async(contents, stream=True),
                timeout=30.0
            )
            
            async for chunk in response:
                try:
                    text_frag = chunk.text
                except Exception:
                    continue

                if not text_frag:
                    continue

                if not first_token_marked:
                    first_token_marked = True
                    if timing_meta is not None:
                        timing_meta["llm_first_token"] = time.perf_counter() - t_start

                buffer += text_frag

                while True:
                    earliest_idx = -1
                    for i, ch in enumerate(buffer):
                        if ch in PUNCTUATION:
                            # Skip decimal numbers like 3.5 or 1.5
                            if ch == '.' and i > 0 and i < len(buffer) - 1 and buffer[i-1].isdigit() and buffer[i+1].isdigit():
                                continue
                            earliest_idx = i
                            break

                    if earliest_idx == -1:
                        break

                    clause = buffer[:earliest_idx + 1].strip()
                    buffer = buffer[earliest_idx + 1:]

                    if any(c.isalnum() for c in clause):
                        yield clause

            if timing_meta is not None:
                timing_meta["full_llm"] = time.perf_counter() - t_start
            break # Stream finished successfully

        except Exception as e:
            err_str = str(e)
            print(f"⚠️ [Gemini Async Stream Error with {m_name}]: {e}")
            if (
                isinstance(e, (asyncio.TimeoutError, TimeoutError))
                or "404" in err_str
                or "503" in err_str
                or "504" in err_str
                or "not found" in err_str.lower()
                or "unavailable" in err_str.lower()
                or "deadline" in err_str.lower()
                or "timeout" in err_str.lower()
            ):
                continue
            break

    remaining = buffer.strip()
    if any(c.isalnum() for c in remaining):
        yield remaining


async def generate_ai_response(transcribed_text: str, image_path: str | None = None) -> str:
    """Queries Google Gemini Multimodal API with transcribed speech and the latest camera frame.
    Uses zero-disk RAM vision buffer when available."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ℹ️ [LLM] GEMINI_API_KEY not configured. Falling back to placeholder echo.")
        return f"I heard you say: {transcribed_text}. Please add your Gemini API key in the dot env file."

    payload_image = latest_enhanced_frame_bytes if vision_mode_active else None
    pil_img = None
    image_attached = False

    if payload_image:
        now_ts = datetime.now().timestamp()
        if now_ts - latest_frame_timestamp <= 20.0:
            try:
                decoded_img = Image.open(io.BytesIO(payload_image))
                if decoded_img.width > 640 or decoded_img.height > 640:
                    decoded_img.thumbnail((640, 640), Image.Resampling.LANCZOS)
                pil_img = decoded_img
                image_attached = True
                print(f"👁️ [Gemini Multimodal] Attached frame from Zero-Disk RAM buffer ({pil_img.size})")
            except Exception as img_err:
                print(f"⚠️ [Gemini Vision] RAM buffer decode failed: {img_err}")
    else:
        print("⚡ [Vision Mode OFF] Strictly text-only inference active.")

    full_prompt = build_gemini_prompt(transcribed_text, chat_history, has_image=image_attached)
    contents = [full_prompt]
    if image_attached and pil_img is not None:
        contents.append(pil_img)

    # Non-blocking model call in worker thread
    def _call_gemini():
        os.environ["GOOGLE_API_KEY"] = api_key
        genai.configure(api_key=api_key)
        models_to_try = [GEMINI_MODEL_NAME, "gemini-flash-latest", "gemini-3.5-flash-lite"]
        seen = set()
        for m_name in models_to_try:
            if m_name in seen:
                continue
            seen.add(m_name)
            try:
                model = genai.GenerativeModel(m_name)
                response = model.generate_content(contents)
                return response.text.strip() if response.text else "I couldn't generate a response."
            except Exception as e:
                err_str = str(e)
                if "404" in err_str or "not found" in err_str.lower() or "no longer available" in err_str.lower():
                    continue
                raise e
        return "I could not reach an active Gemini model."

    try:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        answer = await loop.run_in_executor(None, _call_gemini)
        elapsed_ms = (loop.time() - t0) * 1000
        print(f"🧠 [Gemini LLM] Response in {elapsed_ms:.0f}ms: \"{answer}\"")
        return answer
    except Exception as e:
        print(f"❌ [LLM Error] Gemini request failed: {e}\n{traceback.format_exc()}")
        return f"I heard you say {transcribed_text}, but I had trouble reaching the AI service."


# --- Completely In-Memory Audio Synthesis & Playback (Zero Disk I/O) ---

async def synthesize_clause_pcm(clause_text: str) -> tuple[bytes | None, bytes | None]:
    """Uses edge-tts to synthesize speech entirely in memory.
    Returns (raw_pcm_bytes, wav_bytes) with zero disk I/O."""
    try:
        communicate = edge_tts.Communicate(clause_text, TTS_VOICE)
        mp3_bytes = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_bytes.extend(chunk["data"])

        if not mp3_bytes:
            return None, None

        # Decode MP3 to 16kHz mono PCM in worker thread using PyAV in memory
        def _decode_mp3():
            container = av.open(io.BytesIO(bytes(mp3_bytes)))
            resampler = av.AudioResampler(format='s16', layout='mono', rate=16000)
            pcm = bytearray()
            for frame in container.decode(audio=0):
                for rf in resampler.resample(frame):
                    pcm.extend(rf.to_ndarray().tobytes())
            container.close()
            return bytes(pcm)

        loop = asyncio.get_running_loop()
        pcm_bytes = await loop.run_in_executor(None, _decode_mp3)

        # Wrap in in-memory WAV container for local playback
        wav_buf = io.BytesIO()
        with wave.open(wav_buf, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(pcm_bytes)

        return pcm_bytes, wav_buf.getvalue()
    except Exception as e:
        print(f"❌ [TTS Synthesis Error]: {e}")
        return None, None


async def synthesize_speech(response_text: str) -> tuple[bytes | None, bytes | None]:
    """Synthesizes speech entirely in memory and saves a background copy for web HUD."""
    pcm_bytes, wav_bytes = await synthesize_clause_pcm(response_text)
    if wav_bytes:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        final_wav = f"audio_responses/response_{timestamp}.wav"
        def _bg_save():
            try:
                with open(final_wav, "wb") as f:
                    f.write(wav_bytes)
            except Exception:
                pass
        threading.Thread(target=_bg_save, daemon=True).start()
    return pcm_bytes, wav_bytes


# --- ESP32 Speaker Playback Downlink (TCP/UDP Port 9001) ---

async def handle_playback_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer = writer.get_extra_info('peername')
    print(f"🔗 [Playback] ESP32 Speaker connected from {peer}")
    active_playback_clients.add(writer)
    try:
        while True:
            data = await reader.read(100)
            if not data:
                break
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
    finally:
        active_playback_clients.discard(writer)
        print(f"🔌 [Playback] ESP32 Speaker disconnected: {peer}")


# --- Instant Barge-In Interruptibility Subsystem ---
barge_in_event = threading.Event()
active_ai_pipeline_task: asyncio.Task | None = None
active_playback_task: asyncio.Task | None = None
current_audio_queue: asyncio.Queue | None = None
last_barge_in_time: float = 0.0


def trigger_barge_in(source: str = "user") -> bool:
    """Instantly cuts off active TTS audio playback, clears streaming clause queues, and re-arms VAD listening."""
    global is_playback_streaming, active_ai_pipeline_task, active_playback_task, current_audio_queue, last_barge_in_time
    now = time.time()

    was_speaking = is_playback_streaming
    try:
        import pygame
        if pygame.mixer.get_init() and pygame.mixer.get_busy():
            was_speaking = True
    except Exception:
        pass

    # Signal all playback loops to break immediately
    barge_in_event.set()
    last_barge_in_time = now

    # 1. Stop local audio hardware output immediately
    try:
        import pygame
        if pygame.mixer.get_init():
            pygame.mixer.stop()
    except Exception:
        pass

    try:
        import winsound
        winsound.PlaySound(None, winsound.SND_PURGE)
    except Exception:
        pass

    # 2. Flush / drain pending clause audio queue
    if current_audio_queue is not None:
        while not current_audio_queue.empty():
            try:
                current_audio_queue.get_nowait()
                current_audio_queue.task_done()
            except Exception:
                pass

    # 3. Cancel active playback task if running
    if active_playback_task and not active_playback_task.done():
        active_playback_task.cancel()

    # 4. Update state flags
    is_playback_streaming = False
    dashboard_state["is_playback_streaming"] = False
    dashboard_state["pipeline_status"] = f"Barge-In Interrupted ({source})"
    print(f"⚡ [Barge-In] Playback instantly interrupted by '{source}' (was_speaking={was_speaking})")

    # 5. Re-arm VAD Active Listening immediately so user speech is captured
    if audio_protocol_instance:
        audio_protocol_instance.set_active_listening(reason=f"Barge-In ({source})")

    return was_speaking


async def stream_audio_to_glasses(audio_data, target_ip: str | None = None, target_port: int = 9001, manage_vad_mute: bool = True):
    """Streams 16-bit Mono 16kHz raw PCM audio over UDP directly to the ESP32.
    Accepts raw PCM bytes in memory or a WAV filepath. Checks barge_in_event to halt immediately."""
    global is_playback_streaming, last_esp32_ip, barge_in_event
    import socket

    dest_ip = target_ip or last_esp32_ip
    if not dest_ip:
        return

    try:
        if isinstance(audio_data, (bytes, bytearray)):
            pcm_bytes = audio_data
        elif isinstance(audio_data, str) and os.path.exists(audio_data):
            with open(audio_data, "rb") as f:
                f.seek(44)
                pcm_bytes = f.read()
        else:
            return

        total_bytes = len(pcm_bytes)
        if total_bytes == 0:
            return

        sample_rate = 16000
        bytes_per_sec = sample_rate * 2  # 32,000 bytes/sec
        chunk_size = 1024  # 512 samples = 32ms
        pacing_delay = chunk_size / bytes_per_sec  # 0.032s between bursts

        if manage_vad_mute:
            is_playback_streaming = True
            dashboard_state["is_playback_streaming"] = True

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for offset in range(0, total_bytes, chunk_size):
                if barge_in_event.is_set():
                    print("🛑 [Playback UDP] Stream transmission halted by Barge-In.")
                    break
                chunk = pcm_bytes[offset:offset + chunk_size]
                sock.sendto(chunk, (dest_ip, target_port))
                await asyncio.sleep(pacing_delay)
        finally:
            sock.close()

        if manage_vad_mute and not barge_in_event.is_set():
            await asyncio.sleep(0.35)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"❌ [Playback UDP Error]: {e}")
    finally:
        if manage_vad_mute:
            is_playback_streaming = False
            dashboard_state["is_playback_streaming"] = False


async def play_audio_locally(audio_data):
    """Plays audio through the host computer's speakers in a non-blocking worker thread.
    Accepts either in-memory WAV bytes or a WAV filepath. Checks barge_in_event to halt immediately."""
    global barge_in_event
    if barge_in_event.is_set():
        return

    def _play_blocking():
        try:
            import pygame
            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=16000, size=-16, channels=1)
            if isinstance(audio_data, (bytes, bytearray)):
                sound = pygame.mixer.Sound(io.BytesIO(audio_data))
            else:
                sound = pygame.mixer.Sound(audio_data)
            sound.play()
            while pygame.mixer.get_busy():
                if barge_in_event.is_set():
                    pygame.mixer.stop()
                    break
                pygame.time.wait(20)
        except Exception as e:
            # Fallback to Windows native winsound (supports SND_MEMORY for bytes!)
            try:
                import winsound
                if barge_in_event.is_set():
                    return
                if isinstance(audio_data, (bytes, bytearray)):
                    winsound.PlaySound(audio_data, winsound.SND_MEMORY)
                elif isinstance(audio_data, str) and os.path.exists(audio_data):
                    winsound.PlaySound(audio_data, winsound.SND_FILENAME)
            except Exception as e2:
                print(f"❌ [Local Playback Error]: {e} | Fallback error: {e2}")

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _play_blocking)


# Maintain backward compatibility alias
stream_audio_to_esp32 = stream_audio_to_glasses

# --- Zero-Latency UI Audio Cache (Pre-synthesized WAV & PCM) ---
UI_AUDIO_EVENTS = {
    "snapshot": "Snapshot saved.",
    "vision_on": "Vision mode active.",
    "vision_off": "Vision mode disabled.",
    "memory_cleared": "Memory cleared.",
    "beep": "Listening."
}

ui_audio_cache: dict[str, tuple[bytes, bytes]] = {}  # event_key -> (pcm_bytes, wav_bytes)


async def init_ui_audio_cache():
    """Generates and pre-caches UI audio clips in cache/ on startup for 0ms runtime latency."""
    os.makedirs("cache", exist_ok=True)
    print("⏳ [Audio Cache] Initializing Zero-Latency UI Audio Cache...")
    for event_key, phrase in UI_AUDIO_EVENTS.items():
        wav_path = os.path.join("cache", f"{event_key}.wav")
        if os.path.exists(wav_path):
            try:
                with open(wav_path, "rb") as f:
                    wav_bytes = f.read()
                with wave.open(wav_path, "rb") as wf:
                    pcm_bytes = wf.readframes(wf.getnframes())
                ui_audio_cache[event_key] = (pcm_bytes, wav_bytes)
                print(f"  ⚡ [Audio Cache] Loaded '{event_key}' from disk cache.")
                continue
            except Exception as e:
                print(f"  ⚠️ [Audio Cache] Error reading cache file {wav_path}: {e}")

        # Synthesize via edge-tts in memory
        print(f"  🎙️ [Audio Cache] Synthesizing '{event_key}': \"{phrase}\"...")
        pcm_bytes, wav_bytes = await synthesize_clause_pcm(phrase)
        if wav_bytes and pcm_bytes:
            ui_audio_cache[event_key] = (pcm_bytes, wav_bytes)
            try:
                with open(wav_path, "wb") as f:
                    f.write(wav_bytes)
                print(f"  ✅ [Audio Cache] Saved '{event_key}' to {wav_path}")
            except Exception as e:
                print(f"  ⚠️ [Audio Cache] Failed to save {wav_path}: {e}")
        else:
            print(f"  ❌ [Audio Cache] Failed to synthesize '{event_key}'")

    print(f"✨ [Audio Cache] Zero-Latency Cache Ready ({len(ui_audio_cache)}/{len(UI_AUDIO_EVENTS)} events in RAM)")


async def play_cached_ui_event(event_key: str):
    """Zero-Latency playback of pre-cached audio events to host speaker and ESP32 glasses."""
    global last_esp32_ip
    item = ui_audio_cache.get(event_key)
    if not item:
        print(f"⚠️ [Audio Cache] Event '{event_key}' not found in cache!")
        return
    pcm_bytes, wav_bytes = item
    try:
        await asyncio.gather(
            play_audio_locally(wav_bytes),
            stream_audio_to_glasses(pcm_bytes, last_esp32_ip)
        )
    except Exception as e:
        print(f"❌ [Audio Cache Playback Error]: {e}")



# Global reference to active audio protocol instance
audio_protocol_instance = None

# --- Audio UDP Streaming Protocol with Dedicated OpenWakeWord Engine & VAD ---

class AudioUDPProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        global audio_protocol_instance, audio_state
        self.transport = None
        self.loop = None
        self.audio_buffer = bytearray()
        
        # Audio Interaction State Machine: "IDLE" or "ACTIVE_LISTENING"
        audio_state = "IDLE"
        self.is_speaking = False
        self.silence_chunks = 0
        self.speech_streak = 0
        self.voiced_chunks = 0
        self.recording_chunks = 0

        # Audio & DSP Tuning
        self.SAMPLE_RATE = 16000
        self.BYTES_PER_SAMPLE = 2  # 16-bit PCM = 2 bytes

        # VAD Hysteresis Tuning (Dual-Threshold Schmitt Trigger - Optimized for natural conversational speech)
        self.BASE_START_THRESHOLD = 450.0   # Lowered from 700.0 for effortless normal/quiet voice detection
        self.START_SNR_MULTIPLIER = 1.20    # Requires only 1.20x ambient noise floor to start
 
        self.BASE_CONTINUE_THRESHOLD = 280.0 # Lowered from 350.0 to prevent premature speech cut-off
        self.CONTINUE_SNR_MULTIPLIER = 1.10  # Keeps recording during natural soft speech and vocal decay

        self.REQUIRED_SPEECH_STREAK = 2     # Consecutive frames (~64ms) to confirm speech start
        self.MIN_VOICED_CHUNKS = 2          # Utterance must contain at least ~64ms of vocal energy

        # VAD Tuning: 550ms of trailing silence allows natural pauses between words without cutting sentences
        self.SILENCE_TIMEOUT_SEC = 0.55     
        self.MAX_RECORDING_SEC = 10.0       # Safety cap
        self.MIN_UTTERANCE_SEC = 0.35       # Minimum audio clip length to save

        # Dynamic Noise Floor Tracking
        self.noise_floor = 450.0            # Initial estimate for >> 15 hardware gain

        # Ring buffer for pre-speech frames
        self.pre_buffer = deque(maxlen=12)

        self.packet_count = 0
        audio_protocol_instance = self

    def set_idle(self):
        """Resets the state machine back to IDLE state to listen exclusively for wake words."""
        global audio_state, dashboard_state
        audio_state = "IDLE"
        self.is_speaking = False
        self.audio_buffer.clear()
        self.silence_chunks = 0
        self.speech_streak = 0
        self.voiced_chunks = 0
        self.recording_chunks = 0
        try:
            wakeword_model.reset()
        except Exception:
            pass
        dashboard_state["audio_state"] = "IDLE"
        dashboard_state["pipeline_status"] = "Idle (Listening for wake word...)"
        print("💤 [State Machine] IDLE: Waiting for wake word ('alexa' / 'hey mycroft' / 'hey jarvis')...")

    def set_active_listening(self, reason: str = "Wake word triggered"):
        """Transitions audio engine to ACTIVE_LISTENING mode with an 8-second hot mic window."""
        global audio_state, active_listening_start, dashboard_state
        audio_state = "ACTIVE_LISTENING"
        active_listening_start = time.time()
        self.is_speaking = False
        self.speech_streak = 0
        self.voiced_chunks = 0
        self.silence_chunks = 0
        self.recording_chunks = 0
        self.audio_buffer.clear()
        try:
            wakeword_model.reset()
        except Exception:
            pass
        dashboard_state["audio_state"] = "ACTIVE_LISTENING"
        dashboard_state["pipeline_status"] = f"Active Listening ({reason} - 8s window)..."
        print(f"🔥 [State Machine] ACTIVE_LISTENING: Mic hot for {ACTIVE_LISTEN_TIMEOUT}s ({reason})")

    def connection_made(self, transport):
        self.transport = transport
        self.loop = asyncio.get_running_loop()
        print("🟢 VAD Audio Protocol ready on UDP port 9000 (Initial State: IDLE).")

    def datagram_received(self, data: bytes, addr):
        global is_playback_streaming, last_esp32_ip, dashboard_state, audio_state, active_listening_start, ACTIVE_LISTEN_TIMEOUT
        if addr and addr[0]:
            last_esp32_ip = addr[0]
            dashboard_state["esp32_ip"] = addr[0]
        if is_playback_streaming:
            # Downlink speaker is active: inspect audio for prominent User Voice or Wake Word to trigger Acoustic Barge-In
            audio_data = np.frombuffer(data, dtype=np.int16)
            if audio_data.size > 0:
                audio_float = audio_data.astype(np.float32)
                audio_float -= np.mean(audio_float)
                rms_energy = float(np.sqrt(np.mean(audio_float**2)))

                # 1. Check for intentional user speech onset (prominent vocal energy above speaker bleed)
                barge_threshold = max(850.0, self.noise_floor * 2.2)
                if rms_energy >= barge_threshold:
                    self.speech_streak += 1
                    if self.speech_streak >= 2:
                        print(f"⚡ [Acoustic Barge-In] User Voice Detected during playback! (RMS: {rms_energy:.1f} >= {barge_threshold:.1f})")
                        trigger_barge_in(source="User Voice VAD")
                        self.is_speaking = True
                        self.voiced_chunks = self.speech_streak
                        self.silence_chunks = 0
                        self.recording_chunks = 0
                        self.audio_buffer.clear()
                        for chunk in self.pre_buffer:
                            self.audio_buffer.extend(chunk)
                        return
                else:
                    self.speech_streak = 0

                # 2. Check for wake word trigger during playback
                try:
                    scores = wakeword_model.predict(audio_data)
                    for m_name, score in scores.items():
                        if score >= 0.50 and rms_energy >= 450.0:
                            print(f"✨ [Wake Word Barge-In] '{m_name}' triggered during playback! (Conf: {score:.2f})")
                            trigger_barge_in(source=f"Wake Word '{m_name}'")
                            return
                except Exception:
                    pass

            # If not barging in, drop audio packet to prevent acoustic echo feedback loop
            return

        self.packet_count += 1
        self.last_packet_len = len(data)
        audio_data = np.frombuffer(data, dtype=np.int16)
        if audio_data.size == 0:
            return

        # 1. Remove DC Offset (vital for INMP441 MEMS microphone)
        audio_float = audio_data.astype(np.float32)
        audio_float -= np.mean(audio_float)

        # 2. Compute Zero-Centered RMS Energy
        rms_energy = float(np.sqrt(np.mean(audio_float**2)))

        chunk_duration = len(audio_data) / self.SAMPLE_RATE
        silence_limit_chunks = max(1, int(self.SILENCE_TIMEOUT_SEC / chunk_duration))
        max_recording_chunks = max(1, int(self.MAX_RECORDING_SEC / chunk_duration))

        # Always feed the pre-speech circular buffer
        self.pre_buffer.append(data)

        # -------------------------------------------------------------
        # STATE 1: IDLE - Route incoming UDP directly into OpenWakeWord
        # (Whisper STT is NEVER invoked in Idle state to preserve CPU)
        # -------------------------------------------------------------
        if audio_state == "IDLE":
            self.noise_floor = 0.97 * self.noise_floor + 0.03 * rms_energy
            dashboard_state["noise_floor"] = round(self.noise_floor, 1)

            scores = wakeword_model.predict(audio_data)
            max_score = max(scores.values()) if scores else 0.0
            if max_score >= 0.35 and self.packet_count % 10 == 0:
                print(f"👂 [Wake Word Tracker] Peak: {max_score:.2f} | {scores}")

            detected_model = None
            for m_name, score in scores.items():
                if score >= 0.55 and rms_energy >= 380.0:
                    detected_model = m_name
                    break

            if detected_model:
                print(f"\n✨ [Wake Word Detected] '{detected_model}' triggered! (Confidence: {scores[detected_model]:.2f})")
                self.set_active_listening(reason=f"Wake word '{detected_model}'")

                # Instantly play cached "beep" confirmation (0ms latency)
                if self.loop and self.loop.is_running():
                    asyncio.run_coroutine_threadsafe(play_cached_ui_event("beep"), self.loop)

            return

        # -------------------------------------------------------------
        # STATE 2: ACTIVE_LISTENING - Route audio to VAD buffer & Whisper pipeline
        # (Follow-Up Mode or Instant Vision Listen)
        # -------------------------------------------------------------
        # Check if 8-second hot mic window has timed out while waiting for speech onset
        if not self.is_speaking and (time.time() - active_listening_start > ACTIVE_LISTEN_TIMEOUT):
            print(f"⏳ [Active Listening Timeout] No speech detected in {ACTIVE_LISTEN_TIMEOUT}s. Reverting to IDLE.")
            self.set_idle()
            return

        # Dual Dynamic Threshold Calculation (Hysteresis)
        start_threshold = max(self.BASE_START_THRESHOLD, self.noise_floor * self.START_SNR_MULTIPLIER)
        continue_threshold = max(self.BASE_CONTINUE_THRESHOLD, self.noise_floor * self.CONTINUE_SNR_MULTIPLIER)

        if not self.is_speaking:
            # --- LISTENING FOR SPEECH ONSET ---
            if rms_energy > start_threshold:
                self.speech_streak += 1
                if self.speech_streak >= self.REQUIRED_SPEECH_STREAK:
                    print(f"\n🗣️ [VAD] User Speech Started! (RMS: {rms_energy:.1f} > Gate: {start_threshold:.1f})")
                    self.is_speaking = True
                    dashboard_state["pipeline_status"] = "Listening to speech..."
                    self.voiced_chunks = self.speech_streak
                    self.silence_chunks = 0
                    self.recording_chunks = 0
                    self.audio_buffer.clear()
                    for chunk in self.pre_buffer:
                        self.audio_buffer.extend(chunk)
            else:
                self.speech_streak = 0
        else:
            # --- CURRENTLY BUFFERING SPEECH ---
            self.recording_chunks += 1
            self.audio_buffer.extend(data)

            if rms_energy > continue_threshold:
                self.voiced_chunks += 1
                self.silence_chunks = 0
            else:
                self.silence_chunks += 1

            if self.recording_chunks % 30 == 0:
                print(f"🎙️ [Recording...] RMS: {rms_energy:.0f} | HoldGate: {continue_threshold:.0f} | Silence: {self.silence_chunks}/{silence_limit_chunks}")

            # Check termination conditions (natural silence timeout or max duration)
            if self.silence_chunks > silence_limit_chunks or self.recording_chunks > max_recording_chunks:
                reason = f"{int(self.SILENCE_TIMEOUT_SEC*1000)}ms silence timeout" if self.silence_chunks > silence_limit_chunks else "Max duration limit"
                print(f"🛑 [VAD] Speech ended ({reason}). Finalizing utterance immediately...")
                self._dispatch_save()
                self.is_speaking = False
                self.silence_chunks = 0
                self.speech_streak = 0
                self.voiced_chunks = 0
                self.recording_chunks = 0
                self.audio_buffer.clear()

    def _dispatch_save(self):
        t_vad_end = time.perf_counter()

        # 1. Require minimum actual voiced content
        if self.voiced_chunks < self.MIN_VOICED_CHUNKS:
            print(f"⚠️ [VAD] Discarded noise spike (only {self.voiced_chunks} voiced frames < {self.MIN_VOICED_CHUNKS} required)")
            return

        min_bytes = int(self.SAMPLE_RATE * self.BYTES_PER_SAMPLE * self.MIN_UTTERANCE_SEC)
        if len(self.audio_buffer) < min_bytes:
            print(f"⚠️ [VAD] Utterance discarded (too short: {len(self.audio_buffer)} bytes < {min_bytes} bytes)")
            return

        # Trim trailing silence: keep ~100-120ms of natural vocal decay
        trailing_keep_bytes = int(self.SAMPLE_RATE * self.BYTES_PER_SAMPLE * 0.12)
        packet_len = getattr(self, "last_packet_len", 1024)
        excess_silence_bytes = (self.silence_chunks * packet_len) - trailing_keep_bytes

        if 0 < excess_silence_bytes < len(self.audio_buffer):
            raw_to_save = bytes(self.audio_buffer[:-excess_silence_bytes])
        else:
            raw_to_save = bytes(self.audio_buffer)

        duration_sec = len(raw_to_save) / (self.SAMPLE_RATE * self.BYTES_PER_SAMPLE)

        # Offload in-memory transcription directly to worker thread without saving to disk first
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, self._write_wav_and_transcribe, raw_to_save, duration_sec, t_vad_end)

    def _write_wav_and_transcribe(self, audio_bytes: bytes, duration: float, t_vad_end: float):
        # 1. Zero Disk I/O: Format raw PCM bytes into an in-memory io.BytesIO() WAV container
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(self.BYTES_PER_SAMPLE)
            wf.setframerate(self.SAMPLE_RATE)
            wf.writeframes(audio_bytes)
        wav_buffer.seek(0)

        # Asynchronously save to disk in background daemon thread for debugging/archival without blocking STT
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bg_filename = f"audio_utterances/voice_{timestamp}.wav"
        def _bg_disk_save():
            try:
                with open(bg_filename, "wb") as f:
                    f.write(wav_buffer.getvalue())
            except Exception:
                pass
        threading.Thread(target=_bg_disk_save, daemon=True).start()

        # 2. High-Efficiency Whisper Configuration: transcribe directly from in-memory WAV buffer
        try:
            segments, info = whisper_model.transcribe(
                wav_buffer,
                beam_size=1,
                best_of=1,
                temperature=0,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=200),
                condition_on_previous_text=False,
                language="en",
                initial_prompt=WHISPER_INITIAL_PROMPT
            )
            transcript = " ".join([segment.text for segment in segments]).strip()
            t_stt_end = time.perf_counter()
            vad_to_stt = t_stt_end - t_vad_end

            if transcript:
                print(f"📝 Transcribed: \"{transcript}\" (⏱️ {vad_to_stt*1000:.0f}ms | {duration:.1f}s audio)")
                
                # Strip any leading wake word residue (e.g. 'alexa', 'hey mycroft', 'hey meta', 'hey jarvis')
                import re
                clean_query = re.sub(r'(?i)\b(alexa|hey\s+mycroft|hey\s+meta|hey\s+jarvis)\b', '', transcript).strip(" ,:;.!?")
                if not clean_query:
                    clean_query = transcript.strip()

                timing_meta = {
                    "t_vad_end": t_vad_end,
                    "vad_to_stt": vad_to_stt,
                    "wake_check": 0.0,
                    "t_llm_start": time.perf_counter()
                }
                if self.loop and self.loop.is_running():
                    asyncio.run_coroutine_threadsafe(self.process_llm_and_tts(clean_query, timing_meta), self.loop)
            else:
                print(f"⚠️ [STT] No intelligible speech recognized (⏱️ {vad_to_stt*1000:.0f}ms)")
        except Exception as e:
            print(f"❌ Error during STT transcription: {e}")

    async def process_llm_and_tts(self, user_text: str, timing_meta: dict | None = None):
        """Dispatches voice utterance to Gemini LLM + TTS, then re-arms 8-second Follow-Up window."""
        try:
            await run_full_ai_pipeline(user_text, timing_meta)
        finally:
            # Follow-Up Mode: Immediately keep mic hot for 8 seconds after AI finishes responding
            self.set_active_listening(reason="Follow-Up Mode after response")



async def run_full_ai_pipeline(user_text: str, timing_meta: dict | None = None):
    """Executes the full Vision + Streaming LLM + Clause TTS + Sequential Playback pipeline with telemetry."""
    global dashboard_state, chat_history, latest_enhanced_frame_bytes, latest_frame_timestamp, vision_mode_active
    if pipeline_lock.locked():
        print(f"⏳ [Pipeline] Playback currently busy. Queuing request: \"{user_text}\"")

    async with pipeline_lock:
        barge_in_event.clear()
        try:
            if timing_meta is None:
                timing_meta = {
                    "t_vad_end": time.perf_counter(),
                    "vad_to_stt": 0.0,
                    "wake_check": 0.0,
                    "t_llm_start": time.perf_counter()
                }

            t_llm_start = timing_meta.get("t_llm_start", time.perf_counter())

            dashboard_state["pipeline_status"] = "Thinking with Gemini..."
            user_msg = {
                "id": f"msg_u_{int(datetime.now().timestamp()*1000)}",
                "role": "user",
                "text": user_text,
                "time": datetime.now().strftime("%H:%M:%S")
            }
            if not (chat_history and chat_history[-1].get("role") == "user" and chat_history[-1].get("text") == user_text):
                chat_history.append(user_msg)

            # Check snapshot intent first ("take a picture", "take a snapshot", etc.)
            SNAPSHOT_TRIGGERS = [
                "take a picture", "take a snapshot", "take picture", "take snapshot",
                "capture picture", "capture a picture", "capture a snapshot",
                "snap a picture", "take photo", "take a photo", "save snapshot"
            ]
            clean_cmd = user_text.lower().strip()
            if any(t in clean_cmd for t in SNAPSHOT_TRIGGERS):
                print(f"📸 [Intent] Intercepted snapshot voice command: \"{user_text}\"")
                dashboard_state["pipeline_status"] = "Capturing RGB Snapshot..."

                raw_path = os.path.join("captured_frames", "raw_frame.jpg")
                loop = asyncio.get_running_loop()
                snapshot_url = await loop.run_in_executor(None, enhance_image_rgb, raw_path)

                if snapshot_url:
                    if snapshot_url not in gallery_images:
                        gallery_images.insert(0, snapshot_url)
                    dashboard_state["gallery_images"] = gallery_images
                    await play_cached_ui_event("snapshot")
                else:
                    pcm_bytes, wav_bytes = await synthesize_speech("I could not capture a snapshot right now.")
                    if wav_bytes and pcm_bytes:
                        await asyncio.gather(
                            play_audio_locally(wav_bytes),
                            stream_audio_to_glasses(pcm_bytes, last_esp32_ip)
                        )
                return

            # Check memory reset intent ("clear memory", "forget everything", etc.)
            MEMORY_CLEAR_TRIGGERS = [
                "clear memory", "forget everything", "reset memory", "clear conversation",
                "reset conversation", "clear history", "delete history", "forget our conversation",
                "start over", "new conversation"
            ]
            if any(t in clean_cmd for t in MEMORY_CLEAR_TRIGGERS):
                print(f"🧹 [Intent] Intercepted clear memory voice command: \"{user_text}\"")
                chat_history.clear()
                dashboard_state["chat_history"] = chat_history
                await play_cached_ui_event("memory_cleared")
                dashboard_state["pipeline_status"] = "Conversation Memory Cleared"
                pcm_bytes, wav_bytes = await synthesize_speech("Conversation memory cleared.")
                if wav_bytes and pcm_bytes:
                    await asyncio.gather(
                        play_audio_locally(wav_bytes),
                        stream_audio_to_glasses(pcm_bytes, last_esp32_ip)
                    )
                return

            # Check note-taking intent ("take a note", "add note", "remember to", etc.)
            NOTE_TAKE_PREFIXES = [
                "take a note that", "take a note to", "take a note:", "take a note",
                "create a note that", "create a note to", "create a note:", "create a note",
                "add a note that", "add a note to", "add a note:", "add a note",
                "make a note that", "make a note to", "make a note:", "make a note",
                "write this down that", "write this down to", "write this down:", "write this down",
                "save a note that", "save a note to", "save a note:", "save a note",
                "save note that", "save note to", "save note:", "save note",
                "note that"
            ]
            note_content = None
            for pfx in NOTE_TAKE_PREFIXES:
                if clean_cmd.startswith(pfx):
                    note_content = user_text[len(pfx):].strip(" :,.")
                    break

            if note_content:
                # Capitalize first letter
                note_content = note_content[0].upper() + note_content[1:] if len(note_content) > 1 else note_content.upper()
                print(f"📝 [Intent] Intercepted note creation voice command: \"{note_content}\"")
                dashboard_state["pipeline_status"] = f"Saving Note: \"{note_content}\"..."
                entry = save_note_entry(note_content)
                dashboard_state["notes"] = load_notes()
                confirmation_phrase = f"Note saved: {note_content}."
                pcm_bytes, wav_bytes = await synthesize_speech(confirmation_phrase)
                if wav_bytes and pcm_bytes:
                    await asyncio.gather(
                        play_audio_locally(wav_bytes),
                        stream_audio_to_glasses(pcm_bytes, last_esp32_ip)
                    )
                ai_msg = {
                    "id": f"msg_a_{int(datetime.now().timestamp()*1000)}",
                    "role": "assistant",
                    "text": confirmation_phrase,
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "audio_url": None,
                    "has_vision": False
                }
                chat_history.append(ai_msg)
                return

            # Check read notes intent ("what are my notes", "read my notes", etc.)
            NOTE_READ_TRIGGERS = [
                "what are my notes", "read my notes", "read notes", "list my notes",
                "list notes", "show my notes", "show notes", "tell me my notes",
                "check my notes", "do i have any notes"
            ]
            if any(t in clean_cmd for t in NOTE_READ_TRIGGERS):
                print(f"📝 [Intent] Intercepted read notes voice command: \"{user_text}\"")
                notes = load_notes()
                dashboard_state["pipeline_status"] = "Reading Saved Notes..."
                if not notes:
                    reply_text = "You don't have any saved notes."
                elif len(notes) == 1:
                    reply_text = f"You have one saved note: {notes[0]['text']}."
                else:
                    items = [f"Note {i+1}: {n['text']}" for i, n in enumerate(notes[:4])]
                    reply_text = f"You have {len(notes)} notes. " + ". ".join(items) + "."

                pcm_bytes, wav_bytes = await synthesize_speech(reply_text)
                ai_msg = {
                    "id": f"msg_a_{int(datetime.now().timestamp()*1000)}",
                    "role": "assistant",
                    "text": reply_text,
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "audio_url": None,
                    "has_vision": False
                }
                chat_history.append(ai_msg)
                if wav_bytes and pcm_bytes:
                    await asyncio.gather(
                        play_audio_locally(wav_bytes),
                        stream_audio_to_glasses(pcm_bytes, last_esp32_ip)
                    )
                return

            # Check clear notes intent ("clear all notes", "delete all notes", etc.)
            NOTE_CLEAR_TRIGGERS = [
                "clear all notes", "delete all notes", "wipe my notes", "remove all notes",
                "clear my notes", "delete notes"
            ]
            if any(t in clean_cmd for t in NOTE_CLEAR_TRIGGERS):
                print(f"🧹 [Intent] Intercepted clear notes voice command: \"{user_text}\"")
                clear_all_notes_entries()
                dashboard_state["notes"] = []
                dashboard_state["pipeline_status"] = "Notes Cleared"
                reply_text = "All saved notes have been cleared."
                pcm_bytes, wav_bytes = await synthesize_speech(reply_text)
                ai_msg = {
                    "id": f"msg_a_{int(datetime.now().timestamp()*1000)}",
                    "role": "assistant",
                    "text": reply_text,
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "audio_url": None,
                    "has_vision": False
                }
                chat_history.append(ai_msg)
                if wav_bytes and pcm_bytes:
                    await asyncio.gather(
                        play_audio_locally(wav_bytes),
                        stream_audio_to_glasses(pcm_bytes, last_esp32_ip)
                    )
                return

            # Check on-demand weather intent ("weather", "forecast", "temperature", "will it rain", etc.)
            WEATHER_TRIGGERS = [
                "weather", "forecast", "temperature", "rain", "raining", "rainy",
                "umbrella", "how hot", "is it hot", "how cold", "is it cold", "humidity"
            ]
            if any(t in clean_cmd for t in WEATHER_TRIGGERS):
                print(f"🌤️ [Intent] Intercepted on-demand weather voice command: \"{user_text}\"")
                dashboard_state["pipeline_status"] = "Fetching Live Weather..."

                import re
                m = re.search(r'\b(?:in|for|at)\s+([a-zA-Z\s]+?)(?:\s+today|\s+tomorrow|\s+right now|\?|$)', clean_cmd)
                target_city = m.group(1).strip() if m else None

                loop = asyncio.get_running_loop()
                weather_data = await loop.run_in_executor(None, fetch_live_weather, target_city)

                if "error" in weather_data:
                    reply_text = f"I could not retrieve weather data: {weather_data['error']}"
                else:
                    city_name = weather_data["city"]
                    temp = weather_data["temperature"]
                    cond = weather_data["condition"].lower()
                    humidity = weather_data["humidity"]
                    temp_max = weather_data["temp_max"]
                    temp_min = weather_data["temp_min"]
                    feels = weather_data["apparent_temperature"]
                    rain_prob = weather_data["rain_probability"]

                    if any(r in clean_cmd for r in ["rain", "umbrella", "raining", "rainy"]):
                        if rain_prob > 25:
                            reply_text = f"In {city_name}, there is a {rain_prob}% chance of rain today with {cond} and {temp} degrees."
                        else:
                            reply_text = f"No rain expected in {city_name} today with a {rain_prob}% chance. Currently it is {temp} degrees and {cond}."
                    elif any(t in clean_cmd for t in ["temperature", "how hot", "is it hot", "how cold", "is it cold"]):
                        reply_text = f"Currently in {city_name}, the temperature is {temp} degrees, feeling like {feels} degrees, with a high of {temp_max} degrees."
                    else:
                        reply_text = f"Currently in {city_name}, it is {temp} degrees and {cond} with {humidity}% humidity. Today's high is {temp_max} degrees."

                    weather_data["summary"] = reply_text
                    weather_data["updated_at"] = datetime.now().strftime("%I:%M %p")
                    dashboard_state["latest_weather"] = weather_data

                pcm_bytes, wav_bytes = await synthesize_speech(reply_text)
                ai_msg = {
                    "id": f"msg_a_{int(datetime.now().timestamp()*1000)}",
                    "role": "assistant",
                    "text": reply_text,
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "audio_url": None,
                    "has_vision": False
                }
                chat_history.append(ai_msg)
                dashboard_state["pipeline_status"] = "Weather Dispatched"

                if wav_bytes and pcm_bytes:
                    await asyncio.gather(
                        play_audio_locally(wav_bytes),
                        stream_audio_to_glasses(pcm_bytes, last_esp32_ip)
                    )
                return

            # Check timer and reminder voice intents
            TIMER_CANCEL_TRIGGERS = [
                "cancel timer", "cancel my timer", "cancel the timer", "cancel all timers",
                "delete timer", "delete my timer", "stop timer", "stop the timer",
                "clear timers", "clear all timers", "cancel reminder", "cancel reminders"
            ]
            TIMER_LIST_TRIGGERS = [
                "what are my timers", "what are my active timers", "check timers", "list timers",
                "list my timers", "active timers", "how much time is left", "do i have any timers",
                "what are my reminders", "check reminders", "list reminders"
            ]
            is_timer_create = any(t in clean_cmd for t in ["timer", "remind"])

            if any(t in clean_cmd for t in TIMER_CANCEL_TRIGGERS):
                print(f"⏹️ [Intent] Intercepted cancel timers voice command: \"{user_text}\"")
                count = cancel_all_timers()
                reply_text = f"Cancelled {count} timer." if count == 1 else (f"All {count} timers have been cancelled." if count > 0 else "You don't have any active timers.")
                pcm_bytes, wav_bytes = await synthesize_speech(reply_text)
                ai_msg = {
                    "id": f"msg_a_{int(datetime.now().timestamp()*1000)}",
                    "role": "assistant",
                    "text": reply_text,
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "audio_url": None,
                    "has_vision": False
                }
                chat_history.append(ai_msg)
                dashboard_state["pipeline_status"] = "Timers Cancelled"
                if wav_bytes and pcm_bytes:
                    await asyncio.gather(
                        play_audio_locally(wav_bytes),
                        stream_audio_to_glasses(pcm_bytes, last_esp32_ip)
                    )
                return

            if any(t in clean_cmd for t in TIMER_LIST_TRIGGERS):
                print(f"⏳ [Intent] Intercepted list timers voice command: \"{user_text}\"")
                timers = get_active_timers_list()
                if not timers:
                    reply_text = "You don't have any active timers or reminders."
                elif len(timers) == 1:
                    t = timers[0]
                    rem = t["remaining_sec"]
                    rem_str = f"{rem // 60} minutes" if rem >= 60 else f"{rem} seconds"
                    reply_text = f"You have one active {t['type']} for {t['label']} with {rem_str} remaining."
                else:
                    items = [f"{t['label']} with {t['time_left']} left" for t in timers[:3]]
                    reply_text = f"You have {len(timers)} active timers: " + ", ".join(items) + "."
                pcm_bytes, wav_bytes = await synthesize_speech(reply_text)
                ai_msg = {
                    "id": f"msg_a_{int(datetime.now().timestamp()*1000)}",
                    "role": "assistant",
                    "text": reply_text,
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "audio_url": None,
                    "has_vision": False
                }
                chat_history.append(ai_msg)
                dashboard_state["pipeline_status"] = "Timers Listed"
                if wav_bytes and pcm_bytes:
                    await asyncio.gather(
                        play_audio_locally(wav_bytes),
                        stream_audio_to_glasses(pcm_bytes, last_esp32_ip)
                    )
                return

            if is_timer_create:
                import re
                dur_match = re.search(r'(\d+(?:\.\d+)?)\s*(second|sec|minute|min|hour|hr)s?', clean_cmd)
                if dur_match:
                    val = float(dur_match.group(1))
                    unit = dur_match.group(2)
                    mult = 1
                    if "min" in unit:
                        mult = 60
                    elif "hour" in unit or "hr" in unit:
                        mult = 3600
                    total_sec = int(val * mult)

                    is_reminder = "remind" in clean_cmd
                    label = "Timer"
                    if is_reminder:
                        m1 = re.search(r'remind\s+(?:me\s+)?to\s+(.+?)\s+in\s+\d+', clean_cmd)
                        m2 = re.search(r'remind\s+(?:me\s+)?in\s+\d+\s*(?:second|sec|minute|min|hour|hr)s?\s+to\s+(.+)', clean_cmd)
                        if m1:
                            label = m1.group(1).strip()
                        elif m2:
                            label = m2.group(1).strip()
                        else:
                            label = "Reminder"
                    label = label.capitalize()

                    print(f"⏳ [Intent] Intercepted timer/reminder creation: label=\"{label}\", duration={total_sec}s")
                    timer_obj = create_timer(total_sec, label, is_reminder)

                    if total_sec >= 60:
                        dur_text = f"{total_sec // 60} minute" if total_sec % 60 == 0 else f"{total_sec // 60} minute {total_sec % 60} second"
                    else:
                        dur_text = f"{total_sec} second"

                    if is_reminder:
                        reply_text = f"I will remind you to {label.lower()} in {dur_text}."
                    else:
                        reply_text = f"Timer set for {dur_text}."

                    pcm_bytes, wav_bytes = await synthesize_speech(reply_text)
                    ai_msg = {
                        "id": f"msg_a_{int(datetime.now().timestamp()*1000)}",
                        "role": "assistant",
                        "text": reply_text,
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "audio_url": None,
                        "has_vision": False
                    }
                    chat_history.append(ai_msg)
                    dashboard_state["pipeline_status"] = f"Timer Active: {dur_text}"
                    if wav_bytes and pcm_bytes:
                        await asyncio.gather(
                            play_audio_locally(wav_bytes),
                            stream_audio_to_glasses(pcm_bytes, last_esp32_ip)
                        )
                    return

            # Step A: Build multimodal payload (Attach image ONLY IF vision_mode_active is True)
            payload_image = latest_enhanced_frame_bytes if vision_mode_active else None
            pil_img = None
            image_attached = False

            if payload_image:
                now_ts = datetime.now().timestamp()
                if now_ts - latest_frame_timestamp <= 20.0:
                    try:
                        decoded_img = Image.open(io.BytesIO(payload_image))
                        if decoded_img.width > 640 or decoded_img.height > 640:
                            decoded_img.thumbnail((640, 640), Image.Resampling.LANCZOS)
                        pil_img = decoded_img
                        image_attached = True
                        print(f"👁️ [Gemini Multimodal] Attached frame from Zero-Disk RAM buffer ({pil_img.size})")
                    except Exception as img_err:
                        print(f"⚠️ [Gemini Vision] RAM buffer decode failed: {img_err}")
            else:
                print("⚡ [Vision Mode OFF] Strictly text-only streaming inference active.")

            full_prompt = build_gemini_prompt(user_text, chat_history, has_image=image_attached)
            contents = [full_prompt]
            if image_attached and pil_img is not None:
                contents.append(pil_img)

            # Step B: Streaming Clause-Based TTS Pipeline
            global current_audio_queue, active_playback_task
            barge_in_event.clear()
            audio_queue = asyncio.Queue()
            current_audio_queue = audio_queue

            async def audio_player_worker():
                global is_playback_streaming, last_esp32_ip
                is_playback_streaming = True
                dashboard_state["is_playback_streaming"] = True
                try:
                    while True:
                        if barge_in_event.is_set():
                            break
                        item = await audio_queue.get()
                        if item is None or barge_in_event.is_set():
                            audio_queue.task_done()
                            break
                        pcm_bytes, wav_bytes, clause_text = item
                        if barge_in_event.is_set():
                            audio_queue.task_done()
                            break
                        try:
                            dashboard_state["pipeline_status"] = f"Speaking: \"{clause_text}\""
                            await asyncio.gather(
                                play_audio_locally(wav_bytes),
                                stream_audio_to_glasses(pcm_bytes, last_esp32_ip, manage_vad_mute=False)
                            )
                        except Exception as play_err:
                            print(f"❌ [Playback Error]: {play_err}")
                        finally:
                            audio_queue.task_done()
                finally:
                    if not barge_in_event.is_set():
                        await asyncio.sleep(0.35)
                    is_playback_streaming = False
                    dashboard_state["is_playback_streaming"] = False
                    print("🎙️ [Playback] All streaming clauses completed. VAD listening un-muted.")

            player_task = asyncio.create_task(audio_player_worker())
            active_playback_task = player_task

            clause_idx = 0
            collected_clauses = []
            llm_first_token_sec = 0.0
            tts_first_chunk_sec = 0.0

            # Stream Gemini output asynchronously
            async for clause in stream_gemini_clauses_async(contents, timing_meta):
                if barge_in_event.is_set():
                    print("🛑 [Gemini Generation] Clause generation aborted mid-stream via Barge-In.")
                    break
                if clause_idx == 0:
                    llm_first_token_sec = timing_meta.get("llm_first_token", time.perf_counter() - t_llm_start)
                collected_clauses.append(clause)

                # Synthesize clause directly in memory
                pcm_bytes, wav_bytes = await synthesize_clause_pcm(clause)

                if clause_idx == 0:
                    t_tts_ready = time.perf_counter()
                    tts_first_chunk_sec = t_tts_ready - t_llm_start
                    total_latency_sec = t_tts_ready - timing_meta["t_vad_end"]
                    timing_meta["tts_first_chunk"] = tts_first_chunk_sec
                    timing_meta["total"] = total_latency_sec

                clause_idx += 1
                if pcm_bytes and wav_bytes:
                    await audio_queue.put((pcm_bytes, wav_bytes, clause))

            full_llm_sec = timing_meta.get("full_llm", time.perf_counter() - t_llm_start)

            # Signal player worker that all clauses are queued
            await audio_queue.put(None)

            # Clean Benchmarking Telemetry printout:
            total_sec = timing_meta.get("total", full_llm_sec + tts_first_chunk_sec)
            print(
                f"\n🚀 [TIMING] VAD -> STT: {timing_meta.get('vad_to_stt', 0.0):.2f}s | "
                f"Wake Word Check: {timing_meta.get('wake_check', 0.0):.2f}s | "
                f"LLM First Token: {llm_first_token_sec:.2f}s | "
                f"Full LLM: {full_llm_sec:.2f}s | "
                f"TTS First Chunk: {tts_first_chunk_sec:.2f}s | "
                f"Total: {total_sec:.2f}s\n"
            )

            latest_latency["vad_to_stt"] = round(timing_meta.get('vad_to_stt', 0.0), 2)
            latest_latency["llm_first_token"] = round(llm_first_token_sec, 2)
            latest_latency["full_llm"] = round(full_llm_sec, 2)
            latest_latency["tts_first_chunk"] = round(tts_first_chunk_sec, 2)
            latest_latency["total"] = round(total_sec, 2)
            dashboard_state["latest_latency"] = latest_latency

            await player_task

            full_ai_text = " ".join(collected_clauses).strip()
            if not full_ai_text:
                full_ai_text = "I couldn't generate a response."
            print(f"🤖 Full AI Response: \"{full_ai_text}\"")

            ai_msg = {
                "id": f"msg_a_{int(datetime.now().timestamp()*1000)}",
                "role": "assistant",
                "text": full_ai_text,
                "time": datetime.now().strftime("%H:%M:%S"),
                "audio_url": None,
                "has_vision": image_attached
            }
            chat_history.append(ai_msg)
            if len(chat_history) > 60:
                chat_history.pop(0)

        except Exception as e:
            print(f"❌ Error in Pipeline: {e}\n{traceback.format_exc()}")
        finally:
            dashboard_state["pipeline_status"] = "Ready"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global audio_protocol_instance
    loop = asyncio.get_running_loop()

    # 0. Pre-generate and load Zero-Latency UI Audio Cache
    await init_ui_audio_cache()

    # 1. Setup UDP audio listener for ESP32 microphone stream (Port 9000)
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: AudioUDPProtocol(),
        local_addr=("0.0.0.0", 9000)
    )
    audio_protocol_instance = protocol
    print("🟢 Dedicated OpenWakeWord & VAD Audio Pipeline active on UDP port 9000")

    # 2. Setup TCP playback server for ESP32 MAX98357A speaker downlink (Port 9001)
    playback_server = await asyncio.start_server(
        handle_playback_client,
        host="0.0.0.0",
        port=PLAYBACK_TCP_PORT
    )
    print(f"🔊 Playback TCP Server listening on port {PLAYBACK_TCP_PORT}")

    try:
        yield
    finally:
        transport.close()
        playback_server.close()
        await playback_server.wait_closed()
        print("🛑 Audio & Playback Servers shutdown.")


os.makedirs("static", exist_ok=True)
app = FastAPI(title="NextSight AI Glasses Backend", lifespan=lifespan)

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/captured_frames", StaticFiles(directory="captured_frames"), name="captured_frames")
app.mount("/audio_responses", StaticFiles(directory="audio_responses"), name="audio_responses")


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Serves the NextSight real-time HUD and chat interface."""
    index_file = os.path.join("static", "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return HTMLResponse("<h1>NextSight AI Glasses</h1><p>index.html not found in static/</p>")


@app.get("/api/state")
async def get_dashboard_state():
    """Returns current real-time telemetry, dialogue, and gallery state for the web HUD."""
    global dashboard_state, last_esp32_ip, is_playback_streaming, gallery_images, vision_mode_active, audio_state, latest_latency, chat_history
    dashboard_state["esp32_ip"] = last_esp32_ip
    dashboard_state["is_playback_streaming"] = is_playback_streaming
    dashboard_state["vision_mode_active"] = vision_mode_active
    dashboard_state["audio_state"] = audio_state
    dashboard_state["system_time"] = datetime.now().strftime("%H:%M:%S")
    dashboard_state["gallery_images"] = gallery_images
    dashboard_state["chat_history"] = chat_history
    dashboard_state["latest_latency"] = latest_latency
    dashboard_state["notes"] = load_notes()
    dashboard_state["active_timers"] = get_active_timers_list()

    now_ts = int(datetime.now().timestamp() * 1000)
    if os.path.exists("captured_frames/raw_frame.jpg"):
        dashboard_state["latest_raw_frame"] = f"/captured_frames/raw_frame.jpg?t={now_ts}"
    else:
        dashboard_state["latest_raw_frame"] = ""

    if os.path.exists("captured_frames/enhanced_frame.jpg"):
        dashboard_state["latest_enhanced_frame"] = f"/captured_frames/enhanced_frame.jpg?t={now_ts}"
    else:
        dashboard_state["latest_enhanced_frame"] = ""

    dashboard_state["latest_frame_url"] = dashboard_state["latest_enhanced_frame"] or dashboard_state["latest_raw_frame"]

    return dashboard_state


async def announce_touch_event(text: str):
    """Synthesizes and plays a short touch action confirmation on PC and glasses speaker."""
    global last_esp32_ip
    try:
        pcm_bytes, wav_bytes = await synthesize_speech(text)
        if wav_bytes and pcm_bytes:
            await asyncio.gather(
                play_audio_locally(wav_bytes),
                stream_audio_to_glasses(pcm_bytes, last_esp32_ip)
            )
    except Exception as e:
        print(f"❌ [Touch Announcement Error]: {e}")


@app.post("/api/touch/single")
async def touch_single_click():
    """Single Tap on TTP223: If assistant is speaking, acts as instant tactile Barge-In mute. Otherwise toggles vision."""
    global vision_mode_active, dashboard_state, audio_protocol_instance, audio_state, is_playback_streaming

    try:
        import pygame
        is_busy = pygame.mixer.get_init() and pygame.mixer.get_busy()
    except Exception:
        is_busy = False

    if is_playback_streaming or is_busy:
        trigger_barge_in(source="Temple Touch Tap")
        return {
            "status": "success",
            "action": "barge_in_stop",
            "message": "Playback interrupted via temple tap."
        }

    vision_mode_active = not vision_mode_active
    dashboard_state["vision_mode_active"] = vision_mode_active

    if vision_mode_active:
        event_key = "vision_on"
        prompt_text = "Vision mode active."
        if audio_protocol_instance:
            audio_protocol_instance.set_active_listening(reason="Instant Vision Single Tap")
    else:
        event_key = "vision_off"
        prompt_text = "Vision mode disabled."
        if audio_protocol_instance:
            audio_protocol_instance.set_idle()

    print(f"👆 [Touch Single Tap] Toggled Vision Mode -> {prompt_text} (Active: {vision_mode_active}, Audio State: {audio_state})")
    asyncio.create_task(play_cached_ui_event(event_key))
    return {
        "status": "success",
        "action": "toggle_vision",
        "vision_mode_active": vision_mode_active,
        "audio_state": audio_state,
        "message": prompt_text
    }


@app.post("/api/touch/double")
async def touch_double_click():
    """Double Tap on TTP223: Calls enhance_image_rgb to save snapshot to gallery and plays cached audio."""
    global dashboard_state, gallery_images
    print("👆 [Touch Double Tap] Saving high-res snapshot to gallery...")
    raw_path = os.path.join("captured_frames", "raw_frame.jpg")
    loop = asyncio.get_running_loop()
    snapshot_url = await loop.run_in_executor(None, enhance_image_rgb, raw_path)
    if snapshot_url:
        if snapshot_url not in gallery_images:
            gallery_images.insert(0, snapshot_url)
        dashboard_state["gallery_images"] = gallery_images

    prompt_text = "Snapshot saved."
    asyncio.create_task(play_cached_ui_event("snapshot"))
    return {
        "status": "success",
        "action": "snapshot",
        "snapshot_url": snapshot_url,
        "message": prompt_text
    }


@app.post("/api/touch/long")
async def touch_long_press():
    """Long Press on TTP223: Clears chat_history memory and instantly plays cached audio."""
    global chat_history, dashboard_state
    chat_history.clear()
    dashboard_state["chat_history"] = chat_history
    prompt_text = "Memory cleared."
    print("👆 [Touch Long Press] Memory / chat history cleared.")
    asyncio.create_task(play_cached_ui_event("memory_cleared"))
    return {
        "status": "success",
        "action": "clear_memory",
        "message": prompt_text
    }


@app.post("/api/memory/clear")
async def api_clear_memory():
    """Explicit endpoint to clear conversational memory."""
    global chat_history, dashboard_state
    chat_history.clear()
    dashboard_state["chat_history"] = chat_history
    print("🧹 [/api/memory/clear] Memory cleared via API.")
    asyncio.create_task(play_cached_ui_event("memory_cleared"))
    return {
        "status": "success",
        "action": "clear_memory",
        "message": "Conversation memory cleared."
    }


# --- Notes & Scratchpad REST Endpoints ---

class NoteCreateRequest(BaseModel):
    text: str


@app.get("/api/notes")
async def api_get_notes():
    """Retrieve all persistent notes."""
    return {"status": "success", "notes": load_notes()}


@app.post("/api/notes")
async def api_create_note(req: NoteCreateRequest):
    """Create a new persistent note from the Web UI."""
    entry = save_note_entry(req.text)
    if not entry:
        return {"status": "error", "message": "Note text cannot be empty"}
    dashboard_state["notes"] = load_notes()
    return {"status": "success", "note": entry, "notes": dashboard_state["notes"]}


@app.delete("/api/notes/{note_id}")
async def api_delete_note(note_id: str):
    """Delete a note by ID."""
    deleted = delete_note_entry(note_id)
    dashboard_state["notes"] = load_notes()
    return {"status": "success" if deleted else "not_found", "notes": dashboard_state["notes"]}


@app.post("/api/notes/clear")
async def api_clear_notes():
    """Clear all persistent notes."""
    clear_all_notes_entries()
    dashboard_state["notes"] = []
    return {"status": "success", "message": "All notes cleared", "notes": []}


# --- Weather REST Endpoint ---

@app.get("/api/weather")
async def api_get_weather(city: str | None = None):
    """Fetches live weather on-demand for local or specified city."""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, fetch_live_weather, city)
    if "error" in data:
        return {"status": "error", "message": data["error"]}
    dashboard_state["latest_weather"] = data
    return {"status": "success", "weather": data}


# --- Timers & Reminders REST Endpoints ---

class TimerCreateRequest(BaseModel):
    duration_sec: int
    label: str = "Timer"
    is_reminder: bool = False


@app.get("/api/timers")
async def api_get_timers():
    """Retrieve all active countdown timers & reminders."""
    return {"status": "success", "timers": get_active_timers_list()}


@app.post("/api/timers")
async def api_create_timer(req: TimerCreateRequest):
    """Create a new countdown timer or reminder from Web UI."""
    if req.duration_sec <= 0:
        return {"status": "error", "message": "Duration must be greater than 0 seconds"}
    t_info = create_timer(req.duration_sec, req.label, req.is_reminder)
    return {"status": "success", "timer": t_info, "timers": get_active_timers_list()}


@app.delete("/api/timers/{timer_id}")
async def api_cancel_timer(timer_id: str):
    """Cancel a specific timer by ID."""
    cancelled = cancel_timer(timer_id)
    return {"status": "success" if cancelled else "not_found", "timers": get_active_timers_list()}


@app.post("/api/timers/clear")
async def api_clear_timers():
    """Cancel all active timers."""
    count = cancel_all_timers()
    return {"status": "success", "cancelled_count": count, "timers": []}


@app.post("/api/playback/barge-in")
@app.post("/api/playback/stop")
async def api_barge_in():
    """Immediately interrupts and halts active TTS audio playback, clearing buffers."""
    interrupted = trigger_barge_in(source="Web Cockpit API")
    return {
        "status": "success",
        "interrupted": interrupted,
        "is_playback_streaming": is_playback_streaming,
        "audio_state": audio_state
    }


@app.post("/api/snapshot")
async def api_trigger_snapshot():
    """Triggers an RGB snapshot and voice confirmation from the web UI."""
    asyncio.create_task(run_full_ai_pipeline("take a snapshot"))
    return {"status": "processing", "command": "take a snapshot"}


class AskRequest(BaseModel):
    text: str


@app.post("/api/ask")
async def ask_query(req: AskRequest):
    """Allows testing voice/vision queries directly from the Web UI."""
    query = req.text.strip()
    if not query:
        return {"status": "error", "message": "Empty query"}
    asyncio.create_task(run_full_ai_pipeline(query))
    return {"status": "processing", "query": query}


# --- Hardware Simulation Endpoints ---

class SimulateTouchRequest(BaseModel):
    action: str  # "single" | "double" | "long"


@app.post("/api/simulate/touch")
async def simulate_touch(req: SimulateTouchRequest):
    """Simulates physical touch interrupts on the TTP223 sensor: single, double, or long."""
    act = req.action.lower().strip()
    if act == "single":
        return await touch_single_click()
    elif act == "double":
        return await touch_double_click()
    elif act == "long":
        return await touch_long_press()
    else:
        return {"status": "error", "message": f"Unknown action '{act}'. Use 'single', 'double', or 'long'."}


class SimulateTextQueryRequest(BaseModel):
    text: str


@app.post("/api/simulate/text-query")
async def simulate_text_query(req: SimulateTextQueryRequest):
    """Simulates transcribed speech query directly into the AI pipeline without microphone input."""
    query = req.text.strip()
    if not query:
        return {"status": "error", "message": "Empty text query"}

    # Strip any leading wake word residue (e.g. 'hey meta', 'alexa', 'hey jarvis', 'hey mycroft')
    import re
    clean_query = re.sub(r'(?i)\b(alexa|hey\s+mycroft|hey\s+meta|hey\s+jarvis)\b', '', query).strip(" ,:;.!?")
    if not clean_query:
        clean_query = query.strip()

    t_start = time.perf_counter()
    timing_meta = {
        "t_vad_end": t_start,
        "vad_to_stt": 0.05,
        "wake_check": 0.01,
        "t_llm_start": t_start
    }
    print(f"🧪 [Simulate Text Query] \"{clean_query}\" (Raw: \"{query}\", Vision Mode: {vision_mode_active})")

    # Preemptively barge in if audio playback was active from previous response
    try:
        import pygame
        is_busy = pygame.mixer.get_init() and pygame.mixer.get_busy()
    except Exception:
        is_busy = False
    if is_playback_streaming or is_busy:
        trigger_barge_in(source="Simulated Query Preemption")

    async def _run_sim():
        try:
            await run_full_ai_pipeline(clean_query, timing_meta)
        finally:
            if audio_protocol_instance:
                audio_protocol_instance.set_active_listening(reason="Simulated Text Query Follow-Up")

    asyncio.create_task(_run_sim())
    return {
        "status": "processing",
        "original_query": query,
        "clean_query": clean_query,
        "vision_mode_active": vision_mode_active
    }


@app.post("/api/simulate/upload-frame")
async def simulate_upload_frame(request: Request, file: UploadFile | None = None):
    """Injects a test image directly into the in-memory RAM vision buffer and OCR enhancer."""
    global latest_enhanced_frame_bytes, latest_raw_frame_bytes, latest_frame_timestamp, dashboard_state
    try:
        raw_bytes = None
        if file is not None:
            raw_bytes = await file.read()
        else:
            raw_bytes = await request.body()

        if not raw_bytes:
            return {"status": "error", "message": "Empty file or image body received"}

        raw_path = "captured_frames/raw_frame.jpg"
        enhanced_path = "captured_frames/enhanced_frame.jpg"
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, enhance_frame_for_ocr, raw_bytes, raw_path, enhanced_path)

        now_ts = int(datetime.now().timestamp() * 1000)
        dashboard_state["last_image_time"] = datetime.now().strftime("%H:%M:%S")
        dashboard_state["last_image_bytes"] = len(raw_bytes)
        dashboard_state["latest_raw_frame"] = f"/captured_frames/raw_frame.jpg?t={now_ts}"
        dashboard_state["latest_enhanced_frame"] = f"/captured_frames/enhanced_frame.jpg?t={now_ts}"
        dashboard_state["latest_frame_url"] = dashboard_state["latest_enhanced_frame"]

        print(f"📸 [Simulate Frame] Injected test image: {len(raw_bytes)} bytes into vision buffer")
        return {
            "status": "success",
            "message": "Test frame successfully injected into zero-disk vision buffer",
            "bytes": len(raw_bytes),
            "url": dashboard_state["latest_frame_url"]
        }
    except Exception as e:
        print(f"❌ [Simulate Frame Error]: {e}")
        return {"status": "error", "message": str(e)}


class GalleryDeleteRequest(BaseModel):
    url: str


@app.post("/api/gallery/delete")
async def delete_gallery_image(req: GalleryDeleteRequest):
    """Removes a snapshot from the gallery and filesystem."""
    global gallery_images, dashboard_state
    target_url = req.url.split("?")[0].strip()
    if target_url in gallery_images:
        gallery_images.remove(target_url)
        dashboard_state["gallery_images"] = gallery_images

    if target_url.startswith("/captured_frames/"):
        file_path = target_url.lstrip("/")
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                print(f"🗑️ [Gallery Delete] Removed file: {file_path}")
            except Exception as e:
                print(f"⚠️ [Gallery Delete Error]: {e}")

    return {"status": "success", "deleted": target_url, "remaining": len(gallery_images)}


@app.get("/logs")
async def get_logs(lines: int = 150):
    """Returns the most recent console/server log entries."""
    if not os.path.exists("server.log"):
        return {"logs": "server.log is empty or not yet created."}
    try:
        with open("server.log", "r", encoding="utf-8", errors="replace") as f:
            log_lines = f.readlines()
        return {"logs": "".join(log_lines[-lines:])}
    except Exception as e:
        return {"error": str(e)}


@app.get("/test_speaker")
async def test_speaker():
    """Generates a test voice prompt, plays on PC speakers, and streams over UDP to ESP32."""
    test_text = "Hello! NextSight audio playback is working on your computer and streaming to your glasses."
    print("🔔 [/test_speaker] Generating test speech...")
    pcm_bytes, wav_bytes = await synthesize_speech(test_text)
    if wav_bytes and pcm_bytes:
        await asyncio.gather(
            play_audio_locally(wav_bytes),
            stream_audio_to_glasses(pcm_bytes, last_esp32_ip)
        )
    return {
        "status": "success",
        "message": f"Played on PC speakers and streamed to ESP32 at {last_esp32_ip}:9001",
        "target_ip": last_esp32_ip
    }


@app.get("/playback/status")
async def playback_status():
    """Returns current status of the audio playback downlink."""
    return {
        "esp32_ip": last_esp32_ip,
        "is_playback_streaming": is_playback_streaming
    }


def enhance_frame_for_ocr(raw_bytes: bytes, raw_path: str, enhanced_path: str):
    """Zero-disk memory pipeline: decodes raw JPEG directly from RAM, applies
    Grayscale + CLAHE + 2D Sharpening kernel, updates global RAM buffer, and saves asynchronously."""
    global latest_enhanced_frame_bytes, latest_raw_frame_bytes, latest_frame_timestamp
    try:
        latest_raw_frame_bytes = raw_bytes
        # 1. Decode directly from in-memory byte buffer (ZERO DISK READ)
        nparr = np.frombuffer(raw_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return

        # 2. Convert to Grayscale
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # 3. Contrast Limited Adaptive Histogram Equalization (CLAHE)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        equalized = clahe.apply(gray)

        # 4. Standard Sharpening Kernel (3x3 matrix) to enhance letter edges
        sharpen_kernel = np.array([
            [0, -1, 0],
            [-1, 5, -1],
            [0, -1, 0]
        ], dtype=np.float32)
        enhanced = cv2.filter2D(equalized, -1, sharpen_kernel)

        # 5. Encode enhanced image to JPEG bytes directly in RAM
        success, enc_buf = cv2.imencode(".jpg", enhanced, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        if success:
            enhanced_bytes = enc_buf.tobytes()
            latest_enhanced_frame_bytes = enhanced_bytes
            latest_frame_timestamp = datetime.now().timestamp()
        else:
            enhanced_bytes = None

        # 6. Save to disk asynchronously so HTTP/dashboard static files continue to work
        try:
            with open(raw_path, "wb") as f:
                f.write(raw_bytes)
            if enhanced_bytes:
                with open(enhanced_path, "wb") as f:
                    f.write(enhanced_bytes)
            else:
                cv2.imwrite(enhanced_path, enhanced)
        except Exception:
            pass
    except Exception as e:
        print(f"❌ [OpenCV In-Memory Enhancement Error]: {e}")


@app.websocket("/ws/vision")
async def websocket_vision(websocket: WebSocket):
    """Zero-Latency Binary WebSocket Vision Stream directly from ESP32 camera framebuffer."""
    global last_esp32_ip, dashboard_state
    await websocket.accept()
    client_ip = websocket.client.host if websocket.client else "unknown"
    last_esp32_ip = client_ip
    dashboard_state["esp32_ip"] = client_ip
    print(f"👁️ [WebSocket Vision] ESP32 Connected from {client_ip}")

    raw_path = "captured_frames/raw_frame.jpg"
    enhanced_path = "captured_frames/enhanced_frame.jpg"
    loop = asyncio.get_running_loop()

    try:
        while True:
            frame_bytes = await websocket.receive_bytes()
            if not frame_bytes:
                continue

            # Pass incoming JPEG bytes directly through in-memory OpenCV enhancement
            await loop.run_in_executor(None, enhance_frame_for_ocr, frame_bytes, raw_path, enhanced_path)

            dashboard_state["last_image_time"] = datetime.now().strftime("%H:%M:%S")
            dashboard_state["last_image_bytes"] = len(frame_bytes)
            dashboard_state["esp32_ip"] = client_ip

    except WebSocketDisconnect:
        print(f"🔌 [WebSocket Vision] ESP32 Disconnected ({client_ip})")
    except Exception as e:
        print(f"❌ [WebSocket Vision Error]: {e}")


@app.post("/upload")
async def upload_frame(request: Request):
    """Receives camera frame from ESP32, updates zero-disk RAM vision buffer, and persists in background."""
    global last_esp32_ip
    try:
        if request.client and request.client.host:
            last_esp32_ip = request.client.host

        body = await request.body()
        if not body:
            return {"status": "error", "message": "Empty body"}

        timestamp = datetime.now().strftime("%H%M%S_%f")
        raw_path = "captured_frames/raw_frame.jpg"
        enhanced_path = "captured_frames/enhanced_frame.jpg"
        history_path = f"captured_frames/frame_{timestamp}.jpg"

        # Run OpenCV in-memory pipeline in worker thread to prevent blocking event loop
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, enhance_frame_for_ocr, body, raw_path, enhanced_path)

        # Save historical snapshot in background daemon thread
        def _save_history():
            try:
                with open(history_path, "wb") as f:
                    f.write(body)
            except Exception:
                pass
        loop.run_in_executor(None, _save_history)

        # Update dashboard state
        dashboard_state["last_image_time"] = datetime.now().strftime("%H:%M:%S")
        dashboard_state["last_image_bytes"] = len(body)
        dashboard_state["esp32_ip"] = last_esp32_ip

        return {"status": "success"}
    except asyncio.CancelledError:
        return {"status": "server_stopping"}
    except Exception as e:
        print(f"❌ [/upload Error]: {e}")
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    print("🚀 Starting NextSight Backend...")
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)