#!/usr/bin/env python3
"""
BLE Scanner for Sensirion Smart Gadgets - Advertisement Data Parsing

Scans for Sensirion BLE devices and parses sensor data from advertisements.
Uses device configuration from environment variables to filter and name sensors.

Supports:
- SHT3x gadgets (type 4)
- SHT4x gadgets (type 6)
- MyCO2 gadgets (type 8)

For complete documentation, see README.md
"""

import asyncio
import os
import sys
import random
from typing import Optional, Dict, List
from dataclasses import dataclass, field
import struct
import logging
from datetime import datetime
import json
import math
import time

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
import paho.mqtt.client as mqtt

# ============================================================================
# CONFIGURATION
# ============================================================================

# Sensirion Company ID (for device discovery)
SENSIRION_COMPANY_ID = 0x06D5

# Timing
DEFAULT_SCAN_DURATION = 10.0
DEFAULT_CO2_SCAN_INTERVAL = 60  # CO2: 1 minute
DEFAULT_AIQ_SCAN_INTERVAL = 60  # AQI: 1 minute
DEFAULT_HUMI_SCAN_INTERVAL = 300  # HUMI: 5 minutes

# MQTT
DEFAULT_MQTT_BROKER = "mqtt"
DEFAULT_MQTT_PORT = 1883
DEFAULT_MQTT_TOPIC_PREFIX = "energiemonitor/ble"

# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class DeviceConfig:
    """Configuration for a specific device."""
    address: str
    name: str
    temp_offset: float
    device_type: str  # 'humi', 'co2', 'aiq'


@dataclass
class SensorReading:
    """Container for sensor readings from a Sensirion device."""
    address: str
    name: str
    device_type: str
    rssi: Optional[int] = None
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    co2: Optional[int] = None
    voc: Optional[int] = None
    battery: Optional[int] = None
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        return {
            'address': self.address,
            'name': self.name,
            'rssi': self.rssi,
            'temperature': self.temperature,
            'humidity': self.humidity,
            'co2': self.co2,
            'voc': self.voc,
            'battery': self.battery,
            'timestamp': self.timestamp
        }

# ============================================================================
# HUMIDITY CORRECTION FUNCTIONS
# ============================================================================

def saturation_vapor_pressure(temp_celsius: float) -> float:
    """
    Calculate saturation vapor pressure using Magnus formula.
    
    Args:
        temp_celsius: Temperature in degrees Celsius
        
    Returns:
        Saturation vapor pressure in hPa (hectopascals)
    """
    return 6.112 * math.exp((17.67 * temp_celsius) / (temp_celsius + 243.5))


def absolute_humidity(temp_celsius: float, relative_humidity_percent: float) -> float:
    """
    Calculate absolute humidity from temperature and relative humidity.
    
    Args:
        temp_celsius: Temperature in degrees Celsius
        relative_humidity_percent: Relative humidity in percent (0-100)
        
    Returns:
        Absolute humidity in g/m³
    """
    # Saturation vapor pressure at given temperature
    e_s = saturation_vapor_pressure(temp_celsius)
    
    # Actual vapor pressure
    e = (relative_humidity_percent / 100.0) * e_s
    
    # Absolute humidity in g/m³
    # Formula: AH = (2165.7 * e) / (T_kelvin)
    temp_kelvin = temp_celsius + 273.15
    ah = (2165.7 * e) / temp_kelvin
    
    return ah


def relative_humidity_from_absolute(temp_celsius: float, absolute_humidity_g_m3: float) -> float:
    """
    Calculate relative humidity from absolute humidity at a given temperature.
    
    Args:
        temp_celsius: Temperature in degrees Celsius
        absolute_humidity_g_m3: Absolute humidity in g/m³
        
    Returns:
        Relative humidity in percent (0-100)
    """
    # Convert absolute humidity back to vapor pressure
    temp_kelvin = temp_celsius + 273.15
    e = (absolute_humidity_g_m3 * temp_kelvin) / 2165.7
    
    # Saturation vapor pressure at given temperature
    e_s = saturation_vapor_pressure(temp_celsius)
    
    # Relative humidity
    rh = (e / e_s) * 100.0
    
    return rh


