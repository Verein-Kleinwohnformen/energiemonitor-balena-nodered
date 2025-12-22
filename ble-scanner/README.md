# Sensirion BLE Scanner - Advertisement Parser

BLE scanner for Sensirion Smart Gadgets that reads sensor data from BLE advertisement packets.

## Overview

This scanner monitors Sensirion BLE devices (SHT3x/SHT4x Gadgets, MyCO2 Gadget) and parses sensor values directly from their advertisement data. The data is then published to an MQTT broker for consumption by Node-RED or other systems.

## Advertisement Data Format

The scanner uses **passive advertisement scanning** to read sensor values. Sensirion devices broadcast their data using manufacturer-specific data in BLE advertisements.

### Manufacturer Data Structure

- **Company ID**: `0x06D5` (Sensirion AG)
- **Data Format**: All multi-byte values are **big-endian**

```
Byte:  [0-1]        [2-3]       [4-5]     [6-7]     [8-9]     [10-11]   ...
Field: Sample Type  Device ID   Data1     Data2     Data3     Data4     ...
```

### Sample Types (Product Identification)

| Sample Type | Product          | Data Fields                    |
|-------------|------------------|--------------------------------|
| `0x0003`    | AQI/VOC Sensor   | Temperature, Humidity, VOC     |
| `0x0004`    | SHT3x Gadget     | Temperature, Humidity          |
| `0x0006`    | SHT4x Gadget     | Temperature, Humidity          |
| `0x0008`    | MyCO2 Gadget     | CO2, Temperature, Humidity, VOC|

### Sensor Data Encoding

All sensor values are encoded as **16-bit unsigned integers** (uint16, big-endian) representing "ticks" that must be converted to physical units.

#### Temperature Conversion

For both SHT3x and SHT4x:

```
Temperature (°C) = -45 + (175.0 × ticks) / (2^16 - 1)
Temperature (°C) = -45 + (175.0 × ticks) / 65535
```

**Example**:
- Ticks: `42598` (0xA666)
- Calculation: `-45 + (175.0 × 42598) / 65535 = 68.5°C`

#### Humidity Conversion

**SHT3x Gadget** (Sample Type `0x0004`):
```
Humidity (%) = (100.0 × ticks) / (2^16 - 1)
Humidity (%) = (100.0 × ticks) / 65535
```

**SHT4x Gadget** (Sample Type `0x0006`):
```
Humidity (%) = -6 + (125.0 × ticks) / (2^16 - 1)
Humidity (%) = -6 + (125.0 × ticks) / 65535
```

**Example (SHT4x)**:
- Ticks: `30146` (0x75D2)
- Calculation: `-6 + (125.0 × 30146) / 65535 = 51.5%`

#### CO2 Conversion

For MyCO2 Gadget (Sample Type `0x0008`):
```
CO2 (ppm) = ticks (direct value)
```

**Example**:
- Ticks: `533` (0x0215)
- Value: `533 ppm`

#### VOC Conversion

For MyCO2 Gadget (Sample Type `0x0008`):
```
VOC (index) = ticks (direct value)
```

**Example**:
- Ticks: `120` (0x0078)
- Value: `120` (VOC index)

### Python Parsing Example

