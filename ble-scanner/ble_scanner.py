#!/usr/bin/env python3
"""
BLE Scanner for Sensirion Smart Gadgets
Scans BLE devices and publishes sensor data to MQTT broker

Based on Sensirion BLE specification:
https://sensirion.github.io/ble-services/

KEY FINDINGS FROM RESEARCH:
1. Sensirion gadgets broadcast live data in MANUFACTURER DATA of BLE advertisements
2. No connection needed for basic sensor readings (T, H, CO2, VOC)
3. Connection only needed for battery level and history download
4. Sample types encode which sensors are present (Type 3, 4, 6, 8, 10, etc.)
5. Company ID is 0x06D5 (Sensirion)
"""

import asyncio
import os
import sys
import time
import logging
from typing import List, Dict, Optional, Tuple, Set
from dataclasses import dataclass
import struct

from bleak import BleakClient, BleakScanner, BleakError
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
import paho.mqtt.client as mqtt

# ============================================================================
# CONFIGURATION - Editable defaults (overridden by environment variables)
# ============================================================================

# Sensirion Company ID
SENSIRION_COMPANY_ID = 0x06D5

# BLE Service and Characteristic UUIDs (from official spec)
BLE_UUID_BATTERY_SERVICE = "0000180f-0000-1000-8000-00805f9b34fb"
BLE_UUID_BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"

# Sensirion Service UUIDs - for service discovery only
BLE_UUID_TEMP_SERVICE = "00002234-b38d-4985-720e-0f993a68ee41"
BLE_UUID_HUM_SERVICE = "00001234-b38d-4985-720e-0f993a68ee41"
BLE_UUID_CO2_SERVICE = "00007000-b38d-4985-720e-0f993a68ee41"
BLE_UUID_VOC_SERVICE = "00005588-b38d-4985-720e-0f993a68ee41"

# Sensirion service UUIDs for discovery check
SENSIRION_SERVICE_UUIDS = [
    BLE_UUID_TEMP_SERVICE.lower(),
    BLE_UUID_HUM_SERVICE.lower(),
    BLE_UUID_CO2_SERVICE.lower(),
    BLE_UUID_VOC_SERVICE.lower()
]

# Advertisement sample types (from https://sensirion.github.io/ble-services/#/live-data)
# NOTE: Actual sizes observed from real devices differ from spec!
SAMPLE_TYPES = {
    3: {'size': 8, 'signals': ['T', 'H', 'VOC']},        # AQ Minion - actual: 2 floats + uint16 VOC
    4: {'size': 8, 'signals': ['T', 'H']},                # SHT3x
    6: {'size': 8, 'signals': ['T', 'H']},                # SHT40/SHT43
    8: {'size': 6, 'signals': ['T', 'H', 'CO2']},        # MyCO2 Gadget - actual: 2 uint16 (T+H) + uint16 CO2
    10: {'size': 10, 'signals': ['T', 'H', 'CO2']},       # MyCO2 Gadget (CO2 sensors)
}

# Timing constants
DEFAULT_HUMI_SCAN_INTERVAL = 600  # 10 minutes
DEFAULT_CO2_SCAN_INTERVAL = 180   # 3 minutes
DEFAULT_AIQ_SCAN_INTERVAL = 180   # 3 minutes
DEFAULT_SCAN_DURATION = 15        # seconds - longer for passive scanning
DEFAULT_CONNECTION_TIMEOUT = 10   # seconds - only for battery reads
DEFAULT_MAX_RETRIES = 2           # reduced since we're using advertisements
DEFAULT_RETRY_MAX_DURATION = 60   # seconds - shorter for passive scans
DISCOVERY_INTERVAL = 600          # 10 minutes
INITIAL_RETRY_DELAY = 2           # seconds
MAX_RETRY_DELAY = 10              # seconds

# MQTT constants
DEFAULT_MQTT_BROKER = "mqtt"
DEFAULT_MQTT_PORT = 1883
MQTT_QOS = 0

# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class GadgetConfig:
    """Configuration for a single BLE gadget"""
    mac: str
    name: str
    temp_offset: float
    gadget_type: str  # 'humi', 'co2', 'aqi'

@dataclass
class SensorReading:
    """Sensor reading data"""
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    battery: Optional[int] = None
    co2: Optional[int] = None
    voc: Optional[int] = None
    rssi: Optional[int] = None