def correct_humidity(temp_raw: float, rh_raw: float, temp_correction: float) -> tuple:
    """
    Correct relative humidity based on temperature correction.
    
    Assumes absolute humidity stays constant when correcting for sensor box heat.
    This corrects for the temperature difference between inside the sensor box
    (where raw values are measured) and outside (actual ambient conditions).
    
    Args:
        temp_raw: Raw temperature reading (inside box) in °C
        rh_raw: Raw relative humidity reading (inside box) in %
        temp_correction: Temperature correction to apply in K (typically negative)
        
    Returns:
        Tuple of (corrected_temperature, corrected_humidity)
    """
    # Calculate absolute humidity from raw values (this stays constant)
    ah = absolute_humidity(temp_raw, rh_raw)
    
    # Apply temperature correction
    temp_corrected = temp_raw + temp_correction
    
    # Calculate relative humidity at corrected temperature
    rh_corrected = relative_humidity_from_absolute(temp_corrected, ah)
    
    return temp_corrected, rh_corrected

# ============================================================================
# ADVERTISEMENT DATA PARSING
# ============================================================================

def parse_sensirion_advertisement(raw_data: bytes, logger=None, debug_level=0) -> Optional[Dict]:
    """
    Parse Sensirion advertisement data.
    
    Based on https://github.com/akx/sensirion-ble/blob/main/src/sensirion_ble/converters.py
    
    Advertisement format:
    - Bytes 0-1: Sample type (0x0004, 0x0006, 0x0008, etc.)
    - Bytes 2-3: Device ID
    - Bytes 4+: Sensor data (varies by type)
    
    Returns dict with parsed sensor values or None if invalid.
    """
    if len(raw_data) < 4:
        return None
    
    # Get sample type (first 2 bytes)
    sample_type = raw_data[0:2]
    device_id = raw_data[2:4].hex().upper()
    
    # Log raw data for debugging
    if logger and debug_level >= 1:
        logger.info(f"=== RAW ADVERTISEMENT DATA DEBUG ===")
        logger.info(f"  Full raw data ({len(raw_data)} bytes): {raw_data.hex()}")
        logger.info(f"  Sample type bytes: {sample_type.hex()} (decimal: {int.from_bytes(sample_type, 'big')})")
        logger.info(f"  Device ID: {device_id}")
        
        # Log all data bytes
        if len(raw_data) > 4:
            data_bytes = raw_data[4:]
            logger.info(f"  Data bytes ({len(data_bytes)}): {data_bytes.hex()}")
            
            # Try parsing as different integer formats
            if len(data_bytes) >= 2:
                logger.info(f"  First 2 bytes as uint16 LE: {struct.unpack('<H', data_bytes[0:2])[0]}")
                logger.info(f"  First 2 bytes as uint16 BE: {struct.unpack('>H', data_bytes[0:2])[0]}")
            if len(data_bytes) >= 4:
                logger.info(f"  First 4 bytes as uint16s LE: {struct.unpack('<HH', data_bytes[0:4])}")
                logger.info(f"  First 4 bytes as uint16s BE: {struct.unpack('>HH', data_bytes[0:4])}")
            if len(data_bytes) >= 6:
                logger.info(f"  First 6 bytes as uint16s LE: {struct.unpack('<HHH', data_bytes[0:6])}")
                logger.info(f"  First 6 bytes as uint16s BE: {struct.unpack('>HHH', data_bytes[0:6])}")
            if len(data_bytes) >= 8:
                logger.info(f"  First 8 bytes as uint16s LE: {struct.unpack('<HHHH', data_bytes[0:8])}")
                logger.info(f"  First 8 bytes as uint16s BE: {struct.unpack('>HHHH', data_bytes[0:8])}")
    
    result = {'device_id': device_id}
    
    # SHT3x gadgets (type 4)
    if sample_type == b'\x00\x04':
        if len(raw_data) < 8:
            return None
        temp_ticks, humidity_ticks = struct.unpack("<HH", raw_data[4:8])
        
        if logger and debug_level >= 1:
            logger.info(f"  >>> SHT3x Device Detected <<<")
            logger.info(f"  Temperature ticks: {temp_ticks}")
            logger.info(f"  Humidity ticks: {humidity_ticks}")
        
        # Conversion: T = -45 + (175.0 * ticks) / (2^16 - 1)
        #             RH = (100.0 * ticks) / (2^16 - 1)
        temp = -45 + ((175.0 * temp_ticks) / (2**16 - 1))
        humidity = (100.0 * humidity_ticks) / (2**16 - 1)
        result['temperature'] = round(temp, 1)
        result['humidity'] = round(humidity, 1)
        result['sensor_type'] = 'SHT3x'
        
        if logger and debug_level >= 1:
            logger.info(f"  Temperature: {temp:.2f}°C")
            logger.info(f"  Humidity: {humidity:.2f}%")
        
        return result
    
    # AQI/VOC gadgets (type 3) - Sensirion Air Quality Sensor
    elif sample_type == b'\x00\x03':
        if len(raw_data) < 12:
            return None
        temp_ticks, humidity_ticks, voc, pm25 = struct.unpack("<HHHH", raw_data[4:12])
        
        if logger and debug_level >= 1:
            logger.info(f"  >>> AQI/VOC Device Detected (Type 3) <<<")
            logger.info(f"  Temperature ticks: {temp_ticks}")
            logger.info(f"  Humidity ticks: {humidity_ticks}")
            logger.info(f"  VOC raw value: {voc}")
            logger.info(f"  PM2.5/Extra raw value: {pm25}")
        
        # Conversion: T = -45 + (175.0 * ticks) / (2^16 - 1)
        #             RH = (100.0 * ticks) / (2^16 - 1)
        #             VOC = transmitted value (index)
        temp = -45 + ((175.0 * temp_ticks) / (2**16 - 1))
        humidity = (100.0 * humidity_ticks) / (2**16 - 1)
        result['temperature'] = round(temp, 1)
        result['humidity'] = round(humidity, 1)
        result['voc'] = voc
        result['sensor_type'] = 'AQI'
        
        if logger and debug_level >= 1:
            logger.info(f"  Temperature: {temp:.2f}°C")
            logger.info(f"  Humidity: {humidity:.2f}%")
            logger.info(f"  VOC: {voc} index")
        
        return result
    
    # SHT4x gadgets (type 6)
    elif sample_type == b'\x00\x06':
        if len(raw_data) < 8:
            return None
        temp_ticks, humidity_ticks = struct.unpack("<HH", raw_data[4:8])
        
        if logger and debug_level >= 1:
            logger.info(f"  >>> SHT4x Device Detected <<<")
            logger.info(f"  Temperature ticks: {temp_ticks}")
            logger.info(f"  Humidity ticks: {humidity_ticks}")
        
        # Conversion: T = -45 + (175.0 * ticks) / (2^16 - 1)
        #             RH = -6 + (125.0 * ticks) / (2^16 - 1)
        temp = -45 + ((175.0 * temp_ticks) / (2**16 - 1))
        humidity = -6 + ((125.0 * humidity_ticks) / (2**16 - 1))
        result['temperature'] = round(temp, 2)
        result['humidity'] = round(humidity, 2)
        result['sensor_type'] = 'SHT4x'
        
        if logger and debug_level >= 1:
            logger.info(f"  Temperature: {temp:.2f}°C")
            logger.info(f"  Humidity: {humidity:.2f}%")
        
        return result
    
    # MyCO2 gadgets (type 8)
    elif sample_type == b'\x00\x08':
        if len(raw_data) < 10:
            return None
        temp_ticks, humidity_ticks, co2 = struct.unpack("<HHH", raw_data[4:10])
        
        if logger and debug_level >= 1:
            logger.info(f"  >>> MyCO2 Device Detected <<<")
            logger.info(f"  Temperature ticks: {temp_ticks}")
            logger.info(f"  Humidity ticks: {humidity_ticks}")
            logger.info(f"  CO2 raw value: {co2}")
        
        # Conversion: T = -45 + (175.0 * ticks) / (2^16 - 1)
        #             RH = (100.0 * ticks) / (2^16 - 1)
        #             CO2 = transmitted value
        temp = -45 + ((175.0 * temp_ticks) / (2**16 - 1))
        humidity = (100.0 * humidity_ticks) / (2**16 - 1)
        result['temperature'] = round(temp, 1)
        result['humidity'] = round(humidity, 1)
        result['co2'] = co2
        result['sensor_type'] = 'MyCO2'
        
        if logger and debug_level >= 1:
            logger.info(f"  Temperature: {temp:.2f}°C")
            logger.info(f"  Humidity: {humidity:.2f}%")
            logger.info(f"  CO2: {co2} ppm")
        
        # Check if there's VOC data (12+ bytes total)
        if len(raw_data) >= 12:
            voc = struct.unpack("<H", raw_data[10:12])[0]
            result['voc'] = voc
            if logger and debug_level >= 1:
                logger.info(f"  VOC raw value: {voc}")
                logger.info(f"  VOC: {voc} index")
        
        return result
    
    # Unknown sample type - log it for debugging
    if logger:
        logger.warning(f"  >>> UNKNOWN Sample Type: {sample_type.hex()} <<<")
        logger.warning(f"  This might be an AQI/VOC device or unsupported sensor")
        logger.warning(f"  Please check the data above to determine the conversion formula")
    
    return None

