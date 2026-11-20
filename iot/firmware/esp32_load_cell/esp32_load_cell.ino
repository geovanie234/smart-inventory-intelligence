/*
 * Smart Inventory Intelligence System — ESP32 + HX711 load-cell node
 * -------------------------------------------------------------------
 * Weighs a bulk bin / shelf and reports to the Django backend over HTTPS
 * (default) or MQTT. Readings carry a unique message id so the server can
 * drop duplicates; unacknowledged readings are kept in a small RAM queue and
 * retried, which makes delivery safe on unreliable Wi-Fi.
 *
 * Hardware: ESP32 DevKit, HX711 amplifier, load cell (e.g. 50 kg).
 *   HX711 DT  -> GPIO 16
 *   HX711 SCK -> GPIO 4
 *
 * Libraries (Arduino Library Manager): "HX711" by Bogdan Necula,
 * "ArduinoJson" (v7), "PubSubClient" (only if USE_MQTT is 1).
 *
 * Calibration:
 *   1. Set CALIBRATION_FACTOR to 1, flash, open the Serial Monitor.
 *   2. Place a known weight (e.g. 1000 g); factor = raw_reading / 1000.
 *   3. Enter the empty-bin weight as "Tare grams" on the device page in the app.
 *
 * The device sends gross grams; the server converts to units, compares with the
 * stock ledger and raises an alert if they disagree. It never edits stock itself.
 */
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <time.h>
#include "HX711.h"

// ------------------------------------------------------------------ config
#define USE_MQTT 0

const char* WIFI_SSID     = "YOUR_WIFI";
const char* WIFI_PASSWORD = "YOUR_PASSWORD";

const char* DEVICE_ID  = "sugar-scale-01";      // must match the device registered in the app
const char* DEVICE_KEY = "sisd_PASTE_KEY_HERE"; // shown once when the device is registered
const char* FIRMWARE   = "1.0.0";

const char* API_URL = "https://your-server.example.com/api/v1/iot/readings/";
// Root CA of your server (PEM). Leave empty ONLY for local testing over plain HTTP.
const char* ROOT_CA = "";

#if USE_MQTT
#include <PubSubClient.h>
const char* MQTT_HOST = "broker.example.com";
const int   MQTT_PORT = 8883;
const char* MQTT_USER = "";
const char* MQTT_PASS = "";
#endif

const uint32_t REPORT_INTERVAL_MS = 5UL * 60UL * 1000UL;  // every 5 minutes
const int      SAMPLES            = 15;                    // averaged per reading
const float    CALIBRATION_FACTOR = 21.53f;                // raw units per gram (calibrate!)

const int HX711_DT  = 16;
const int HX711_SCK = 4;

// ------------------------------------------------------------------ state
HX711 scale;
uint32_t bootId;
uint32_t seq = 0;
uint32_t lastReport = 0;

struct Pending { char id[32]; char measuredAt[32]; float grams; bool used; };
const int QUEUE_SIZE = 24;  // ~2 hours of readings at 5-minute intervals
Pending queue[QUEUE_SIZE];

#if USE_MQTT
WiFiClientSecure mqttNet;
PubSubClient mqtt(mqttNet);
#endif

// ------------------------------------------------------------------ helpers
void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) delay(250);
  if (WiFi.status() == WL_CONNECTED) {
    configTime(0, 0, "pool.ntp.org", "time.google.com");  // UTC; server validates timestamps
  }
}

bool isoNow(char* out, size_t len) {
  struct tm t;
  if (!getLocalTime(&t, 2000)) return false;
  strftime(out, len, "%Y-%m-%dT%H:%M:%SZ", &t);
  return true;
}

float readGrams() {
  if (!scale.wait_ready_timeout(2000)) return NAN;
  return scale.get_units(SAMPLES);
}

