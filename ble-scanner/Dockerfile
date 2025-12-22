FROM balenalib/raspberrypi4-64-python:3.11

# Install system dependencies for BLE
RUN install_packages \
    bluez \
    bluetooth \
    libbluetooth-dev \
    libdbus-1-dev \
    libglib2.0-dev \
    dbus \
    bluez-tools

# Set working directory
WORKDIR /usr/src/app

# Copy requirements and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application (advertisement-based scanner with GATT fallback)
COPY ble_scanner.py .
COPY start.sh .
RUN chmod +x start.sh

# Run the scanner with dbus
CMD ["/bin/bash", "start.sh"]