# ============================================================================
# GLOBAL CONFIGURATION
# ============================================================================

class Config:
    """Global configuration loaded from environment variables"""
    
    def __init__(self):
        self.debug_mode = self._get_bool_env('DEBUG_MODE', True)
        
        # Setup logging
        log_level = logging.DEBUG if self.debug_mode else logging.WARNING
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
            force=True
        )
        self.logger = logging.getLogger(__name__)
        
        # Suppress verbose bleak/dbus logging - must be after basicConfig
        for logger_name in ['bleak', 'bleak.backends', 'bleak.backends.bluezdbus', 'dbus_fast']:
            logging.getLogger(logger_name).setLevel(logging.WARNING)
            logging.getLogger(logger_name).propagate = False
        
        # MQTT configuration
        self.mqtt_broker = os.getenv('MQTT_BROKER', DEFAULT_MQTT_BROKER)
        self.mqtt_port = int(os.getenv('MQTT_PORT', DEFAULT_MQTT_PORT))
        
        # Timing configuration
        self.humi_scan_interval = int(os.getenv('HUMI_SCAN_INTERVAL', DEFAULT_HUMI_SCAN_INTERVAL))
        self.co2_scan_interval = int(os.getenv('CO2_SCAN_INTERVAL', DEFAULT_CO2_SCAN_INTERVAL))
        self.aiq_scan_interval = int(os.getenv('AIQ_SCAN_INTERVAL', DEFAULT_AIQ_SCAN_INTERVAL))
        self.scan_duration = float(os.getenv('SCAN_DURATION', DEFAULT_SCAN_DURATION))
        self.connection_timeout = int(os.getenv('CONNECTION_TIMEOUT', DEFAULT_CONNECTION_TIMEOUT))
        self.max_retries = int(os.getenv('MAX_RETRIES', DEFAULT_MAX_RETRIES))
        self.retry_max_duration = int(os.getenv('RETRY_MAX_DURATION', DEFAULT_RETRY_MAX_DURATION))
        
        # Parse gadget configurations
        self.humi_gadgets = self._parse_gadgets('HUMI')
        self.co2_gadgets = self._parse_gadgets('CO2')
        self.aiq_gadgets = self._parse_gadgets('AIQ')
        
        self.logger.info("Configuration loaded successfully")
        self.logger.debug(f"HUMI gadgets: {len(self.humi_gadgets)}")
        self.logger.debug(f"CO2 gadgets: {len(self.co2_gadgets)}")
        self.logger.debug(f"AQI gadgets: {len(self.aiq_gadgets)}")
    
    def _get_bool_env(self, key: str, default: bool) -> bool:
        """Get boolean environment variable"""
        val = os.getenv(key, str(int(default)))
        return val.strip() in ('1', 'true', 'True', 'yes', 'Yes')
    
    def _parse_gadgets(self, prefix: str) -> List[GadgetConfig]:
        """Parse gadget configuration from environment variables"""
        macs_str = os.getenv(f'{prefix}_GADGETS', '').strip()
        names_str = os.getenv(f'{prefix}_NAMES', '').strip()
        offsets_str = os.getenv(f'{prefix}_TEMP_OFFSET', '').strip()
        
        # Handle empty configurations
        if not macs_str:
            return []
        
        macs = [m.strip().lower() for m in macs_str.split(',') if m.strip()]
        names = [n.strip() for n in names_str.split(',') if n.strip()]
        offsets = [float(o.strip()) for o in offsets_str.split(',') if o.strip()]
        
        # Validation
        if len(macs) != len(names):
            raise ValueError(
                f"{prefix}: Number of MACs ({len(macs)}) != Number of names ({len(names)})"
            )
        if len(macs) != len(offsets):
            raise ValueError(
                f"{prefix}: Number of MACs ({len(macs)}) != Number of offsets ({len(offsets)})"
            )
        
        # Validate MAC addresses
        for mac in macs:
            if not self._is_valid_mac(mac):
                raise ValueError(f"Invalid MAC address: {mac}")
        
        gadget_type = prefix.lower()
        return [
            GadgetConfig(mac=mac, name=name, temp_offset=offset, gadget_type=gadget_type)
            for mac, name, offset in zip(macs, names, offsets)
        ]
    
    def _is_valid_mac(self, mac: str) -> bool:
        """Validate MAC address format"""
        import re
        pattern = r'^([0-9a-f]{2}:){5}[0-9a-f]{2}$'
        return bool(re.match(pattern, mac))

