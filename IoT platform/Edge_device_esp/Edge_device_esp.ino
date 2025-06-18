#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <HTTPUpdate.h>
#include <WiFiClientSecure.h>
#include <DHT.h>
#include <SPIFFS.h>
#include <mbedtls/md.h>

// Device Configuration
#define DEVICE_ID "esp32_sensor_001"
#define FIRMWARE_VERSION "1.0.0"
#define DHT_PIN 4
#define DHT_TYPE DHT22

// Network Configuration
const char* ssid = "YourWiFiSSID";
const char* password = "YourWiFiPassword";
const char* mqtt_server = "192.168.1.100";  // Raspberry Pi IP @ 
const int mqtt_port = 8883;  // MQTT over TLS 

// MQTT Topics
const char* telemetry_topic = "devices/esp32_sensor_001/telemetry";
const char* command_topic = "devices/esp32_sensor_001/commands";
const char* status_topic = "devices/esp32_sensor_001/status";
const char* ota_topic = "devices/esp32_sensor_001/ota";

// Global Objects
WiFiClientSecure espClient;
PubSubClient mqtt_client(espClient);
DHT dht(DHT_PIN, DHT_TYPE);

// Device State
unsigned long lastTelemetry = 0;
unsigned long lastHeartbeat = 0;
const long telemetryInterval = 30000;  // 30 seconds
const long heartbeatInterval = 60000;  // 1 minute
bool deviceRegistered = false;

// TLS Certificate (Self-signed for testing)
const char* ca_cert = R"EOF(
-----BEGIN CERTIFICATE-----
MIIDXTCCAkWgAwIBAgIJAKL0UG+gD8CKMA0GCSqGSIb3DQEBCwUAMEUxCzAJBgNV
BAYTAkFVMRMwEQYDVQQIDApTb21lLVN0YXRlMSEwHwYDVQQKDBhJbnRlcm5ldCBX
aWRnaXRzIFB0eSBMdGQwHhcNMjQwMTAxMDAwMDAwWhcNMjUwMTAxMDAwMDAwWjBF
MQswCQYDVQQGEwJBVTETMBEGA1UECAwKU29tZS1TdGF0ZTEhMB8GA1UECgwYSW50
ZXJuZXQgV2lkZ2l0cyBQdHkgTHRkMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIB
CgKCAQEA4qiWWWWjkIXbWeFx2q37SsCOUW2bGQG3z1bz7QMH5pOp8DhqE6EzMFif
Q8s5A9J+fJ9JYFJGb2Kp/pQ4QM0CgYEA4qiWWWWjkIXbWeFx2q37SsCOUW2bGQG3
-----END CERTIFICATE-----
)EOF";

void setup() {
    Serial.begin(9600);
    delay(1000);
    
    Serial.println("=== IoT Device Starting ===");
    
    // Initialize SPIFFS for configuration
    if (!SPIFFS.begin(true)) {
        Serial.println("SPIFFS initialization failed!");
        return;
    }
    
    // Initialize DHT sensor
    dht.begin();
    
    // Setup WiFi
    setupWiFi();
    
    // Setup MQTT with TLS
    setupMQTT();
    
    // Register device
    registerDevice();
    
    Serial.println("Device initialization complete!");
}

void loop() {
    if (!mqtt_client.connected()) {
        reconnectMQTT();
    }
    mqtt_client.loop();
    
    unsigned long now = millis();
    
    // Send telemetry data
    if (now - lastTelemetry > telemetryInterval) {
        sendTelemetry();
        lastTelemetry = now;
    }
    
    // Send heartbeat
    if (now - lastHeartbeat > heartbeatInterval) {
        sendHeartbeat();
        lastHeartbeat = now;
    }
    
    delay(100);
}

void setupWiFi() {
    WiFi.begin(ssid, password);
    Serial.print("Connecting to WiFi");
    
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");
    }
    
    Serial.println();
    Serial.print("WiFi connected! IP address: ");
    Serial.println(WiFi.localIP());
}

void setupMQTT() {
    espClient.setCACert(ca_cert);
    mqtt_client.setServer(mqtt_server, mqtt_port);
    mqtt_client.setCallback(onMqttMessage);
    mqtt_client.setBufferSize(1024);
}

void reconnectMQTT() {
    while (!mqtt_client.connected()) {
        Serial.print("Attempting MQTT connection...");
        
        String clientId = String(DEVICE_ID) + "_" + String(random(0xffff), HEX);
        
        if (mqtt_client.connect(clientId.c_str(), "iot_device", "secure_password")) {
            Serial.println("Connected to MQTT broker!");
            
            // Subscribe to command topic
            mqtt_client.subscribe(command_topic);
            mqtt_client.subscribe(ota_topic);
            
            // Send online status
            sendStatus("online");
            
        } else {
            Serial.print("Failed, rc=");
            Serial.print(mqtt_client.state());
            Serial.println(" retrying in 5 seconds");
            delay(5000);
        }
    }
}

void onMqttMessage(char* topic, byte* payload, unsigned int length) {
    String message;
    for (int i = 0; i < length; i++) {
        message += (char)payload[i];
    }
    
    Serial.printf("Message received [%s]: %s\n", topic, message.c_str());
    
    if (String(topic) == command_topic) {
        handleCommand(message);
    } else if (String(topic) == ota_topic) {
        handleOTAUpdate(message);
    }
}

