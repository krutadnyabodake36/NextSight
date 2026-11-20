#include "esp_camera.h"
#include <ESPmDNS.h>
#include <HTTPClient.h>
#include <OneButton.h>
#include <WebSocketsClient.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <driver/i2s.h>

// --- Network Configuration ---
const char *WIFI_SSID = "VivoT3pro";
const char *WIFI_PASSWORD = "krutadnya911";

// Primary laptop/PC hostname for mDNS auto-discovery (e.g., "LAPTOP-NBHBH25N")
const char *LAPTOP_HOSTNAME = "LAPTOP-NBHBH25N";

// Direct Fallback Server IP if mDNS is blocked by Windows Defender Firewall
const char *FALLBACK_SERVER_IP = "10.55.117.111";

IPAddress serverIP;
String serverIPString = "";
const int AUDIO_UDP_PORT = 9000;
const int VISION_HTTP_PORT = 8000;
const int PLAYBACK_UDP_PORT = 9001;

// --- TTP223 Touch Sensor Pin & Configuration ---
#ifndef D0
#define D0 1 // Pin D0 (or GPIO 1 on ESP32-S3 boards)
#endif
#define TOUCH_PIN D0

// TTP223 capacitive touch sensor (Active HIGH when touched, activeLow = false,
// pullup = false)
OneButton touchSensor(TOUCH_PIN, false, false);

// --- WebSocket Camera Client ---
WebSocketsClient webSocket;
bool wsConnected = false;

// --- Camera Pin Definitions (OV3660) ---
#define PWDN_GPIO_NUM -1
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 10
#define SIOD_GPIO_NUM 40
#define SIOC_GPIO_NUM 39
#define Y9_GPIO_NUM 48
#define Y8_GPIO_NUM 11
#define Y7_GPIO_NUM 12
#define Y6_GPIO_NUM 14
#define Y5_GPIO_NUM 16
#define Y4_GPIO_NUM 18
#define Y3_GPIO_NUM 17
#define Y2_GPIO_NUM 15
#define VSYNC_GPIO_NUM 38
#define HREF_GPIO_NUM 47
#define PCLK_GPIO_NUM 13

// --- Audio Pin Definitions ---
#define I2S_BCLK 7
#define I2S_LRC 8
#define I2S_DIN 4
#define I2S_DOUT 9
#define I2S_PORT I2S_NUM_0
#define SAMPLE_RATE 16000
#define AUDIO_BUF_LEN 512

// Hardware Noise Gate: Drop ambient silence packets to save bandwidth & Wi-Fi
// transmission power Lowered to 200 to capture normal/soft conversational
// speech effortlessly
const int16_t NOISE_FLOOR_THRESHOLD = 200;

WiFiUDP udp;        // Uplink: Mic -> Server (Port 9000)
WiFiUDP udpAudioRx; // Downlink: Server -> Speaker (Port 9001)
TaskHandle_t AudioTaskHandle = NULL;

// Acoustic feedback suppression & volume control
volatile bool isPlaying = false;
const float PLAYBACK_VOLUME = 1.0f; // 100% full scale output

// Non-blocking timer for camera frame upload over WebSocket
unsigned long last_camera_upload_ms = 0;
const unsigned long CAMERA_UPLOAD_INTERVAL_MS = 1000;

// --- WebSocket Event Callback ---
void webSocketEvent(WStype_t type, uint8_t *payload, size_t length) {
  switch (type) {
  case WStype_DISCONNECTED:
    wsConnected = false;
    Serial.println("🔌 [WS Camera] Disconnected from server.");
    break;
  case WStype_CONNECTED:
    wsConnected = true;
    Serial.printf("👁️ [WS Camera] Connected to ws://%s:%d/ws/vision\n",
                  serverIPString.c_str(), VISION_HTTP_PORT);
    break;
  case WStype_TEXT:
  case WStype_BIN:
  case WStype_ERROR:
    break;
  }
}