void enqueue(float grams) {
  int slot = -1;
  for (int i = 0; i < QUEUE_SIZE; i++) if (!queue[i].used) { slot = i; break; }
  if (slot < 0) {  // queue full: drop the oldest reading
    for (int i = 1; i < QUEUE_SIZE; i++) queue[i - 1] = queue[i];
    slot = QUEUE_SIZE - 1;
  }
  snprintf(queue[slot].id, sizeof(queue[slot].id), "%08lx-%lu", (unsigned long)bootId, (unsigned long)seq++);
  if (!isoNow(queue[slot].measuredAt, sizeof(queue[slot].measuredAt))) queue[slot].measuredAt[0] = '\0';
  queue[slot].grams = grams;
  queue[slot].used = true;
}

String buildPayload(const Pending& p, bool includeKey) {
  JsonDocument doc;
  doc["message_id"] = p.id;
  if (p.measuredAt[0]) doc["measured_at"] = p.measuredAt;
  doc["weight_g"] = roundf(p.grams * 10) / 10.0;
  doc["firmware"] = FIRMWARE;
  doc["rssi"] = WiFi.RSSI();
  if (includeKey) doc["key"] = DEVICE_KEY;
  String body;
  serializeJson(doc, body);
  return body;
}

#if !USE_MQTT
bool sendHttps(const Pending& p) {
  HTTPClient http;
  WiFiClientSecure secure;
  bool ok;
  if (strlen(ROOT_CA)) { secure.setCACert(ROOT_CA); ok = http.begin(secure, API_URL); }
  else { ok = http.begin(API_URL); }  // testing only
  if (!ok) return false;
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Id", DEVICE_ID);
  http.addHeader("Authorization", String("Device ") + DEVICE_KEY);
  http.setTimeout(8000);
  int code = http.POST(buildPayload(p, false));
  http.end();
  // 201 created, 200 duplicate (already stored) -> both mean "delivered".
  // 400 = rejected permanently (bad data) -> drop it rather than retry forever.
  return code == 200 || code == 201 || code == 400;
}
#else
bool sendMqtt(const Pending& p) {
  if (!mqtt.connected()) {
    mqttNet.setCACert(ROOT_CA);
    mqtt.setServer(MQTT_HOST, MQTT_PORT);
    if (!mqtt.connect(DEVICE_ID, MQTT_USER, MQTT_PASS)) return false;
  }
  String topic = String("sis/") + DEVICE_ID + "/readings";
  return mqtt.publish(topic.c_str(), buildPayload(p, true).c_str());
}
#endif

void flushQueue() {
  connectWiFi();
  if (WiFi.status() != WL_CONNECTED) return;
  for (int i = 0; i < QUEUE_SIZE; i++) {
    if (!queue[i].used) continue;
#if USE_MQTT
    bool delivered = sendMqtt(queue[i]);
#else
    bool delivered = sendHttps(queue[i]);
#endif
    if (delivered) queue[i].used = false;
    else break;  // keep order; retry next cycle
  }
}

// ------------------------------------------------------------------ main
void setup() {
  Serial.begin(115200);
  bootId = esp_random();
  scale.begin(HX711_DT, HX711_SCK);
  scale.set_scale(CALIBRATION_FACTOR);
  // No scale.tare(): the server applies the configured tare so a reboot with
  // stock on the scale does not zero the reading.
  connectWiFi();
  Serial.printf("SIS load-cell node %s ready (boot %08lx)\n", DEVICE_ID, (unsigned long)bootId);
}

void loop() {
#if USE_MQTT
  mqtt.loop();
#endif
  if (millis() - lastReport >= REPORT_INTERVAL_MS || lastReport == 0) {
    lastReport = millis();
    float g = readGrams();
    if (!isnan(g)) {
      Serial.printf("Weight: %.1f g\n", g);
      enqueue(g);
    } else {
      Serial.println("HX711 not ready — check wiring");
    }
    flushQueue();
  }
  delay(200);
}
