#!/usr/bin/env python3
"""
BLE Scanner for Sensirion Smart Gadgets
Scans BLE devices and publishes sensor data to MQTT broker
"""

import asyncio
import os
import sys
import time
import logging
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
import struct

from bleak import BleakClient, BleakScanner
import paho.mqtt.client as mqtt

# ============================================================================
# CONFIGURATION - Editable defaults (overridden by environment variables)
# ============================================================================

# BLE Service and Characteristic UUIDs
BLE_UUID_BATTERY_SERVICE = "0000180f-0000-1000-8000-00805f9b34fb"
BLE_UUID_BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"
BLE_UUID_TEMP_SERVICE = "00002234-b38d-4985-720e-0f993a68ee41"
BLE_UUID_TEMP = "00002235-b38d-4985-720e-0f993a68ee41"
BLE_UUID_HUM_SERVICE = "00001234-b38d-4985-720e-0f993a68ee41"
BLE_UUID_HUM = "00001235-b38d-4985-720e-0f993a68ee41"
BLE_UUID_CO2_SERVICE = "00007000-b38d-4985-720e-0f993a68ee41"
BLE_UUID_CO2 = "00007001-b38d-4985-720e-0f993a68ee41"
BLE_UUID_VOC_SERVICE = "00005588-b38d-4985-720e-0f993a68ee41"
BLE_UUID_VOC = "00005582-b38d-4985-720e-0f993a68ee41"

# Sensirion service UUIDs for discovery check
SENSIRION_SERVICE_UUIDS = [
    BLE_UUID_TEMP_SERVICE,
    BLE_UUID_HUM_SERVICE,
    BLE_UUID_CO2_SERVICE,
    BLE_UUID_VOC_SERVICE
]