// --- Helper Function: Send Touch Event via HTTP POST ---
void sendTouchEvent(const char *endpoint) {
  if (WiFi.status() == WL_CONNECTED && serverIP != IPAddress(0, 0, 0, 0)) {
    HTTPClient http;
    String url =
        "http://" + serverIPString + ":" + String(VISION_HTTP_PORT) + endpoint;
    http.begin(url);
    http.addHeader("Content-Type", "application/json");
    int httpResponseCode = http.POST("{}");
    if (httpResponseCode > 0) {
      Serial.printf("👆 [Touch] Event '%s' sent! HTTP %d\n", endpoint,
                    httpResponseCode);
    } else {
      Serial.printf("❌ [Touch] Error sending '%s': %s\n", endpoint,
                    http.errorToString(httpResponseCode).c_str());
    }
    http.end();
  }
}

// --- OneButton Callbacks ---
void onTouchSingleClick() {
  Serial.println("👆 [Touch Single Tap] -> /api/touch/single (Toggle Vision)");
  sendTouchEvent("/api/touch/single");
}

void onTouchDoubleClick() {
  Serial.println("👆 [Touch Double Tap] -> /api/touch/double (Snapshot)");
  sendTouchEvent("/api/touch/double");
}

void onTouchLongPressStart() {
  Serial.println("👆 [Touch Long Press] -> /api/touch/long (Clear Memory)");
  sendTouchEvent("/api/touch/long");
}

// --- Core 0: Full-Duplex Audio Task with Hardware Noise Gate ---
void audioTask(void *pvParameters) {
  static int32_t mic_buffer[AUDIO_BUF_LEN];
  static int16_t tx_buffer[AUDIO_BUF_LEN];
  static int16_t rx_pcm[AUDIO_BUF_LEN];
  static int32_t playback_buffer[AUDIO_BUF_LEN];
  size_t bytes_read = 0;
  size_t bytes_written = 0;
  unsigned long last_playback_ms = 0;

  for (;;) {
    // 1. Check for incoming UDP audio packets from the server (Port 9001)
    int packetSize = udpAudioRx.parsePacket();
    if (packetSize > 0) {
      isPlaying = true; // Mute outbound mic to prevent feedback loop
      last_playback_ms = millis();

      int bytesToRead = min((int)sizeof(rx_pcm), packetSize);
      int len = udpAudioRx.read((char *)rx_pcm, bytesToRead);

      if (len > 0) {
        int samples = len / sizeof(int16_t);

        // Convert 16-bit PCM to 32-bit signed integers for I2S slot
        for (int i = 0; i < samples; i++) {
          int32_t val = (int32_t)(rx_pcm[i] * PLAYBACK_VOLUME);
          playback_buffer[i] = val << 16;
        }

        i2s_write(I2S_PORT, playback_buffer, samples * sizeof(int32_t),
                  &bytes_written, pdMS_TO_TICKS(20));
      }
    } else {
      // Acoustic hangover: wait 350ms after last packet before unmuting mic
      if (isPlaying && (millis() - last_playback_ms > 350)) {
        isPlaying = false;
        i2s_zero_dma_buffer(I2S_PORT); // Keep amplifier quiet
      }
    }

    // 2. Microphone Capture & Uplink Streaming with Dynamic Barge-In
    // Punch-Through (Port 9000)
    esp_err_t rx_err = i2s_read(I2S_PORT, mic_buffer, sizeof(mic_buffer),
                                &bytes_read, pdMS_TO_TICKS(20));
    if (rx_err == ESP_OK && bytes_read > 0 && WiFi.status() == WL_CONNECTED) {
      int samples = bytes_read / sizeof(int32_t);

      // Convert 32-bit I2S down to 16-bit PCM and track peak amplitude
      int16_t peak_amplitude = 0;
      for (int i = 0; i < samples; i++) {
        int32_t sample = mic_buffer[i] >> 15;
        if (sample > 32767)
          sample = 32767;
        else if (sample < -32768)
          sample = -32768;
        tx_buffer[i] = (int16_t)sample;

        int16_t abs_sample = abs((int16_t)sample);
        if (abs_sample > peak_amplitude) {
          peak_amplitude = abs_sample;
        }
      }

      // Dynamic Noise Gate:
      // - Normal Idle: threshold is NOISE_FLOOR_THRESHOLD (200) for effortless
      // conversational speech
      // - During Speaker Playback: threshold increases to 1800 so speaker audio
      // bleed is suppressed,
      //   BUT user speaking aloud punches through to trigger instant voice
      //   barge-in on the server!
      int16_t active_threshold = isPlaying ? 1800 : NOISE_FLOOR_THRESHOLD;

      if (peak_amplitude >= active_threshold) {
        udp.beginPacket(serverIP, AUDIO_UDP_PORT);
        udp.write((uint8_t *)tx_buffer, samples * sizeof(int16_t));
        udp.endPacket();
      }
    }

    vTaskDelay(pdMS_TO_TICKS(
        1)); // Yield to FreeRTOS scheduler to prevent watchdog trigger
  }
}

