#!/usr/bin/env python3
"""
IoT Edge Gateway Service
Handles device management, data processing, and cloud connectivity
"""

import asyncio
import json
import sqlite3
import logging
import ssl
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import aiohttp
import paho.mqtt.client as mqtt
from dataclasses import dataclass, asdict
import signal
import sys
import configparser
import hashlib
import jwt

# Configuration
CONFIG_FILE = '/etc/iot-gateway/config.ini'
DATABASE_FILE = '/var/lib/iot-gateway/gateway.db'
LOG_FILE = '/var/log/iot-gateway/gateway.log'

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

@dataclass
class DeviceInfo:
    device_id: str
    device_type: str
    mac_address: str
    ip_address: str
    firmware_version: str
    capabilities: str
    last_seen: datetime
    status: str = "online"
    registration_time: Optional[datetime] = None

@dataclass
class TelemetryData:
    device_id: str
    timestamp: datetime
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    wifi_rssi: Optional[int] = None
    free_heap: Optional[int] = None
    uptime: Optional[int] = None
    custom_data: Optional[Dict] = None

class EdgeGateway:
    def __init__(self, config_file: str = CONFIG_FILE):
        self.config = configparser.ConfigParser()
        self.config.read(config_file)
        
        self.devices: Dict[str, DeviceInfo] = {}
        self.mqtt_client = None
        self.cloud_client = None
        self.running = False
        
        # Initialize database
        self.init_database()
        
        # Load existing devices
        self.load_devices()

    def init_database(self):
        """Initialize SQLite database for local data storage"""
        try:
            conn = sqlite3.connect(DATABASE_FILE)
            cursor = conn.cursor()
            
            # Devices table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    device_type TEXT,
                    mac_address TEXT,
                    ip_address TEXT,
                    firmware_version TEXT,
                    capabilities TEXT,
                    status TEXT,
                    last_seen TIMESTAMP,
                    registration_time TIMESTAMP
                )
            ''')
            
            # Telemetry data table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS telemetry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT,
                    timestamp TIMESTAMP,
                    temperature REAL,
                    humidity REAL,
                    wifi_rssi INTEGER,
                    free_heap INTEGER,
                    uptime INTEGER,
                    custom_data TEXT,
                    FOREIGN KEY (device_id) REFERENCES devices (device_id)
                )
            ''')
            
            # Commands table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT,
                    command TEXT,
                    parameters TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    executed_at TIMESTAMP,
                    FOREIGN KEY (device_id) REFERENCES devices (device_id)
                )
            ''')
            
            # Create indexes
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_telemetry_device_time ON telemetry(device_id, timestamp)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_commands_device_status ON commands(device_id, status)')
            
            conn.commit()
            conn.close()
            logger.info("Database initialized successfully")
            
        except Exception as e:
            logger.error(f"Database initialization failed: {e}")
            raise

    def load_devices(self):
        """Load existing devices from database"""
        try:
            conn = sqlite3.connect(DATABASE_FILE)
            cursor = conn.cursor()
            
            cursor.execute('SELECT * FROM devices')
            rows = cursor.fetchall()
            
            for row in rows:
                device = DeviceInfo(
                    device_id=row[0],
                    device_type=row[1],
                    mac_address=row[2],
                    ip_address=row[3],
                    firmware_version=row[4],
                    capabilities=row[5],
                    status=row[6],
                    last_seen=datetime.fromisoformat(row[7]) if row[7] else None,
                    registration_time=datetime.fromisoformat(row[8]) if row[8] else None
                )
                self.devices[device.device_id] = device
            
            conn.close()
            logger.info(f"Loaded {len(self.devices)} devices from database")
            
        except Exception as e:
            logger.error(f"Failed to load devices: {e}")

    def setup_mqtt(self):
        """Setup MQTT client for local broker communication"""
        self.mqtt_client = mqtt.Client()
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message
        self.mqtt_client.on_disconnect = self.on_mqtt_disconnect
        
        # Setup TLS if configured
        if self.config.getboolean('mqtt', 'use_tls', fallback=False):
            context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            self.mqtt_client.tls_set_context(context)
        
        # Set credentials
        username = self.config.get('mqtt', 'username', fallback=None)
        password = self.config.get('mqtt', 'password', fallback=None)
        if username and password:
            self.mqtt_client.username_pw_set(username, password)

    def on_mqtt_connect(self, client, userdata, flags, rc):
        """Callback for MQTT connection"""
        if rc == 0:
            logger.info("Connected to MQTT broker")
            
            # Subscribe to device topics
            client.subscribe("devices/+/telemetry")
            client.subscribe("devices/+/status")
            client.subscribe("devices/registration")
            
        else:
            logger.error(f"Failed to connect to MQTT broker: {rc}")

    def on_mqtt_disconnect(self, client, userdata, rc):
        """Callback for MQTT disconnection"""
        logger.warning(f"Disconnected from MQTT broker: {rc}")

    def on_mqtt_message(self, client, userdata, msg):
        """Handle incoming MQTT messages"""
        try:
            topic = msg.topic
            payload = json.loads(msg.payload.decode())
            
            logger.debug(f"Received message on {topic}: {payload}")
            
            if topic.startswith("devices/") and topic.endswith("/telemetry"):
                self.handle_telemetry(payload)
            elif topic.startswith("devices/") and topic.endswith("/status"):
                self.handle_status_update(payload)
            elif topic == "devices/registration":
                self.handle_device_registration(payload)
                
        except Exception as e:
            logger.error(f"Error processing MQTT message: {e}")

    def handle_device_registration(self, payload: Dict):
        """Handle new device registration"""
        try:
            device_id = payload['device_id']
            
            device = DeviceInfo(
                device_id=device_id,
                device_type=payload.get('device_type', 'unknown'),
                mac_address=payload.get('mac_address', ''),
                ip_address=payload.get('ip_address', ''),
                firmware_version=payload.get('firmware_version', ''),
                capabilities=payload.get('capabilities', ''),
                last_seen=datetime.now(),
                status='online',
                registration_time=datetime.now()
            )
            
            # Save to database
            self.save_device(device)
            self.devices[device_id] = device
            
            logger.info(f"Registered new device: {device_id}")
            
            # Send welcome command
            welcome_msg = {
                "command": "registration_confirmed",
                "gateway_id": self.config.get('gateway', 'id', fallback='gateway_001'),
                "timestamp": datetime.now().isoformat()
            }
            
            self.mqtt_client.publish(f"devices/{device_id}/commands", json.dumps(welcome_msg))
            
        except Exception as e:
            logger.error(f"Error handling device registration: {e}")

    def handle_telemetry(self, payload: Dict):
        """Process telemetry data"""
        try:
            device_id = payload.get('device_id')
            if not device_id:
                logger.warning("Telemetry message missing device_id")
                return
            
            # Update device last seen
            if device_id in self.devices:
                self.devices[device_id].last_seen = datetime.now()
                self.devices[device_id].status = 'online'
            
            # Create telemetry record
            telemetry = TelemetryData(
                device_id=device_id,
                timestamp=datetime.now(),
                temperature=payload.get('temperature'),
                humidity=payload.get('humidity'),
                wifi_rssi=payload.get('wifi_rssi'),
                free_heap=payload.get('free_heap'),
                uptime=payload.get('uptime'),
                custom_data=payload.get('custom_data')
            )
            
            # Save to database
            self.save_telemetry(telemetry)
            
            # Forward to cloud if connected
            if self.cloud_client:
                self.forward_to_cloud('telemetry', payload)
            
            logger.debug(f"Processed telemetry from {device_id}")
            
        except Exception as e:
            logger.error(f"Error handling telemetry: {e}")

    def handle_status_update(self, payload: Dict):
        """Handle device status updates"""
        try:
            device_id = payload.get('device_id')
            status = payload.get('status', 'unknown')
            
            if device_id in self.devices:
                self.devices[device_id].status = status
                self.devices[device_id].last_seen = datetime.now()
                
                # Update database
                self.update_device_status(device_id, status)
                
                logger.info(f"Device {device_id} status: {status}")
            
        except Exception as e:
            logger.error(f"Error handling status update: {e}")

    def save_device(self, device: DeviceInfo):
        """Save device to database"""
        try:
            conn = sqlite3.connect(DATABASE_FILE)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT OR REPLACE INTO devices 
                (device_id, device_type, mac_address, ip_address, firmware_version, 
                 capabilities, status, last_seen, registration_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                device.device_id, device.device_type, device.mac_address,
                device.ip_address, device.firmware_version, device.capabilities,
                device.status, device.last_seen.isoformat() if device.last_seen else None,
                device.registration_time.isoformat() if device.registration_time else None
            ))
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            logger.error(f"Error saving device: {e}")

    def save_telemetry(self, telemetry: TelemetryData):
        """Save telemetry data to database"""
        try:
            conn = sqlite3.connect(DATABASE_FILE)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO telemetry 
                (device_id, timestamp, temperature, humidity, wifi_rssi, free_heap, uptime, custom_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                telemetry.device_id, telemetry.timestamp.isoformat(),
                telemetry.temperature, telemetry.humidity, telemetry.wifi_rssi,
                telemetry.free_heap, telemetry.uptime,
                json.dumps(telemetry.custom_data) if telemetry.custom_data else None
            ))
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            logger.error(f"Error saving telemetry: {e}")

    def update_device_status(self, device_id: str, status: str):
        """Update device status in database"""
        try:
            conn = sqlite3.connect(DATABASE_FILE)
            cursor = conn.cursor()
            
            cursor.execute('''
                UPDATE devices SET status = ?, last_seen = ? WHERE device_id = ?
            ''', (status, datetime.now().isoformat(), device_id))
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            logger.error(f"Error updating device status: {e}")

    def forward_to_cloud(self, message_type: str, payload: Dict):
        """Forward messages to cloud platform"""
        try:
            if not self.cloud_client:
                return
            
            cloud_payload = {
                'gateway_id': self.config.get('gateway', 'id', fallback='gateway_001'),
                'message_type': message_type,
                'timestamp': datetime.now().isoformat(),
                'data': payload
            }
            
            # Send to cloud via HTTP API
            asyncio.create_task(self.send_to_cloud_api(cloud_payload))
            
        except Exception as e:
            logger.error(f"Error forwarding to cloud: {e}")

    async def send_to_cloud_api(self, payload: Dict):
        """Send data to cloud via HTTP API"""
        try:
            cloud_url = self.config.get('cloud', 'api_url', fallback='')
            if not cloud_url:
                return
            
            headers = {
                'Content-Type': 'application/json',
                'Authorization': f"Bearer {self.config.get('cloud', 'api_token', fallback='')}"
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(cloud_url + '/api/v1/gateway/data', 
                                      json=payload, headers=headers) as response:
                    if response.status == 200:
                        logger.debug("Data sent to cloud successfully")
                    else:
                        logger.warning(f"Cloud API error: {response.status}")
                        
        except Exception as e:
            logger.error(f"Error sending to cloud API: {e}")

    def check_device_health(self):
        """Check device health and mark offline devices"""
        try:
            offline_threshold = timedelta(minutes=5)
            current_time = datetime.now()
            
            for device_id, device in self.devices.items():
                if device.last_seen and (current_time - device.last_seen) > offline_threshold:
                    if device.status != 'offline':
                        device.status = 'offline'
                        self.update_device_status(device_id, 'offline')
                        logger.warning(f"Device {device_id} marked as offline")
            
        except Exception as e:
            logger.error(f"Error checking device health: {e}")

    async def cleanup_old_data(self):
        """Clean up old telemetry data to save space"""
        try:
            retention_days = self.config.getint('database', 'retention_days', fallback=30)
            cutoff_date = datetime.now() - timedelta(days=retention_days)
            
            conn = sqlite3.connect(DATABASE_FILE)
            cursor = conn.cursor()
            
            cursor.execute('DELETE FROM telemetry WHERE timestamp < ?', (cutoff_date.isoformat(),))
            deleted_rows = cursor.rowcount
            
            conn.commit()
            conn.close()
            
            if deleted_rows > 0:
                logger.info(f"Cleaned up {deleted_rows} old telemetry records")
                
        except Exception as e:
            logger.error(f"Error cleaning up old data: {e}")

    async def start(self):
        """Start the gateway service"""
        self.running = True
        logger.info("Starting IoT Edge Gateway...")
        
        # Setup MQTT
        self.setup_mqtt()
        
        # Connect to MQTT broker
        mqtt_host = self.config.get('mqtt', 'host', fallback='localhost')
        mqtt_port = self.config.getint('mqtt', 'port', fallback=1883)
        
        try:
            self.mqtt_client.connect(mqtt_host, mqtt_port, 60)
            self.mqtt_client.loop_start()
        except Exception as e:
            logger.error(f"Failed to connect to MQTT broker: {e}")
            return
        
        # Start background tasks
        health_check_task = asyncio.create_task(self.health_check_loop())
        cleanup_task = asyncio.create_task(self.cleanup_loop())
        
        logger.info("IoT Edge Gateway started successfully")
        
        try:
            # Keep the service running
            while self.running:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutdown signal received")
        finally:
            await self.stop()

    async def health_check_loop(self):
        """Background task for device health checking"""
        while self.running:
            self.check_device_health()
            await asyncio.sleep(60)  # Check every minute

    async def cleanup_loop(self):
        """Background task for data cleanup"""
        while self.running:
            await self.cleanup_old_data()
            await asyncio.sleep(3600)  # Clean up every hour

    async def stop(self):
        """Stop the gateway service"""
        self.running = False
        
        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
        
        logger.info("IoT Edge Gateway stopped")

def signal_handler(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}")
    sys.exit(0)

def main():
    """Main entry point"""
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    gateway = EdgeGateway()
    
    try:
        asyncio.run(gateway.start())
    except Exception as e:
        logger.error(f"Gateway failed to start: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()