# ============================================================================
# BLE SCANNER
# ============================================================================

class BLEScanner:
    """Handles BLE scanning and data reading using advertisements"""
    
    def __init__(self, config: Config):
        self.config = config
        self.logger = config.logger
        # Cache for discovered devices
        self.advertisement_cache: Dict[str, Tuple[BLEDevice, AdvertisementData]] = {}
    
    def _parse_manufacturer_data(self, mfr_data: bytes) -> Optional[Dict]:
        """Parse Sensirion manufacturer data from advertisement
        
        Format (from spec):
        Bytes 0-1: Company ID (0x06D5 little endian = 0xD5, 0x06)
        Byte 2: Advertisement type (0x00 for sensor data)
        Byte 3: Sample type (3, 4, 6, 8, 10, etc.)
        Bytes 4-5: Device ID (last 2 bytes of MAC)
        Bytes 6+: Sample data (varies by type)
        """
        # Debug: Show raw data
        self.logger.debug(f"Raw mfr_data: {len(mfr_data)} bytes = {mfr_data.hex()}")
        
        if len(mfr_data) < 2:
            self.logger.debug(f"Manufacturer data too short: {len(mfr_data)} bytes")
            return None
        
        # Company ID is already stripped by Bleak - we receive data starting at byte 2
        # So we need to adjust:
        # Byte 0: Advertisement type
        # Byte 1: Sample type
        # Bytes 2-3: Device ID (2 bytes)
        # Bytes 4+: Sample data
        
        adv_type = mfr_data[0]
        sample_type = mfr_data[1]
        
        self.logger.debug(f"Parsed header: adv_type={adv_type:#04x}, sample_type={sample_type}")
        
        if adv_type != 0x00:  # Only process sensor data advertisements
            self.logger.debug(f"Non-sensor advertisement type: {adv_type:#04x}")
            return None
        
        if sample_type not in SAMPLE_TYPES:
            self.logger.debug(f"Unknown sample type: {sample_type}")
            return None
        
        sample_info = SAMPLE_TYPES[sample_type]
        # Total size = adv_type (1) + sample_type (1) + device_id (2) + sample_data
        expected_total = 4 + sample_info['size']
        
        self.logger.debug(f"Sample type {sample_type}: expecting {expected_total} bytes total ({sample_info['size']} data bytes), got {len(mfr_data)} bytes")
        
        if len(mfr_data) < expected_total:
            self.logger.debug(f"Incomplete data for sample type {sample_type}: {len(mfr_data)} < {expected_total}")
            # Try to parse anyway if we have at least the header
            if len(mfr_data) < 4:
                return None
            # Adjust to actual available data
            sample_data = mfr_data[4:]
            self.logger.debug(f"Attempting to parse {len(sample_data)} bytes of sample data (expected {sample_info['size']})")
        else:
            # Extract sample data
            sample_data = mfr_data[4:expected_total]
        
        result = {'sample_type': sample_type, 'signals': sample_info['signals']}
        
        # Parse based on sample type (all values are little-endian)
        try:
            if sample_type == 3:  # T_RH_VOC (8 bytes: uint16 temp, uint16 hum, uint16 voc, uint16 reserved)
                if len(sample_data) < 6:
                    self.logger.debug(f"Type 3: insufficient data ({len(sample_data)} < 6)")
                    return None
                # Parse as uint16 values with scale factors (based on Sensirion format)
                # Temperature: value / 200.0 (degrees C)
                # Humidity: value / 100.0 (percent)
                # VOC: raw value (ppb or index)
                temp_raw, hum_raw, voc_raw = struct.unpack('<HHH', sample_data[:6])
                result['temperature'] = temp_raw / 200.0
                result['humidity'] = hum_raw / 100.0
                result['voc'] = voc_raw
                self.logger.debug(f"Type 3 parsed: T={result['temperature']:.2f}°C (raw={temp_raw}), H={result['humidity']:.1f}% (raw={hum_raw}), VOC={voc_raw}")
            elif sample_type in [4, 6]:  # T_RH (8 bytes: 2 floats)
                if len(sample_data) < 8:
                    self.logger.debug(f"Type {sample_type}: insufficient data ({len(sample_data)} < 8)")
                    return None
                temp, hum = struct.unpack('<ff', sample_data[:8])
                result['temperature'] = temp
                result['humidity'] = hum
                self.logger.debug(f"Type {sample_type} parsed: T={temp:.2f}°C, H={hum:.1f}%")
            elif sample_type == 8:  # T_RH_CO2 (6 bytes: uint16 temp, uint16 hum, uint16 co2)
                if len(sample_data) < 6:
                    self.logger.debug(f"Type 8: insufficient data ({len(sample_data)} < 6)")
                    return None
                # Parse as uint16 values with scale factors
                # Temperature: value / 200.0 (degrees C)
                # Humidity: value / 100.0 (percent)
                # CO2: raw value (ppm)
                temp_raw, hum_raw, co2 = struct.unpack('<HHH', sample_data[:6])
                result['temperature'] = temp_raw / 200.0
                result['humidity'] = hum_raw / 100.0
                result['co2'] = co2
                self.logger.debug(f"Type 8 parsed: T={result['temperature']:.2f}°C (raw={temp_raw}), H={result['humidity']:.1f}% (raw={hum_raw}), CO2={co2}ppm")
            elif sample_type == 10:  # T_RH_CO2 alt (10 bytes: 2 floats + uint16 co2)
                if len(sample_data) < 10:
                    self.logger.debug(f"Type 10: insufficient data ({len(sample_data)} < 10)")
                    return None
                temp, hum, co2 = struct.unpack('<ffH', sample_data[:10])
                result['temperature'] = temp
                result['humidity'] = hum
                result['co2'] = co2
                self.logger.debug(f"Type 10 parsed: T={temp:.2f}°C, H={hum:.1f}%, CO2={co2}ppm")
        except struct.error as e:
            self.logger.debug(f"Struct unpack error for sample type {sample_type}: {e}")
            return None
        
        return result
    
    async def scan_for_devices(self, target_macs: Set[str]) -> Dict[str, Tuple[BLEDevice, AdvertisementData]]:
        """Scan for BLE devices and return those matching target MACs
        
        Returns dict of {mac: (device, advertisement_data)}
        """
        self.logger.debug(f"Scanning for {len(target_macs)} device(s) for {self.config.scan_duration}s...")
        
        found_devices = {}
        
        try:
            # Scan for devices with advertisement data
            devices = await BleakScanner.discover(
                timeout=self.config.scan_duration,
                return_adv=True
            )
            
            # Check each discovered device
            for address, (device, adv_data) in devices.items():
                mac = address.lower()
                if mac in target_macs:
                    found_devices[mac] = (device, adv_data)
                    self.logger.debug(f"Found target device: {mac} (RSSI={adv_data.rssi}dBm)")
            
            # Update cache
            self.advertisement_cache.update(found_devices)
            
        except Exception as e:
            self.logger.error(f"Scan error: {e}")
        
        return found_devices
    
    def read_from_advertisement(self, gadget: GadgetConfig, adv_data: AdvertisementData) -> Optional[SensorReading]:
        """Read sensor data from advertisement manufacturer data"""
        
        # Check for Sensirion manufacturer data
        if SENSIRION_COMPANY_ID not in adv_data.manufacturer_data:
            self.logger.debug(f"No Sensirion manufacturer data for {gadget.name} ({gadget.mac})")
            return None
        
        self.logger.debug(f"Reading advertisement from {gadget.name} ({gadget.mac})")
        mfr_data = adv_data.manufacturer_data[SENSIRION_COMPANY_ID]
        parsed = self._parse_manufacturer_data(mfr_data)
        
        if not parsed:
            self.logger.debug(f"Failed to parse manufacturer data for {gadget.name}")
            return None
        
        # Create reading with available data
        reading = SensorReading(rssi=adv_data.rssi)
        
        if 'temperature' in parsed:
            # Apply temperature offset
            reading.temperature = parsed['temperature'] + gadget.temp_offset
        if 'humidity' in parsed:
            reading.humidity = parsed['humidity']
        if 'co2' in parsed:
            reading.co2 = parsed['co2']
        if 'voc' in parsed:
            reading.voc = parsed['voc']
        
        return reading
    
    async def read_battery_level(self, mac: str) -> Optional[int]:
        """Read battery level (requires connection)"""
        try:
            async with BleakClient(mac, timeout=self.config.connection_timeout) as client:
                if not client.is_connected:
                    return None
                
                battery_bytes = await client.read_gatt_char(BLE_UUID_BATTERY)
                return int(battery_bytes[0]) if battery_bytes else None
                
        except Exception as e:
            self.logger.debug(f"Battery read failed for {mac}: {e}")
            return None