```python
import struct

def parse_sensirion_advertisement(manufacturer_data):
    """Parse Sensirion BLE advertisement data."""
    if len(manufacturer_data) < 6:
        return None
    
    # Parse header (big-endian)
    sample_type, device_id = struct.unpack('>HH', manufacturer_data[0:4])
    
    # Determine product type
    if sample_type == 0x0004:
        # SHT3x Gadget: Temperature + Humidity
        if len(manufacturer_data) < 8:
            return None
        temp_ticks, hum_ticks = struct.unpack('>HH', manufacturer_data[4:8])
        
        temperature = -45 + (175.0 * temp_ticks) / 65535
        humidity = (100.0 * hum_ticks) / 65535  # SHT3x formula
        
        return {
            'device_type': 'SHT3x',
            'device_id': device_id,
            'temperature': temperature,
            'humidity': humidity
        }
    
    elif sample_type == 0x0006:
        # SHT4x Gadget: Temperature + Humidity
        if len(manufacturer_data) < 8:
            return None
        temp_ticks, hum_ticks = struct.unpack('>HH', manufacturer_data[4:8])
        
        temperature = -45 + (175.0 * temp_ticks) / 65535
        humidity = -6 + (125.0 * hum_ticks) / 65535  # SHT4x formula
        
        return {
            'device_type': 'SHT4x',
            'device_id': device_id,
            'temperature': temperature,
            'humidity': humidity
        }
    
    elif sample_type == 0x0008:
        # MyCO2 Gadget: CO2 + Temperature + Humidity + VOC
        if len(manufacturer_data) < 12:
            return None
        co2, temp_ticks, hum_ticks, voc = struct.unpack('>HHHH', manufacturer_data[4:12])
        
        temperature = -45 + (175.0 * temp_ticks) / 65535
        humidity = (100.0 * hum_ticks) / 65535
        
        return {
            'device_type': 'MyCO2',
            'device_id': device_id,
            'co2': co2,
            'temperature': temperature,
            'humidity': humidity,
            'voc': voc
        }
    
    return None
```

## Humidity Correction Algorithm

The scanner applies a psychrometric correction to humidity values when temperature offsets are configured. This preserves absolute humidity while adjusting relative humidity for the corrected temperature.

### Algorithm Steps

1. Calculate saturation vapor pressure at measured temperature
2. Convert relative humidity to absolute humidity (g/m³)
3. Calculate saturation vapor pressure at corrected temperature
4. Convert absolute humidity back to relative humidity at corrected temperature

This ensures that the amount of water vapor in the air remains constant when applying temperature corrections.

## Device-Specific Scan Intervals

The scanner supports different scan intervals per device type to optimize battery life and data freshness:

- **CO2 Sensors**: Default 60 seconds (frequent updates for air quality monitoring)
- **AQI Sensors**: Default 60 seconds (frequent updates for air quality monitoring)
- **Humidity Sensors**: Default 300 seconds (less frequent, slower-changing values)

Configure via environment variables: `CO2_SCAN_INTERVAL`, `AIQ_SCAN_INTERVAL`, `HUMI_SCAN_INTERVAL`

## MQTT Output Format

Data is published to MQTT with the following structure:

**Topic**: `energiemonitor/ble/{device_id}`
- `{device_id}` is derived from the device name (e.g., `myairco2`) or MAC address if name is unknown

**Payload** (JSON):
```json
{
  "address": "dc:e6:4d:5e:e1:f6",
  "name": "MyAirCO2",
  "device_type": "MyCO2",
  "device_id": "0x1234",
  "rssi": -65,
  "timestamp": "2024-01-15T10:30:45.123456",
  "co2": 533,
  "temperature": 22.5,
  "humidity": 45.2,
  "voc": 120
}
```

## Environment Variables

| Variable              | Default   | Description                                      |
|-----------------------|-----------|--------------------------------------------------|
| `SCAN_DURATION`       | `10.0`    | BLE scan duration in seconds                     |
| `CO2_SCAN_INTERVAL`   | `60`      | Scan interval for CO2 devices (seconds)          |
| `AIQ_SCAN_INTERVAL`   | `60`      | Scan interval for AQI devices (seconds)          |
| `HUMI_SCAN_INTERVAL`  | `300`     | Scan interval for humidity devices (seconds)     |
| `MQTT_BROKER`         | `mqtt`    | MQTT broker hostname                             |
| `MQTT_PORT`           | `1883`    | MQTT broker port                                 |
| `MQTT_TOPIC_PREFIX`   | `energiemonitor/ble` | MQTT topic prefix                     |
| `DEBUG_LEVEL`         | `INFO`    | Logging level (DEBUG, INFO, WARNING, ERROR)      |

## References

- [Sensirion BLE Parser (Python)](https://github.com/akx/sensirion-ble) - Reference implementation
- [Sensirion Smart Gadget BLE Protocol](https://developer.sensirion.com/)
- Conversion formulas validated against manufacturer specifications

## Dependencies

- Python 3.11+
- `bleak` - Bluetooth Low Energy library
- `paho-mqtt` - MQTT client library

## License

See main project LICENSE file.
