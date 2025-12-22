#!/bin/bash

# Wait for system DBus and Bluetooth to be ready
sleep 5

# Ensure hci0 is up and powered
hciconfig hci0 up 2>/dev/null || echo "Warning: Could not bring up hci0"

# Check adapter status
echo "Checking Bluetooth adapter status:"
hciconfig -a

# Start the Python scanner (advertisement-based with GATT fallback)
exec python -u ble_scanner.py