void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 3000)
    ;

  Serial.println("\n--- NextSight Smart Glasses ---");

  // 1. Connect to Wi-Fi
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Connecting to Wi-Fi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.printf("\nConnected! ESP32 IP: %s\n",
                WiFi.localIP().toString().c_str());

  // 2. Initialize mDNS & Dynamically Resolve Laptop Server IP
  if (!MDNS.begin("nextsight-glasses")) {
    Serial.println("❌ Error setting up MDNS responder!");
  } else {
    Serial.println("✅ mDNS responder started: nextsight-glasses.local");
  }

  Serial.printf(
      "🔍 Resolving server IP for host '%s' via mDNS (with 5s fallback)...\n",
      LAPTOP_HOSTNAME);
  int mdns_attempts = 0;
  while (serverIP == IPAddress(0, 0, 0, 0) && mdns_attempts < 5) {
    serverIP = MDNS.queryHost(LAPTOP_HOSTNAME);
    if (serverIP == IPAddress(0, 0, 0, 0)) {
      mdns_attempts++;
      Serial.printf(
          "⏳ Waiting for mDNS resolution (attempt %d/5)... retrying in 1s\n",
          mdns_attempts);
      delay(1000);
    }
  }

  if (serverIP == IPAddress(0, 0, 0, 0)) {
    serverIP.fromString(FALLBACK_SERVER_IP);
    Serial.printf("⚡ [mDNS Fallback] Windows Firewall blocked mDNS. Using "
                  "direct server IP: %s\n",
                  FALLBACK_SERVER_IP);
  } else {
    Serial.printf("🎯 Successfully Resolved Server IP via mDNS: %s\n",
                  serverIP.toString().c_str());
  }
  serverIPString = serverIP.toString();

  // 3. Initialize Camera
  camera_config_t cam_config;
  cam_config.ledc_channel = LEDC_CHANNEL_0;
  cam_config.ledc_timer = LEDC_TIMER_0;
  cam_config.pin_d0 = Y2_GPIO_NUM;
  cam_config.pin_d1 = Y3_GPIO_NUM;
  cam_config.pin_d2 = Y4_GPIO_NUM;
  cam_config.pin_d3 = Y5_GPIO_NUM;
  cam_config.pin_d4 = Y6_GPIO_NUM;
  cam_config.pin_d5 = Y7_GPIO_NUM;
  cam_config.pin_d6 = Y8_GPIO_NUM;
  cam_config.pin_d7 = Y9_GPIO_NUM;
  cam_config.pin_xclk = XCLK_GPIO_NUM;
  cam_config.pin_pclk = PCLK_GPIO_NUM;
  cam_config.pin_vsync = VSYNC_GPIO_NUM;
  cam_config.pin_href = HREF_GPIO_NUM;
  cam_config.pin_sccb_sda = SIOD_GPIO_NUM;
  cam_config.pin_sccb_scl = SIOC_GPIO_NUM;
  cam_config.pin_pwdn = PWDN_GPIO_NUM;
  cam_config.pin_reset = RESET_GPIO_NUM;
  cam_config.xclk_freq_hz = 16000000;
  cam_config.pixel_format = PIXFORMAT_JPEG;
  cam_config.frame_size = FRAMESIZE_XGA; // 1024x768 for crisp text & OCR
  cam_config.jpeg_quality = 10;          // Higher detail / lower compression
  cam_config.fb_count = 2;
  cam_config.grab_mode = CAMERA_GRAB_LATEST;

  esp_err_t cam_err = esp_camera_init(&cam_config);
  if (cam_err != ESP_OK) {
    Serial.printf("❌ Camera init failed with error 0x%x\n", cam_err);
  } else {
    Serial.println("✅ Camera initialized in XGA (1024x768) mode.");
    sensor_t *s = esp_camera_sensor_get();
    if (s != NULL) {
      s->set_sharpness(s,
                       2); // Max hardware sharpness (+2) for crisp text edges
      s->set_contrast(
          s, 1); // Boost contrast (+1) to make text pop against background
      s->set_exposure_ctrl(s, 1); // Auto-exposure enabled
      s->set_awb_gain(s, 1);      // Auto white balance enabled
      Serial.println(
          "🎯 Sensor tuned: Sharpness +2, Contrast +1, AE & AWB active.");
    }
  }

  // 4. Initialize Full-Duplex I2S
  i2s_config_t i2s_config = {
      .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX | I2S_MODE_TX),
      .sample_rate = SAMPLE_RATE,
      .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,
      .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
      .communication_format = I2S_COMM_FORMAT_STAND_I2S,
      .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
      .dma_buf_count = 6,
      .dma_buf_len = AUDIO_BUF_LEN,
      .use_apll = false,
      .tx_desc_auto_clear = true,
      .fixed_mclk = 0};
  i2s_pin_config_t pin_config = {.bck_io_num = I2S_BCLK,
                                 .ws_io_num = I2S_LRC,
                                 .data_out_num = I2S_DOUT,
                                 .data_in_num = I2S_DIN};
  i2s_driver_install(I2S_PORT, &i2s_config, 0, NULL);
  i2s_set_pin(I2S_PORT, &pin_config);

  // 5. Start Audio UDP Sockets (TX port 9000, RX port 9001)
  udp.begin(AUDIO_UDP_PORT);
  udpAudioRx.begin(PLAYBACK_UDP_PORT);
  Serial.printf("UDP Audio TX on port %d, RX on port %d\n", AUDIO_UDP_PORT,
                PLAYBACK_UDP_PORT);

  // 6. Start Audio Task on Core 0
  xTaskCreatePinnedToCore(audioTask, "AudioTask", 8192, NULL, 2,
                          &AudioTaskHandle, 0);

  // 7. Setup TTP223 Touch Sensor with OneButton on Pin D0
  touchSensor.attachClick(onTouchSingleClick);
  touchSensor.attachDoubleClick(onTouchDoubleClick);
  touchSensor.attachLongPressStart(onTouchLongPressStart);
  touchSensor.setDebounceMs(30);
  touchSensor.setClickMs(250);
  touchSensor.setPressMs(800);
  Serial.println("✅ TTP223 Touch Sensor initialized on Pin D0.");

  // 8. Initialize WebSocket Camera Client for Zero-Latency Vision Streaming
  webSocket.begin(serverIPString.c_str(), VISION_HTTP_PORT, "/ws/vision");
  webSocket.onEvent(webSocketEvent);
  webSocket.setReconnectInterval(2000);
  Serial.println(
      "🌐 WebSocket camera client configured for ws://server:8000/ws/vision");
}

// --- Core 1: WebSocket Camera Streaming & Non-blocking Touch Polling ---
void loop() {
  // 1. Maintain WebSocket connection & event loop
  webSocket.loop();

  // 2. Service Touch Sensor on every loop pass (~5ms polling)
  touchSensor.tick();

  // 3. Zero-Latency Camera Stream: push raw JPEG buffer directly over WebSocket
  unsigned long now = millis();
  if (now - last_camera_upload_ms >= CAMERA_UPLOAD_INTERVAL_MS) {
    last_camera_upload_ms = now;

    if (WiFi.status() == WL_CONNECTED && wsConnected) {
      camera_fb_t *fb = esp_camera_fb_get();
      if (fb) {
        // Direct binary frame push over open WebSocket (zero HTTP headers, zero
        // TCP handshakes!)
        webSocket.sendBIN(fb->buf, fb->len);
        esp_camera_fb_return(fb); // CRITICAL: Return buffer immediately
      }
    }
  }

  // Brief yield for FreeRTOS scheduler without blocking button polling
  vTaskDelay(pdMS_TO_TICKS(5));
}