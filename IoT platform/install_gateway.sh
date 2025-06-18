
#!/bin/bash

# IoT Gateway Installation Script

set -e

echo "=== IoT Edge Gateway Installation ==="

# Update system
sudo apt update && sudo apt upgrade -y

# Install dependencies
sudo apt install -y python3 python3-pip mosquitto mosquitto-clients sqlite3 nginx

# Install Python packages
pip3 install -r requirements.txt

# Create directories
sudo mkdir -p /etc/iot-gateway
sudo mkdir -p /var/lib/iot-gateway
sudo mkdir -p /var/log/iot-gateway

# Copy configuration files
sudo cp config.ini /etc/iot-gateway/
sudo cp mosquitto.conf /etc/mosquitto/conf.d/iot-gateway.conf

# Create mosquitto password file
sudo mosquitto_passwd -c /etc/mosquitto/passwd iot_device
sudo mosquitto_passwd /etc/mosquitto/passwd gateway_user

# Generate TLS certificates
cd /etc/mosquitto/certs
sudo openssl req -new -x509 -days 365 -extensions v3_ca -keyout ca.key -out ca.crt -subj "/C=US/ST=State/L=City/O=IoT/CN=IoT-CA"
sudo openssl genrsa -out server.key 2048
sudo openssl req -new -key server.key -out server.csr -subj "/C=US/ST=State/L=City/O=IoT/CN=iot-gateway"
sudo openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server.crt -days 365

# Set permissions
sudo chown mosquitto:mosquitto /etc/mosquitto/certs/*
sudo chmod 400 /etc/mosquitto/certs/*.key
sudo chmod 444 /etc/mosquitto/certs/*.crt

# Create systemd service
sudo tee /etc/systemd/system/iot-gateway.service > /dev/null <<EOF
[Unit]
Description=IoT Edge Gateway Service
After=network.target mosquitto.service

[Service]
Type=simple
User=iot-gateway
Group=iot-gateway
WorkingDirectory=/opt/iot-gateway
ExecStart=/usr/bin/python3 /opt/iot-gateway/gateway.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Create user
sudo useradd -r -s /bin/false iot-gateway

# Set ownership
sudo chown -R iot-gateway:iot-gateway /var/lib/iot-gateway
sudo chown -R iot-gateway:iot-gateway /var/log/iot-gateway

# Enable and start services
sudo systemctl daemon-reload
sudo systemctl enable mosquitto
sudo systemctl enable iot-gateway
sudo systemctl start mosquitto
sudo systemctl start iot-gateway

echo "=== Installation Complete ==="
echo "Gateway status: $(sudo systemctl is-active iot-gateway)"
echo "Mosquitto status: $(sudo systemctl is-active mosquitto)"