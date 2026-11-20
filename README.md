# NextSight: IoT-Enabled Wearable Spatial Computing Platform

NextSight is an open-architecture, displayless Wearable IoT (WIoT) smart glasses platform engineered for real-time auditory assistance, spatial perception, and multimodal edge intelligence.

---

## 📁 Repository Directory Structure

```text
NextSight/
├── cad/                                  # 3D Printing CAD Files & OpenSCAD Source
│   ├── nextsight_glasses.scad            # Master parametric CAD model (OpenSCAD)
│   ├── front_frame.stl                   # Front frame with 21mm bridge & camera mount
│   ├── left_temple.stl                   # Left temple (enclosing 3.7V LiPo & MAX98357A)
│   ├── right_temple.stl                  # Right temple (enclosing ESP32-S3, mic & touch)
│   ├── bridge_wire_channel_lid.stl       # Snap-in retention cover for bridge wire conduit
│   └── nextsight_glasses_all_parts.stl   # Complete single-bed 3D print layout
│
├── firmware/                             # ESP32-S3 Dual-Core FreeRTOS Firmware
│   └── firmware.ino                      # Production firmware (I2S DMA, dynamic noise gate, mDNS fallback)
│
├── static/                               # Real-Time Web Cockpit & Neural HUD
│   ├── docs/                             # Hardware schematics & architectural diagrams
│   ├── presets/                          # Vision simulation test scenes
│   └── index.html                        # Bento Grid HUD (viewfinder, telemetry, simulator bay)
│
├── tests/                                # Automated Verification & Quality Assurance Suites
│   ├── test_hardware_system_readiness.py # End-to-end hardware protocol simulation (WS, UDP, HTTP)
│   ├── test_barge_in_feature.py          # 4-way barge-in interruptibility validation
│   ├── test_timers_feature.py            # Natural language countdown timers & reminders test
│   ├── test_weather_feature.py           # Open-Meteo live meteorological forecasting test
│   ├── test_notes_feature.py             # Wearable notes & persistent JSON locking test
│   └── test_stt.py                       # Faster-Whisper transcription benchmark
│
├── .env.example                          # Environment variable configuration template
├── .gitignore                            # Git ignore specifications
├── notes.json                            # Persistent wearable notes storage
├── requirements.txt                      # Python dependencies specification
├── server.py                             # Edge Gateway Server (FastAPI, Whisper, Gemini, Edge-TTS)
└── README.md                             # Project documentation
```

---

## ⚡ Quick Start Guide

### 1. Configure Environment Variables
Copy `.env.example` to `.env` and fill in your Gemini API key:
```bash
cp .env.example .env
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Start the Edge Gateway Server
```bash
python server.py
```
* **Web Cockpit HUD:** Open `http://localhost:8000` in any web browser.
* **Microphone UDP Ingest:** Listening on `0.0.0.0:9000`.
* **Speaker UDP Downlink:** Streaming to `0.0.0.0:9001`.

### 4. Run Automated Verification Tests
Execute any test suite from the repository root:
```bash
# Verify all hardware protocols and LAN reachability:
python tests/test_hardware_system_readiness.py

# Verify Instant Multi-Modal Barge-In interruptibility:
python tests/test_barge_in_feature.py

# Verify Voice Reminders and Countdown Timers:
python tests/test_timers_feature.py

# Verify Weather Forecasting:
python tests/test_weather_feature.py

# Verify Persistent Wearable Notes:
python tests/test_notes_feature.py
```

### 5. Flash the Physical ESP32-S3 Hardware
Open `firmware/firmware.ino` in Arduino IDE and set:
* **Board:** `ESP32S3 Dev Module` (or `XIAO_ESP32S3`)
* **PSRAM:** `OPI PSRAM` (*Crucial for camera XGA framebuffers*)
* **Partition Scheme:** `Huge APP (3MB No OTA / 1MB SPIFFS)`
* **USB CDC On Boot:** `Enabled`

---

## 🔌 Hardware Master Pinout Table

| Component | Signal | ESP32-S3 GPIO | Description |
| :--- | :--- | :--- | :--- |
| **INMP441 MEMS Mic** | `BCLK` | `GPIO 7` | Shared I2S Master Clock (1.024 MHz) |
| | `LRC` | `GPIO 8` | Shared I2S Word Select (16 kHz) |
| | `SD / DOUT` | `GPIO 4` | Digital Serial Audio to RX DMA |
| | `L/R` | `GND` | Left channel stereo slot |
| **MAX98357A Amp** | `BCLK` | `GPIO 7` | Shared I2S Master Clock |
| | `LRC` | `GPIO 8` | Shared I2S Word Select |
| | `DIN` | `GPIO 9` | Digital Serial Audio from TX DMA |
| | `GAIN` | `GND` | +12dB output gain |
| **TTP223 Touch Sensor**| `SIG / IO`| `GPIO 1 (D0)`| Active-HIGH capacitive touch interrupt |
| **OV3660 Camera** | `XCLK` | `GPIO 10` | 16MHz Master Clock |
| | `SIOD / SIOC`| `GPIO 40 / 39`| Camera Sensor Control Bus (SCCB) |
| | `D0 - D7` | `GPIO 11,12,14-18,48`| 8-bit Parallel Video Data Port (DVP) |