# ============================================================================
# BLE SCANNER CLASS
# ============================================================================

class SensirionBLEScanner:
    """Scanner for Sensirion Smart Gadgets using advertisement data."""
    
    def __init__(self):
        """Initialize the BLE scanner."""
        # Parse debug level safely
        debug_str = os.getenv('DEBUG_LEVEL', '0').strip()
        self.debug_level = int(debug_str) if debug_str else 0
        self.logger = self._setup_logging()
        
        # Parse device configurations from environment variables
        self.devices = self._parse_device_config()
        
        if not self.devices:
            # Kein Fehler: Geraete ohne Sensirion-Gadgets sind erlaubt. Sauber beenden (Exit-Code 0);
            # docker-compose startet den Container nur bei Fehlern neu (restart: on-failure).
            self.logger.info("No devices configured (HUMI_GADGETS, CO2_GADGETS, AIQ_GADGETS empty). Shutting down.")
            sys.exit(0)

        self.logger.info(f"Configured {len(self.devices)} device(s):")
        for device in self.devices:
            self.logger.info(f"  {device.device_type.upper()}: {device.name} ({device.address}) offset={device.temp_offset:+.1f}°C")
        
        # Configuration from environment variables (handle empty strings)
        scan_duration_str = os.getenv('SCAN_DURATION', str(DEFAULT_SCAN_DURATION)).strip()
        self.scan_duration = float(scan_duration_str) if scan_duration_str else DEFAULT_SCAN_DURATION
        
        # Device-specific scan intervals (handle empty strings)
        co2_interval_str = os.getenv('CO2_SCAN_INTERVAL', str(DEFAULT_CO2_SCAN_INTERVAL)).strip()
        self.co2_scan_interval = int(co2_interval_str) if co2_interval_str else DEFAULT_CO2_SCAN_INTERVAL
        
        aiq_interval_str = os.getenv('AIQ_SCAN_INTERVAL', str(DEFAULT_AIQ_SCAN_INTERVAL)).strip()
        self.aiq_scan_interval = int(aiq_interval_str) if aiq_interval_str else DEFAULT_AIQ_SCAN_INTERVAL
        
        humi_interval_str = os.getenv('HUMI_SCAN_INTERVAL', str(DEFAULT_HUMI_SCAN_INTERVAL)).strip()
        self.humi_scan_interval = int(humi_interval_str) if humi_interval_str else DEFAULT_HUMI_SCAN_INTERVAL
        
        # Track last scan times per device type
        self.last_scan_times = {'co2': 0, 'aiq': 0, 'humi': 0}
        
        # MQTT configuration
        self.mqtt_broker = os.getenv('MQTT_BROKER', DEFAULT_MQTT_BROKER)
        mqtt_port_str = os.getenv('MQTT_PORT', str(DEFAULT_MQTT_PORT)).strip()
        self.mqtt_port = int(mqtt_port_str) if mqtt_port_str else DEFAULT_MQTT_PORT
        self.mqtt_topic_prefix = os.getenv('MQTT_TOPIC_PREFIX', DEFAULT_MQTT_TOPIC_PREFIX)
        
        # MQTT client
        self.mqtt_client = None
        self.mqtt_connected = False
        
        self.logger.info("Sensirion BLE Scanner (Advertisement mode) initialized")
        self.logger.info(f"  Scan duration: {self.scan_duration}s")
        self.logger.info(f"  Scan intervals: CO2={self.co2_scan_interval}s, AQI={self.aiq_scan_interval}s, HUMI={self.humi_scan_interval}s")
        self.logger.info(f"  MQTT broker: {self.mqtt_broker}:{self.mqtt_port}")
    
    def _setup_logging(self) -> logging.Logger:
        """Configure logging."""
        # Check DEBUG_LEVEL environment variable (0=INFO, 1=DEBUG, 2=VERBOSE DEBUG)
        # For backwards compatibility, also check DEBUG_MODE
        if self.debug_level == 0:
            # If DEBUG_LEVEL not set, try DEBUG_MODE
            debug_mode_str = os.getenv('DEBUG_MODE', '0')
            try:
                self.debug_level = int(debug_mode_str)
            except ValueError:
                # Handle '1' or 'true' as level 1
                if debug_mode_str.lower() in ('1', 'true'):
                    self.debug_level = 1
        
        log_level = logging.DEBUG if self.debug_level >= 1 else logging.INFO
        
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        
        # Bleak debug messages only at level 2
        if self.debug_level >= 2:
            logging.getLogger('bleak').setLevel(logging.DEBUG)
        else:
            logging.getLogger('bleak').setLevel(logging.WARNING)
        
        return logging.getLogger('SensirionBLE')
    
    def _parse_device_config(self) -> List[DeviceConfig]:
        """Parse device configuration from environment variables."""
        devices = []
        
        # Parse HUMI devices
        humi_addrs = os.getenv('HUMI_GADGETS', '').split(',')
        humi_names = os.getenv('HUMI_NAMES', '').split(',')
        humi_offsets = os.getenv('HUMI_TEMP_OFFSET', '').split(',')
        
        for i, addr in enumerate(humi_addrs):
            addr = addr.strip()
            if addr:
                name = humi_names[i].strip() if i < len(humi_names) else f'humi_{i}'
                offset = float(humi_offsets[i].strip()) if i < len(humi_offsets) and humi_offsets[i].strip() else 0.0
                devices.append(DeviceConfig(
                    address=addr.lower(),
                    name=name,
                    temp_offset=offset,
                    device_type='humi'
                ))
        
        # Parse CO2 devices
        co2_addrs = os.getenv('CO2_GADGETS', '').split(',')
        co2_names = os.getenv('CO2_NAMES', '').split(',')
        co2_offsets = os.getenv('CO2_TEMP_OFFSET', '').split(',')
        
        for i, addr in enumerate(co2_addrs):
            addr = addr.strip()
            if addr:
                name = co2_names[i].strip() if i < len(co2_names) else f'co2_{i}'
                offset = float(co2_offsets[i].strip()) if i < len(co2_offsets) and co2_offsets[i].strip() else 0.0
                devices.append(DeviceConfig(
                    address=addr.lower(),
                    name=name,
                    temp_offset=offset,
                    device_type='co2'
                ))
        
        # Parse AIQ devices
        aiq_addrs = os.getenv('AIQ_GADGETS', '').split(',')
        aiq_names = os.getenv('AIQ_NAMES', '').split(',')
        aiq_offsets = os.getenv('AIQ_TEMP_OFFSET', '').split(',')
        
        for i, addr in enumerate(aiq_addrs):
            addr = addr.strip()
            if addr:
                name = aiq_names[i].strip() if i < len(aiq_names) else f'aiq_{i}'
                offset = float(aiq_offsets[i].strip()) if i < len(aiq_offsets) and aiq_offsets[i].strip() else 0.0
                devices.append(DeviceConfig(
                    address=addr.lower(),
                    name=name,
                    temp_offset=offset,
                    device_type='aiq'
                ))
        
        return devices
    
    def _setup_mqtt(self) -> None:
        """Initialize MQTT client."""
        self.mqtt_client = mqtt.Client(client_id='sensirion-ble-scanner')
        self.mqtt_client.on_connect = self._on_mqtt_connect
        self.mqtt_client.on_disconnect = self._on_mqtt_disconnect
        
        try:
            self.logger.info(f"Connecting to MQTT broker {self.mqtt_broker}:{self.mqtt_port}...")
            self.mqtt_client.connect(self.mqtt_broker, self.mqtt_port, 60)
            self.mqtt_client.loop_start()
        except Exception as e:
            self.logger.error(f"Failed to connect to MQTT broker: {e}")
    
    def _on_mqtt_connect(self, client, userdata, flags, rc):
        """MQTT connection callback."""
        if rc == 0:
            self.mqtt_connected = True
            self.logger.info("Connected to MQTT broker")
        else:
            self.logger.error(f"MQTT connection failed with code {rc}")
    
    def _on_mqtt_disconnect(self, client, userdata, rc):
        """MQTT disconnection callback."""
        self.mqtt_connected = False
        if rc != 0:
            self.logger.warning(f"Unexpected MQTT disconnection (code {rc})")
    
    def parse_advertisement_data(self, adv_data: AdvertisementData, config: DeviceConfig) -> Optional[Dict]:
        """
        Parse Sensirion advertisement data for temperature and humidity.
        
        Args:
            adv_data: Advertisement data from the BLE device
            config: Device configuration
            
        Returns:
            Dictionary with parsed sensor values or None if parsing failed
        """
        # Check for Sensirion manufacturer data
        if SENSIRION_COMPANY_ID not in adv_data.manufacturer_data:
            if self.debug_level >= 2:
                self.logger.debug(f"  No Sensirion manufacturer data found")
                self.logger.debug(f"  Available manufacturer IDs: {list(adv_data.manufacturer_data.keys())}")
            return None
        
        raw_data = adv_data.manufacturer_data[SENSIRION_COMPANY_ID]
        
        if self.debug_level >= 2:
            self.logger.debug(f"  Raw manufacturer data ({len(raw_data)} bytes): {raw_data.hex()}")
        
        # Parse the advertisement data with debug logging
        parsed = parse_sensirion_advertisement(raw_data, logger=self.logger, debug_level=self.debug_level)
        
        if not parsed:
            self.logger.warning(f"Failed to parse advertisement data for {config.name}")
            if self.debug_level >= 1:
                self.logger.debug(f"  Raw data: {raw_data.hex()}")
            return None
        
        if self.debug_level >= 1:
            self.logger.debug(f"  Sensor type: {parsed.get('sensor_type', 'Unknown')}")
            self.logger.debug(f"  Device ID: {parsed.get('device_id', 'Unknown')}")
        
        result = {}
        
        # Apply temperature and humidity corrections if both are available
        if 'temperature' in parsed and 'humidity' in parsed:
            temp_raw = parsed['temperature']
            hum_raw = parsed['humidity']
            
            if self.debug_level >= 1:
                self.logger.debug(f"  Temperature (raw): {temp_raw:.2f}°C")
                self.logger.debug(f"  Humidity (raw): {hum_raw:.1f}%")
            
            # Apply correction using absolute humidity method
            temp_corrected, hum_corrected = correct_humidity(temp_raw, hum_raw, config.temp_offset)
            result['temperature'] = round(temp_corrected, 2)
            result['humidity'] = round(hum_corrected, 1)
            
            if self.debug_level >= 1:
                self.logger.debug(f"  Temperature (corrected): {temp_corrected:.2f}°C (offset: {config.temp_offset:+.1f}K)")
                self.logger.debug(f"  Humidity (corrected): {hum_corrected:.1f}% (via absolute humidity)")
        
        # Add CO2 if present (for MyCO2 devices)
        if 'co2' in parsed:
            result['co2'] = parsed['co2']
            if self.debug_level >= 1:
                self.logger.debug(f"  CO2: {parsed['co2']} ppm")
        
        # Add VOC if present (for AQI/MyCO2 devices)
        if 'voc' in parsed:
            result['voc'] = parsed['voc']
            if self.debug_level >= 1:
                self.logger.debug(f"  VOC: {parsed['voc']} index")
        
        return result if result else None
    
    async def scan_and_parse_devices(self) -> List[SensorReading]:
        """Scan for configured Sensirion BLE devices and parse their advertisement data."""
        self.logger.debug(f"Scanning for configured Sensirion devices ({self.scan_duration}s)...")
        if self.debug_level >= 2:
            self.logger.debug(f"Looking for devices: {[f'{d.name} ({d.address})' for d in self.devices]}")
        
        # Discover devices
        if self.debug_level >= 2:
            scan_start = time.time()
        devices = await BleakScanner.discover(timeout=self.scan_duration, return_adv=True)
        if self.debug_level >= 2:
            scan_duration = time.time() - scan_start
            self.logger.debug(f"Scan completed in {scan_duration:.2f}s, found {len(devices)} total BLE devices")
        
        # Filter for configured devices and parse their data
        readings = []
        for device, adv_data in devices.values():
            device_addr = device.address.lower()
            for config in self.devices:
                if config.address == device_addr:
                    self.logger.debug(f"Found configured device: {config.name} ({device.address}), RSSI: {adv_data.rssi} dBm")
                    
                    # Parse advertisement data
                    sensor_data = self.parse_advertisement_data(adv_data, config)
                    
                    if sensor_data:
                        reading = SensorReading(
                            address=device.address,
                            name=config.name,
                            device_type=config.device_type,
                            rssi=adv_data.rssi,
                            temperature=sensor_data.get('temperature'),
                            humidity=sensor_data.get('humidity'),
                            co2=sensor_data.get('co2'),
                            voc=sensor_data.get('voc')
                        )
                        readings.append(reading)
                        self.logger.debug(f"Successfully parsed data from {config.name}")
                    else:
                        self.logger.warning(f"No parseable data from {config.name}")
                    
                    break
        
        if not readings:
            self.logger.warning(f"No configured devices found or parsed. Looking for: {[d.address for d in self.devices]}")
            if self.debug_level >= 2:
                self.logger.warning(f"  All discovered devices: {[dev.address.lower() for dev, _ in devices.values()]}")
        
        return readings
    
    def publish_reading(self, reading: SensorReading) -> None:
        """Publish sensor reading to MQTT."""
        if not self.mqtt_connected:
            self.logger.warning("MQTT not connected, skipping publish")
            return
        
        # Determine base topic based on device type
        # HUMI: energiemonitor/humi/{name}/...
        # CO2:  energiemonitor/co2/{name}/...
        # AQI:  energiemonitor/aqi/{name}/...
        base_topic = f"energiemonitor/{reading.device_type}/{reading.name}"
        
        # Publish individual values to separate topics
        values_published = 0
        
        if reading.temperature is not None:
            topic = f"{base_topic}/temperature"
            self._publish_value(topic, reading.temperature)
            values_published += 1
        
        if reading.humidity is not None:
            topic = f"{base_topic}/humidity"
            self._publish_value(topic, reading.humidity)
            values_published += 1
        
        if reading.co2 is not None:
            topic = f"{base_topic}/co2"
            self._publish_value(topic, reading.co2)
            values_published += 1
        
        if reading.voc is not None:
            topic = f"{base_topic}/voc"
            self._publish_value(topic, reading.voc)
            values_published += 1
        
        if reading.battery is not None:
            topic = f"{base_topic}/battery"
            self._publish_value(topic, reading.battery)
            values_published += 1
        
        if reading.rssi is not None:
            topic = f"{base_topic}/rssi"
            self._publish_value(topic, reading.rssi)
            values_published += 1
        
        if values_published > 0:
            self.logger.debug(f"Published {values_published} values for {reading.name}")
    
    def _publish_value(self, topic: str, value) -> None:
        """Publish a single value to an MQTT topic."""
        try:
            result = self.mqtt_client.publish(topic, str(value), qos=0, retain=False)
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                self.logger.warning(f"Failed to publish to {topic}: {result.rc}")
        except Exception as e:
            self.logger.warning(f"MQTT publish error for {topic}: {e}")
    
    def _should_scan_device(self, device_type: str) -> bool:
        """Check if enough time has passed to scan a specific device type."""
        current_time = time.time()
        
        interval_map = {
            'co2': self.co2_scan_interval,
            'aiq': self.aiq_scan_interval,
            'humi': self.humi_scan_interval
        }
        
        interval = interval_map.get(device_type, self.co2_scan_interval)
        last_scan = self.last_scan_times.get(device_type, 0)
        
        return (current_time - last_scan) >= interval
    
    async def run(self) -> None:
        """Main run loop."""
        self._setup_mqtt()
        
        self.logger.info("Starting BLE scanner loop (advertisement mode)...")
        
        while True:
            try:
                current_time = time.time()
                
                # Determine which device types should be scanned
                scan_co2 = self._should_scan_device('co2')
                scan_aiq = self._should_scan_device('aiq')
                scan_humi = self._should_scan_device('humi')
                
                if scan_co2 or scan_aiq or scan_humi:
                    # Scan and parse devices
                    readings = await self.scan_and_parse_devices()
                    
                    # Filter and publish readings based on intervals
                    for reading in readings:
                        device_type = reading.device_type
                        
                        # Check if this device type should be published
                        should_publish = False
                        if device_type == 'co2' and scan_co2:
                            should_publish = True
                            self.last_scan_times['co2'] = current_time
                        elif device_type == 'aiq' and scan_aiq:
                            should_publish = True
                            self.last_scan_times['aiq'] = current_time
                        elif device_type == 'humi' and scan_humi:
                            should_publish = True
                            self.last_scan_times['humi'] = current_time
                        
                        if should_publish:
                            self.publish_reading(reading)
                    
                    self.logger.debug(f"Scan complete. Published readings for: CO2={scan_co2}, AQI={scan_aiq}, HUMI={scan_humi}")
                
                # Sleep for a short time before next check
                await asyncio.sleep(10)
                
            except KeyboardInterrupt:
                self.logger.info("Shutting down...")
                break
            except Exception as e:
                self.logger.error(f"Error in main loop: {e}")
                await asyncio.sleep(10)
        
        # Cleanup
        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()

# ============================================================================
# MAIN
# ============================================================================

async def main():
    """Main entry point."""
    scanner = SensirionBLEScanner()
    await scanner.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown requested...exiting")
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)