# Timing constants
DEFAULT_HUMI_SCAN_INTERVAL = 600  # 10 minutes
DEFAULT_CO2_SCAN_INTERVAL = 180   # 3 minutes
DEFAULT_AIQ_SCAN_INTERVAL = 180   # 3 minutes
DEFAULT_CONNECTION_TIMEOUT = 30   # seconds
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_MAX_DURATION = 120  # seconds
DISCOVERY_INTERVAL = 600          # 10 minutes
INITIAL_RETRY_DELAY = 1           # seconds
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
    """Handles BLE scanning and data reading"""
    
    def __init__(self, config: Config):
        self.config = config
        self.logger = config.logger
    
    async def read_characteristic(self, client: BleakClient, uuid: str) -> Optional[bytes]:
        """Read a characteristic value"""
        try:
            value = await client.read_gatt_char(uuid)
            return value
        except Exception as e:
            self.logger.debug(f"Failed to read characteristic {uuid}: {e}")
            return None
    
    async def scan_humi_gadget(self, gadget: GadgetConfig) -> Optional[SensorReading]:
        """Scan humidity/temperature gadget"""
        
        try:
            async with BleakClient(
                gadget.mac,
                timeout=self.config.connection_timeout
            ) as client:
                if not client.is_connected:
                    return None
                
                # Get RSSI (from scanner, not client)
                rssi = await self._get_rssi(gadget.mac)
                
                # Read battery
                battery_bytes = await self.read_characteristic(client, BLE_UUID_BATTERY)
                battery = int(battery_bytes[0]) if battery_bytes else None
                
                # Read temperature
                temp_bytes = await self.read_characteristic(client, BLE_UUID_TEMP)
                temperature = struct.unpack('<f', temp_bytes)[0] if temp_bytes else None
                
                # Read humidity
                hum_bytes = await self.read_characteristic(client, BLE_UUID_HUM)
                humidity = struct.unpack('<f', hum_bytes)[0] if hum_bytes else None
                
                # Apply temperature offset
                if temperature is not None:
                    temperature += gadget.temp_offset
                
                reading = SensorReading(
                    temperature=temperature,
                    humidity=humidity,
                    battery=battery,
                    rssi=rssi
                )
                
                self.logger.debug(f"[OK] HUMI '{gadget.name}': T={temperature:.2f}°C, H={humidity:.2f}%, Bat={battery}%, RSSI={rssi}dBm")
                return reading
                
        except Exception as e:
            return None
    
    async def scan_co2_gadget(self, gadget: GadgetConfig) -> Optional[SensorReading]:
        """Scan CO2 gadget"""
        
        try:
            async with BleakClient(
                gadget.mac,
                timeout=self.config.connection_timeout
            ) as client:
                if not client.is_connected:
                    return None
                
                # Get RSSI
                rssi = await self._get_rssi(gadget.mac)
                
                # Read CO2
                co2_bytes = await self.read_characteristic(client, BLE_UUID_CO2)
                co2 = struct.unpack('<H', co2_bytes)[0] if co2_bytes else None
                
                # Read temperature
                temp_bytes = await self.read_characteristic(client, BLE_UUID_TEMP)
                temperature = struct.unpack('<f', temp_bytes)[0] if temp_bytes else None
                
                # Apply temperature offset
                if temperature is not None:
                    temperature += gadget.temp_offset
                
                reading = SensorReading(
                    co2=co2,
                    temperature=temperature,
                    rssi=rssi
                )
                
                self.logger.debug(f"[OK] CO2 '{gadget.name}': CO2={co2}ppm, T={temperature:.2f}°C, RSSI={rssi}dBm")
                return reading
                
        except Exception as e:
            return None
    
    async def scan_aqi_gadget(self, gadget: GadgetConfig) -> Optional[SensorReading]:
        """Scan AQI/VOC gadget"""
        
        try:
            async with BleakClient(
                gadget.mac,
                timeout=self.config.connection_timeout
            ) as client:
                if not client.is_connected:
                    return None
                
                # Get RSSI
                rssi = await self._get_rssi(gadget.mac)
                
                # Read VOC
                voc_bytes = await self.read_characteristic(client, BLE_UUID_VOC)
                voc = int(voc_bytes[0]) if voc_bytes else None
                
                # Read temperature
                temp_bytes = await self.read_characteristic(client, BLE_UUID_TEMP)
                temperature = struct.unpack('<f', temp_bytes)[0] if temp_bytes else None
                
                # Read humidity
                hum_bytes = await self.read_characteristic(client, BLE_UUID_HUM)
                humidity = struct.unpack('<f', hum_bytes)[0] if hum_bytes else None
                
                # Apply temperature offset
                if temperature is not None:
                    temperature += gadget.temp_offset
                
                reading = SensorReading(
                    voc=voc,
                    temperature=temperature,
                    humidity=humidity,
                    rssi=rssi
                )
                
                self.logger.debug(f"[OK] AQI '{gadget.name}': VOC={voc}ppb, T={temperature:.2f}°C, H={humidity:.2f}%, RSSI={rssi}dBm")
                return reading
                
        except Exception as e:
            return None
    
    async def _get_rssi(self, mac: str) -> Optional[int]:
        """Get RSSI value for a device"""
        try:
            devices = await BleakScanner.discover(timeout=5.0, return_adv=True)
            for address, (device, adv_data) in devices.items():
                if address.lower() == mac.lower():
                    return adv_data.rssi
        except Exception as e:
            self.logger.debug(f"Failed to get RSSI for {mac}: {e}")
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
    
    def publish_humi_reading(self, gadget: GadgetConfig, reading: SensorReading):
        """Publish HUMI gadget reading to MQTT"""
        base_topic = f"energiemonitor/humi/{gadget.name}"
        
        if reading.temperature is not None:
            self.client.publish(f"{base_topic}/temperature", f"{reading.temperature:.2f}", qos=MQTT_QOS)
        if reading.humidity is not None:
            self.client.publish(f"{base_topic}/humidity", f"{reading.humidity:.2f}", qos=MQTT_QOS)
        if reading.battery is not None:
            self.client.publish(f"{base_topic}/battery", str(reading.battery), qos=MQTT_QOS)
        if reading.rssi is not None:
            self.client.publish(f"{base_topic}/rssi", str(reading.rssi), qos=MQTT_QOS)
    
    def publish_co2_reading(self, gadget: GadgetConfig, reading: SensorReading):
        """Publish CO2 gadget reading to MQTT"""
        base_topic = f"energiemonitor/co2/{gadget.name}"
        
        if reading.co2 is not None:
            self.client.publish(f"{base_topic}/co2", str(reading.co2), qos=MQTT_QOS)
        if reading.temperature is not None:
            self.client.publish(f"{base_topic}/temperature", f"{reading.temperature:.2f}", qos=MQTT_QOS)
        if reading.rssi is not None:
            self.client.publish(f"{base_topic}/rssi", str(reading.rssi), qos=MQTT_QOS)
    
    def publish_aqi_reading(self, gadget: GadgetConfig, reading: SensorReading):
        """Publish AQI gadget reading to MQTT"""
        base_topic = f"energiemonitor/aqi/{gadget.name}"
        
        if reading.voc is not None:
            self.client.publish(f"{base_topic}/voc", str(reading.voc), qos=MQTT_QOS)
        if reading.temperature is not None:
            self.client.publish(f"{base_topic}/temperature", f"{reading.temperature:.2f}", qos=MQTT_QOS)
        if reading.humidity is not None:
            self.client.publish(f"{base_topic}/humidity", f"{reading.humidity:.2f}", qos=MQTT_QOS)
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
        self.last_discovery = time.time()  # Initialize to now to prevent immediate re-run
    
    async def discover_nearby_devices(self):
        """Discover all nearby BLE devices and check for Sensirion services"""
        if not self.config.debug_mode:
            return
        
        try:
            self.logger.debug("Discovering nearby BLE devices...")
            devices = await BleakScanner.discover(timeout=10.0)
            
            self.logger.debug(f"Found {len(devices)} BLE device(s) total")
            
            # Check for Sensirion services
            await self._check_sensirion_services_internal(devices)
                
        except Exception as e:
            self.logger.debug(f"Device discovery error: {e}")
    
    async def _check_sensirion_services_internal(self, devices):
        """Check discovered devices for Sensirion temperature/humidity services"""
        sensirion_devices = []
        
        self.logger.debug(f"Checking {len(devices)} devices for Sensirion services...")
        
        for device in devices:
            try:
                # Quick connect with short timeout to check services
                async with BleakClient(device.address, timeout=3.0) as client:
                    if not client.is_connected:
                        continue
                    
                    services = client.services
                    found_services = []
                    
                    for service in services:
                        service_uuid = service.uuid.lower()
                        if service_uuid in SENSIRION_SERVICE_UUIDS:
                            # Identify service type
                            if service_uuid == BLE_UUID_TEMP_SERVICE:
                                found_services.append("TEMP")
                            elif service_uuid == BLE_UUID_HUM_SERVICE:
                                found_services.append("HUM")
                            elif service_uuid == BLE_UUID_CO2_SERVICE:
                                found_services.append("CO2")
                            elif service_uuid == BLE_UUID_VOC_SERVICE:
                                found_services.append("VOC")
                    
                    if found_services:
                        name = device.name or "Unknown"
                        mac = device.address.lower()
                        rssi = device.rssi if hasattr(device, 'rssi') else "?"
                        sensirion_devices.append((name, mac, rssi, found_services))
                        
            except asyncio.TimeoutError:
                # Device didn't respond in time, skip it
                continue
            except Exception as e:
                # Skip devices we can't connect to
                continue
        
        if sensirion_devices:
            self.logger.debug(f"Sensirion devices found: {len(sensirion_devices)}")
            for name, mac, rssi, services in sensirion_devices:
                services_str = "+".join(services)
                self.logger.debug(f"  [SENSIRION] '{name}' ({mac}) RSSI={rssi}dBm [{services_str}]")
        else:
            self.logger.debug("No Sensirion devices found")
    
    async def scan_with_retry(self, gadget: GadgetConfig, scan_func) -> Tuple[GadgetConfig, Optional[SensorReading]]:
        """Scan a single gadget with retry logic"""
        for retry in range(self.config.max_retries + 1):
            if retry > 0:
                delay = min(INITIAL_RETRY_DELAY * (2 ** (retry - 1)), MAX_RETRY_DELAY)
                self.logger.debug(f"Retry {retry}/{self.config.max_retries} for {gadget.name} after {delay}s")
                await asyncio.sleep(delay)
            
            reading = await scan_func(gadget)
            if reading is not None:
                return (gadget, reading)
        
        return (gadget, None)
    
    async def scan_gadgets_with_global_retry(self, gadgets: List[GadgetConfig], scan_func, publish_func):
        """Scan all gadgets with global retry queue"""
        if not gadgets:
            return
        
        retry_start = time.time()
        failed_gadgets = []
        
        # Phase 1: Initial scan of all gadgets
        self.logger.debug(f"Phase 1: Scanning {len(gadgets)} gadgets")
        for gadget in gadgets:
            reading = await scan_func(gadget)
            if reading is not None:
                publish_func(gadget, reading)
            else:
                failed_gadgets.append(gadget)
        
        # Phase 2: Retry failed gadgets
        retry_count = 1
        while failed_gadgets and retry_count <= self.config.max_retries:
            # Check if we've exceeded retry duration
            elapsed = time.time() - retry_start
            if elapsed > self.config.retry_max_duration:
                self.logger.warning(f"Retry timeout ({self.config.retry_max_duration}s) exceeded, aborting retries")
                break
            
            delay = min(INITIAL_RETRY_DELAY * (2 ** (retry_count - 1)), MAX_RETRY_DELAY)
            self.logger.debug(f"Phase 2: Retry {retry_count}/{self.config.max_retries} for {len(failed_gadgets)} failed gadgets after {delay}s")
            await asyncio.sleep(delay)
            
            still_failed = []
            for gadget in failed_gadgets:
                reading = await scan_func(gadget)
                if reading is not None:
                    publish_func(gadget, reading)
                else:
                    still_failed.append(gadget)
            
            failed_gadgets = still_failed
            retry_count += 1
        
        # Log warnings for still-failed gadgets
        if failed_gadgets:
            for gadget in failed_gadgets:
                self.logger.warning(f"Failed to read from {gadget.gadget_type.upper()} gadget '{gadget.name}' ({gadget.mac}) after {self.config.max_retries} retries")
    
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
        
        self.logger.info(f"Scan cycle: CO2={scan_co2}, AQI={scan_aqi}, HUMI={scan_humi}")
        
        # Collect all gadgets to scan this cycle
        all_failed_gadgets = []
        retry_start = time.time()
        
        # Phase 1: Initial scan of ALL gadgets (no retries yet)
        self.logger.debug("Phase 1: Initial scan of all scheduled gadgets")
        
        # Scan CO2 gadgets
        if scan_co2 and self.config.co2_gadgets:
            for gadget in self.config.co2_gadgets:
                reading = await self.scanner.scan_co2_gadget(gadget)
                if reading is not None:
                    self.publisher.publish_co2_reading(gadget, reading)
                else:
                    all_failed_gadgets.append((gadget, self.scanner.scan_co2_gadget, self.publisher.publish_co2_reading))
            self.last_co2_scan = current_time
        
        # Scan AQI gadgets
        if scan_aqi and self.config.aiq_gadgets:
            for gadget in self.config.aiq_gadgets:
                reading = await self.scanner.scan_aqi_gadget(gadget)
                if reading is not None:
                    self.publisher.publish_aqi_reading(gadget, reading)
                else:
                    all_failed_gadgets.append((gadget, self.scanner.scan_aqi_gadget, self.publisher.publish_aqi_reading))
            self.last_aqi_scan = current_time
        
        # Scan HUMI gadgets
        if scan_humi and self.config.humi_gadgets:
            for gadget in self.config.humi_gadgets:
                reading = await self.scanner.scan_humi_gadget(gadget)
                if reading is not None:
                    self.publisher.publish_humi_reading(gadget, reading)
                else:
                    all_failed_gadgets.append((gadget, self.scanner.scan_humi_gadget, self.publisher.publish_humi_reading))
            self.last_humi_scan = current_time
        
        # Phase 2: Retry ALL failed gadgets together
        if all_failed_gadgets:
            self.logger.debug(f"Phase 2: Retrying {len(all_failed_gadgets)} failed gadget(s)")
            retry_count = 1
            
            while all_failed_gadgets and retry_count <= self.config.max_retries:
                # Check if we've exceeded retry duration
                elapsed = time.time() - retry_start
                if elapsed > self.config.retry_max_duration:
                    self.logger.warning(f"Retry timeout ({self.config.retry_max_duration}s) exceeded, aborting retries")
                    break
                
                delay = min(INITIAL_RETRY_DELAY * (2 ** (retry_count - 1)), MAX_RETRY_DELAY)
                self.logger.debug(f"  Retry {retry_count}/{self.config.max_retries} ({delay}s delay)")
                await asyncio.sleep(delay)
                
                still_failed = []
                for gadget, scan_func, publish_func in all_failed_gadgets:
                    reading = await scan_func(gadget)
                    if reading is not None:
                        publish_func(gadget, reading)
                    else:
                        still_failed.append((gadget, scan_func, publish_func))
                
                all_failed_gadgets = still_failed
                retry_count += 1
            
            # Log warnings for still-failed gadgets
            if all_failed_gadgets:
                for gadget, _, _ in all_failed_gadgets:
                    self.logger.warning(f"[FAIL] {gadget.gadget_type.upper()} '{gadget.name}' ({gadget.mac}): Failed after {self.config.max_retries} retries")
    
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
        
        # Return minimum interval (next scan to trigger)
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
        
        config.logger.info("BLE Scanner started successfully")
        config.logger.info(f"Scan intervals: HUMI={config.humi_scan_interval}s, CO2={config.co2_scan_interval}s, AQI={config.aiq_scan_interval}s")
        
        # Initial device discovery (includes Sensirion check)
        await coordinator.discover_nearby_devices()
        
        # Main loop
        while True:
            try:
                await coordinator.run_scan_cycle()
                
                # Calculate time until next scan
                sleep_time = coordinator.get_next_scan_interval()
                config.logger.info(f"Sleeping for {sleep_time:.1f}s until next scan")
                await asyncio.sleep(sleep_time)
                
            except Exception as e:
                config.logger.error(f"Error in scan cycle: {e}", exc_info=True)
                await asyncio.sleep(60)  # Wait before retrying
    
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