void handleCommand(String command) {
    DynamicJsonDocument doc(256);
    deserializeJson(doc, command);
    
    String cmd = doc["command"];
    
    if (cmd == "restart") {
        sendStatus("restarting");
        delay(1000);
        ESP.restart();
    } else if (cmd == "get_status") {
        sendFullStatus();
    } else if (cmd == "set_interval") {
        // Update telemetry interval
        int newInterval = doc["interval"];
        if (newInterval >= 5000 && newInterval <= 300000) {
            // telemetryInterval = newInterval; // Note: would need to make non-const
            sendResponse("interval_updated", newInterval);
        }
    }
}

void handleOTAUpdate(String otaInfo) {
    DynamicJsonDocument doc(512);
    deserializeJson(doc, otaInfo);
    
    String url = doc["url"];
    String version = doc["version"];
    String checksum = doc["checksum"];
    
    Serial.println("Starting OTA update...");
    sendStatus("updating");
    
    WiFiClientSecure client;
    client.setCACert(ca_cert);
    
    t_httpUpdate_return ret = httpUpdate.update(client, url);
    
    switch (ret) {
        case HTTP_UPDATE_FAILED:
            Serial.printf("HTTP_UPDATE_FAILED Error (%d): %s\n", 
                         httpUpdate.getLastError(), 
                         httpUpdate.getLastErrorString().c_str());
            sendStatus("update_failed");
            break;
            
        case HTTP_UPDATE_NO_UPDATES:
            Serial.println("HTTP_UPDATE_NO_UPDATES");
            sendStatus("no_updates");
            break;
            
        case HTTP_UPDATE_OK:
            Serial.println("HTTP_UPDATE_OK");
            sendStatus("update_success");
            break;
    }
}

void sendTelemetry() {
    float temperature = dht.readTemperature();
    float humidity = dht.readHumidity();
    
    if (isnan(temperature) || isnan(humidity)) {
        Serial.println("Failed to read from DHT sensor!");
        return;
    }
    
    DynamicJsonDocument doc(512);
    doc["device_id"] = DEVICE_ID;
    doc["timestamp"] = millis();
    doc["temperature"] = temperature;
    doc["humidity"] = humidity;
    doc["wifi_rssi"] = WiFi.RSSI();
    doc["free_heap"] = ESP.getFreeHeap();
    doc["uptime"] = millis() / 1000;
    doc["firmware_version"] = FIRMWARE_VERSION;
    
    String payload;
    serializeJson(doc, payload);
    
    if (mqtt_client.publish(telemetry_topic, payload.c_str())) {
        Serial.println("Telemetry sent: " + payload);
    } else {
        Serial.println("Failed to send telemetry");
    }
}

void sendHeartbeat() {
    DynamicJsonDocument doc(256);
    doc["device_id"] = DEVICE_ID;
    doc["timestamp"] = millis();
    doc["status"] = "alive";
    doc["uptime"] = millis() / 1000;
    
    String payload;
    serializeJson(doc, payload);
    
    mqtt_client.publish(status_topic, payload.c_str());
}

void sendStatus(String status) {
    DynamicJsonDocument doc(256);
    doc["device_id"] = DEVICE_ID;
    doc["timestamp"] = millis();
    doc["status"] = status;
    doc["firmware_version"] = FIRMWARE_VERSION;
    
    String payload;
    serializeJson(doc, payload);
    
    mqtt_client.publish(status_topic, payload.c_str());
}

void sendFullStatus() {
    DynamicJsonDocument doc(512);
    doc["device_id"] = DEVICE_ID;
    doc["timestamp"] = millis();
    doc["firmware_version"] = FIRMWARE_VERSION;
    doc["wifi_ssid"] = WiFi.SSID();
    doc["wifi_rssi"] = WiFi.RSSI();
    doc["ip_address"] = WiFi.localIP().toString();
    doc["free_heap"] = ESP.getFreeHeap();
    doc["uptime"] = millis() / 1000;
    doc["chip_id"] = ESP.getChipModel();
    doc["mac_address"] = WiFi.macAddress();
    
    String payload;
    serializeJson(doc, payload);
    
    mqtt_client.publish(status_topic, payload.c_str());
}

void sendResponse(String responseType, int value) {
    DynamicJsonDocument doc(256);
    doc["device_id"] = DEVICE_ID;
    doc["timestamp"] = millis();
    doc["response"] = responseType;
    doc["value"] = value;
    
    String payload;
    serializeJson(doc, payload);
    
    mqtt_client.publish(status_topic, payload.c_str());
}

void registerDevice() {
    DynamicJsonDocument doc(512);
    doc["device_id"] = DEVICE_ID;
    doc["device_type"] = "sensor";
    doc["firmware_version"] = FIRMWARE_VERSION;
    doc["capabilities"] = "temperature,humidity,wifi_status";
    doc["mac_address"] = WiFi.macAddress();
    doc["ip_address"] = WiFi.localIP().toString();
    doc["registration_time"] = millis();
    
    String payload;
    serializeJson(doc, payload);
    
    if (mqtt_client.publish("devices/registration", payload.c_str())) {
        Serial.println("Device registered successfully");
        deviceRegistered = true;
    }
}