# ============================================================================
# MQTT PUBLISHER
# ============================================================================

class MQTTPublisher:
    """Handles MQTT publishing"""
    
    def __init__(self, config: Config):
        self.config = config
        self.logger = config.logger
        self.client = mqtt.Client()
        self.connected = False
        
        # Set up callbacks
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
    
    def _on_connect(self, client, userdata, flags, rc):
        """Callback for MQTT connection"""
        if rc == 0:
            self.connected = True
            self.logger.info("Connected to MQTT broker")
        else:
            self.logger.error(f"Failed to connect to MQTT broker: {rc}")
    
    def _on_disconnect(self, client, userdata, rc):
        """Callback for MQTT disconnection"""
        self.connected = False
        if rc != 0:
            self.logger.warning(f"Unexpected MQTT disconnection: {rc}")
    
    async def connect(self, max_retries: int = 10, retry_delay: int = 5):
        """Connect to MQTT broker with retry logic"""
        for attempt in range(max_retries):
            try:
                self.logger.info(f"Connecting to MQTT broker at {self.config.mqtt_broker}:{self.config.mqtt_port}")
                self.client.connect(self.config.mqtt_broker, self.config.mqtt_port, 60)
                self.client.loop_start()
                
                # Wait for connection
                timeout = 10
                start = time.time()
                while not self.connected and (time.time() - start) < timeout:
                    await asyncio.sleep(0.1)
                
                if self.connected:
                    return True
                    
            except Exception as e:
                self.logger.warning(f"MQTT connection attempt {attempt + 1}/{max_retries} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
        
        self.logger.error("Failed to connect to MQTT broker after all retries")
        return False
    
    def publish_reading(self, gadget: GadgetConfig, reading: SensorReading):
        """Publish sensor reading to MQTT"""
        base_topic = f"energiemonitor/{gadget.gadget_type}/{gadget.name}"
        
        if reading.temperature is not None:
            self.client.publish(f"{base_topic}/temperature", f"{reading.temperature:.2f}", qos=MQTT_QOS)
        if reading.humidity is not None:
            self.client.publish(f"{base_topic}/humidity", f"{reading.humidity:.2f}", qos=MQTT_QOS)
        if reading.co2 is not None:
            self.client.publish(f"{base_topic}/co2", str(reading.co2), qos=MQTT_QOS)
        if reading.voc is not None:
            self.client.publish(f"{base_topic}/voc", str(reading.voc), qos=MQTT_QOS)
        if reading.battery is not None:
            self.client.publish(f"{base_topic}/battery", str(reading.battery), qos=MQTT_QOS)
        if reading.rssi is not None:
            self.client.publish(f"{base_topic}/rssi", str(reading.rssi), qos=MQTT_QOS)
    
    def disconnect(self):
        """Disconnect from MQTT broker"""
        self.client.loop_stop()
        self.client.disconnect()

# ============================================================================
# MAIN SCANNER LOGIC
# ============================================================================

class ScanCoordinator:
    """Coordinates scanning of all gadgets with retry logic"""
    
    def __init__(self, config: Config, scanner: BLEScanner, publisher: MQTTPublisher):
        self.config = config
        self.scanner = scanner
        self.publisher = publisher
        self.logger = config.logger
        
        # Track last scan times
        self.last_humi_scan = 0
        self.last_co2_scan = 0
        self.last_aqi_scan = 0
        self.last_discovery = time.time()
        
        # Build set of all configured MACs
        self.all_macs = set()
        for gadget in (self.config.humi_gadgets + self.config.co2_gadgets + self.config.aiq_gadgets):
            self.all_macs.add(gadget.mac.lower())
    
    async def discover_nearby_devices(self):
        """Discover all nearby BLE devices and identify Sensirion gadgets"""
        if not self.config.debug_mode:
            return
        
        try:
            self.logger.debug("Discovering nearby BLE devices...")
            devices = await BleakScanner.discover(timeout=10.0, return_adv=True)
            
            sensirion_found = []
            configured_found = []
            other_count = 0
            
            for address, (device, adv_data) in devices.items():
                mac = address.lower()
                
                # Check if it's a Sensirion device (by manufacturer data)
                if SENSIRION_COMPANY_ID in adv_data.manufacturer_data:
                    mfr_data = adv_data.manufacturer_data[SENSIRION_COMPANY_ID]
                    parsed = self.scanner._parse_manufacturer_data(mfr_data)
                    
                    if parsed:
                        signals = "+".join(parsed['signals'])
                        name = device.name or "Unknown"
                        rssi = adv_data.rssi
                        
                        if mac in self.all_macs:
                            configured_found.append((name, mac, rssi, signals))
                        else:
                            sensirion_found.append((name, mac, rssi, signals))
                else:
                    other_count += 1
            
            # Updated message format
            if configured_found:
                self.logger.debug(f"Found {len(sensirion_found)} additional Sensirion device(s), {len(configured_found)} configured device(s), {other_count} other device(s)")
            else:
                self.logger.debug(f"Found {len(sensirion_found)} Sensirion device(s), {other_count} other device(s)")
            
            # Show configured devices
            if configured_found:
                for name, mac, rssi, signals in configured_found:
                    self.logger.debug(f"  [CONFIGURED] '{name}' ({mac}) RSSI={rssi}dBm [{signals}]")
            
            # Show additional (unconfigured) devices
            if sensirion_found:
                for name, mac, rssi, signals in sensirion_found:
                    self.logger.debug(f"  [SENSIRION] '{name}' ({mac}) RSSI={rssi}dBm [{signals}]")
                    
        except Exception as e:
            self.logger.debug(f"Discovery error: {e}")
    
    async def scan_gadgets(self, gadgets: List[GadgetConfig]) -> Dict[str, Optional[SensorReading]]:
        """Scan gadgets using advertisement data"""
        if not gadgets:
            return {}
        
        # Get target MACs
        target_macs = {g.mac.lower() for g in gadgets}
        
        # Scan for devices
        found_devices = await self.scanner.scan_for_devices(target_macs)
        
        # Process readings
        readings = {}
        for gadget in gadgets:
            mac = gadget.mac.lower()
            
            if mac in found_devices:
                device, adv_data = found_devices[mac]
                reading = self.scanner.read_from_advertisement(gadget, adv_data)
                
                if reading:
                    readings[mac] = reading
                    
                    # Log successful read
                    parts = [f"{gadget.gadget_type.upper()} '{gadget.name}'"]
                    if reading.temperature is not None:
                        parts.append(f"T={reading.temperature:.2f}°C")
                    if reading.humidity is not None:
                        parts.append(f"H={reading.humidity:.1f}%")
                    if reading.co2 is not None:
                        parts.append(f"CO2={reading.co2}ppm")
                    if reading.voc is not None:
                        parts.append(f"VOC={reading.voc}")
                    parts.append(f"RSSI={reading.rssi}dBm")
                    
                    self.logger.info(f"[OK] {', '.join(parts)}")
                else:
                    readings[mac] = None
            else:
                readings[mac] = None
        
        return readings
    
    async def run_scan_cycle(self):
        """Run a single scan cycle based on independent timers"""
        current_time = time.time()
        
        # Device discovery every 10 minutes (if debug mode)
        if (current_time - self.last_discovery) >= DISCOVERY_INTERVAL:
            await self.discover_nearby_devices()
            self.last_discovery = current_time
        
        # Determine which gadget types to scan
        scan_co2 = (current_time - self.last_co2_scan) >= self.config.co2_scan_interval
        scan_aqi = (current_time - self.last_aqi_scan) >= self.config.aiq_scan_interval
        scan_humi = (current_time - self.last_humi_scan) >= self.config.humi_scan_interval
        
        if not (scan_co2 or scan_aqi or scan_humi):
            return
        
        self.logger.info(f"Scan cycle: CO2={scan_co2}, AQI={scan_aqi}, HUMI={scan_humi}")
        
        # Collect all gadgets to scan
        gadgets_to_scan = []
        if scan_co2:
            gadgets_to_scan.extend(self.config.co2_gadgets)
        if scan_aqi:
            gadgets_to_scan.extend(self.config.aiq_gadgets)
        if scan_humi:
            gadgets_to_scan.extend(self.config.humi_gadgets)
        
        if not gadgets_to_scan:
            return
        
        # Phase 1: Scan all gadgets
        retry_start = time.time()
        readings = await self.scan_gadgets(gadgets_to_scan)
        
        # Publish successful readings
        for gadget in gadgets_to_scan:
            mac = gadget.mac.lower()
            if mac in readings and readings[mac] is not None:
                self.publisher.publish_reading(gadget, readings[mac])
        
        # Track failed gadgets
        failed_gadgets = [g for g in gadgets_to_scan if readings.get(g.mac.lower()) is None]
        
        # Phase 2: Retry failed gadgets
        retry_count = 1
        while failed_gadgets and retry_count <= self.config.max_retries:
            # Check timeout
            if (time.time() - retry_start) > self.config.retry_max_duration:
                self.logger.warning(f"Retry timeout ({self.config.retry_max_duration}s) exceeded")
                break
            
            delay = min(INITIAL_RETRY_DELAY * (2 ** (retry_count - 1)), MAX_RETRY_DELAY)
            self.logger.debug(f"Retry {retry_count}/{self.config.max_retries} for {len(failed_gadgets)} gadgets ({delay}s delay)")
            await asyncio.sleep(delay)
            
            # Retry scan
            retry_readings = await self.scan_gadgets(failed_gadgets)
            
            # Publish and update failed list
            still_failed = []
            for gadget in failed_gadgets:
                mac = gadget.mac.lower()
                if mac in retry_readings and retry_readings[mac] is not None:
                    self.publisher.publish_reading(gadget, retry_readings[mac])
                else:
                    still_failed.append(gadget)
            
            failed_gadgets = still_failed
            retry_count += 1
        
        # Log persistent failures
        if failed_gadgets:
            for gadget in failed_gadgets:
                self.logger.warning(f"[FAIL] {gadget.gadget_type.upper()} '{gadget.name}' ({gadget.mac}): Not found after {self.config.max_retries} retries")
        
        # Update last scan times
        if scan_co2:
            self.last_co2_scan = current_time
        if scan_aqi:
            self.last_aqi_scan = current_time
        if scan_humi:
            self.last_humi_scan = current_time
    
    def get_next_scan_interval(self) -> float:
        """Calculate time until next scan"""
        current_time = time.time()
        
        intervals = []
        
        if self.config.co2_gadgets:
            time_until_co2 = self.config.co2_scan_interval - (current_time - self.last_co2_scan)
            intervals.append(max(0, time_until_co2))
        
        if self.config.aiq_gadgets:
            time_until_aqi = self.config.aiq_scan_interval - (current_time - self.last_aqi_scan)
            intervals.append(max(0, time_until_aqi))
        
        if self.config.humi_gadgets:
            time_until_humi = self.config.humi_scan_interval - (current_time - self.last_humi_scan)
            intervals.append(max(0, time_until_humi))
        
        return min(intervals) if intervals else self.config.co2_scan_interval

# ============================================================================
# MAIN
# ============================================================================

async def main():
    """Main entry point"""
    try:
        # Load configuration
        config = Config()
        
        # Initialize components
        scanner = BLEScanner(config)
        publisher = MQTTPublisher(config)
        
        # Connect to MQTT
        if not await publisher.connect():
            config.logger.error("Failed to connect to MQTT broker, exiting")
            sys.exit(1)
        
        # Initialize coordinator
        coordinator = ScanCoordinator(config, scanner, publisher)
        
        config.logger.info("BLE Scanner started (ADVERTISEMENT MODE)")
        config.logger.info(f"Scan intervals: HUMI={config.humi_scan_interval}s, CO2={config.co2_scan_interval}s, AQI={config.aiq_scan_interval}s")
        config.logger.info(f"Scan duration: {config.scan_duration}s per cycle")
        
        # Initial device discovery
        await coordinator.discover_nearby_devices()
        
        # Main loop
        while True:
            try:
                await coordinator.run_scan_cycle()
                
                # Calculate time until next scan
                sleep_time = coordinator.get_next_scan_interval()
                if sleep_time > 0:
                    config.logger.info(f"Sleeping for {sleep_time:.1f}s until next scan")
                    await asyncio.sleep(sleep_time)
                
            except Exception as e:
                config.logger.error(f"Error in scan cycle: {e}", exc_info=True)
                await asyncio.sleep(60)
    
    except KeyboardInterrupt:
        config.logger.info("Shutdown requested")
    except Exception as e:
        config.logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        if 'publisher' in locals():
            publisher.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
