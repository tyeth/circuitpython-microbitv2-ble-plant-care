# PlantBit - BLE Plant Watering System

Automated plant watering using CircuitPython, BLE, and Adafruit hardware. A micro:bit v2 reads soil moisture and controls a pump over BLE, while a Feather ESP32-S2 coordinates multi-zone watering with solenoid valves.

## Hardware

### Sensor Node (micro:bit v2)

| Component | Purpose |
|-----------|---------|
| [BBC micro:bit v2](https://microbit.org/) | Main controller, BLE radio, 5x5 LED display |
| [Adafruit Bonsai Buckaroo](https://www.adafruit.com/product/4534) | MOSFET pump driver + soil moisture sensor breakout |
| 3V DC water pump | Waters the plant |
| Soil moisture sensor | Resistive sensor on pin P1 |

**Wiring:** The Bonsai Buckaroo clips onto the micro:bit edge connector. P1 = soil moisture (analog), P2 = pump MOSFET (digital).

### Controller Node (Feather ESP32-S2 Reverse TFT)

| Component | Purpose |
|-----------|---------|
| [Feather ESP32-S2 Reverse TFT](https://www.adafruit.com/product/5345) | WiFi controller with built-in 240x135 TFT |
| [8-Channel Solenoid Driver](https://www.adafruit.com/product/6318) | I2C STEMMA QT solenoid/valve driver (MCP23017) |
| 5x solenoid valves | One per plant zone |

**Wiring:** Solenoid driver connects via STEMMA QT cable. Solenoid valves wire to terminal blocks on channels 0-4.

## Firmware

Both devices run [CircuitPython](https://circuitpython.org/). The micro:bit v2 uses CircuitPython 10.x (nRF52833 build).

### micro:bit v2 - BLE Soil Sensor + Pump

```
examples/microbitv2_ble_plant_care/code.py
```

**Features:**
- Soil moisture reading every wake cycle (default 60s)
- BLE peripheral with custom GATT service for remote control
- 5x5 LED graph: 5-day moisture history (high/low per day)
- Status icons: smiley (boot), water drop (pump), diamond (BLE), circle (sensor read)
- Buttons: A = re-read sensor, B = activate pump (both wake from sleep)
- Configurable sleep modes for battery life
- Pump cooldown: skips moisture reads for 10s after pumping (soil needs time to absorb)

**No additional libraries required** - uses built-in `_bleio`, `alarm`, `analogio`, `digitalio`.

### Feather ESP32-S2 - Multi-Zone Solenoid Controller

```
examples/feather_esp32s2_reverse_tft_solenoid/code.py
```

**Features:**
- Controls 5 solenoid valves via I2C (8-channel driver, channels 0-4)
- Polls Adafruit IO for moisture data over WiFi
- TFT display: per-zone moisture, color-coded status, watering activity
- Auto-waters zones below threshold (default 30%) with cooldown
- Currently uses a single moisture value as proxy for all 5 zones

**Note:** The Feather ESP32-S2 does not support BLE. If you want BLE on the controller side, use an ESP32-S3 variant (we will sort this later).

**Required CircuitPython libraries** (install via [circup](https://github.com/adafruit/circup) or the [bundle](https://circuitpython.org/libraries)):
- `adafruit_mcp230xx`
- `adafruit_requests`
- `adafruit_display_text`

## BLE Service

The micro:bit advertises as **"PlantBit"** with a custom GATT service.

| Characteristic | UUID | Properties | Format |
|----------------|------|------------|--------|
| Service | `12340001-1234-5678-1234-56789abcdef0` | - | - |
| Moisture | `12340002-1234-5678-1234-56789abcdef0` | READ, NOTIFY, WRITE | 1 byte, 0-100 (%) |
| Pump | `12340003-1234-5678-1234-56789abcdef0` | READ, WRITE | 1 byte = duration in seconds |
| Sleep Interval | `12340004-1234-5678-1234-56789abcdef0` | READ, WRITE | uint16 LE, seconds (10-3600) |

### Testing with nRF Connect (Android/iOS)

1. Scan for **"PlantBit"**
2. Connect and expand the service starting `12340001...`
3. **Read moisture:** tap the read button on `...0002` - returns a byte like `0x1E` (30%)
4. **Force a fresh moisture reading:** write a different byte each time (e.g. `00` then `01`, or a rolling millis byte) to `...0002`, then read again
5. **Trigger pump for 3 seconds:** write `03` to `...0003`
6. **Set sleep to 5 minutes:** write `2C01` to `...0004` (0x012C = 300 in little-endian)

Common sleep interval hex values:

| Seconds | Hex (LE) |
|---------|----------|
| 10 | `0A00` |
| 60 | `3C00` |
| 120 | `7800` |
| 300 (5 min) | `2C01` |

## Configuration

### micro:bit v2 - settings.toml

Create `settings.toml` on the CIRCUITPY drive to override defaults:

```toml
SLEEP_MODE = "light"
SLEEP_SECONDS = 60
```

| Setting | Values | Default | Description |
|---------|--------|---------|-------------|
| `SLEEP_MODE` | `"light"`, `"deep"`, `"none"` | `"light"` | Sleep strategy between wake cycles |
| `SLEEP_SECONDS` | `10` - `3600` | `60` | Seconds between wake cycles (also settable via BLE) |

**Sleep modes:**

| Mode | Power | Buttons wake | BLE/USB alive | Program state |
|------|-------|-------------|---------------|---------------|
| `light` | Medium | Yes | Yes | Continues |
| `deep` | Lowest | Yes | No (reconnect needed) | Restarts |
| `none` | Highest | No (waits for timer) | Yes | Continues |

### Feather ESP32-S2 - settings.toml

Copy `settings.toml.example` to `settings.toml` and fill in credentials:

```toml
CIRCUITPY_WIFI_SSID = "your_wifi_ssid"
CIRCUITPY_WIFI_PASSWORD = "your_wifi_password"
ADAFRUIT_AIO_USERNAME = "your_aio_username"
ADAFRUIT_AIO_KEY = "aio_xxxxxxxxxxxx"
MOISTURE_FEED = "plantbit.moisture"
```

## LED Display

The micro:bit 5x5 LED matrix shows a moisture history graph:

```
Column:  0    1    2    3    4
         today              oldest

         *         *              Row 0  ┐
         *    *    *              Row 1  ├ Daily HIGH (1-3 LEDs from top)
              *                   Row 2  ┘
                        *    *    Row 3  ┐ Daily LOW (1-3 LEDs from bottom)
                   *    *    *    Row 4  ┘
```

Each column represents one day. Top LEDs show the highest moisture reading, bottom LEDs show the lowest. The mapping is:

| Moisture | LEDs |
|----------|------|
| 0% | 0 |
| 1-33% | 1 |
| 34-66% | 2 |
| 67-100% | 3 |

Status icons flash for 1 second over the graph, then revert:
- **Boot:** smiley face
- **Pump active:** water droplet
- **BLE connected:** diamond
- **Sensor read:** circle

## Architecture

```
┌─────────────────┐     BLE      ┌──────────────┐
│  micro:bit v2   │◄────────────►│  Phone / App │
│  + Buckaroo     │              │  (nRF Connect)│
│                 │              └──────────────┘
│  Soil sensor ───┤
│  Pump MOSFET ───┤                    │
│  5x5 LED ───────┤              Adafruit IO
│  BLE peripheral │              (MQTT/REST)
└─────────────────┘                    │
                                       ▼
                              ┌─────────────────┐
                              │ Feather ESP32-S2 │
                              │ Reverse TFT      │
                              │                   │
                              │  WiFi ────────────┤
                              │  TFT display ─────┤
                              │  I2C ─────┐       │
                              └───────────┼───────┘
                                          │
                                          ▼
                              ┌─────────────────┐
                              │  8-Ch Solenoid   │
                              │  Driver (#6318)  │
                              │                   │
                              │  Ch 0-4: valves  │
                              │  Ch 5-7: spare   │
                              └─────────────────┘
```

**Current state:** The micro:bit reads a single soil sensor and exposes it over BLE. The Feather polls Adafruit IO and uses that value as a proxy for all 5 zones.

**Future:** Each zone gets its own sensor (I2C, WiFi, or BLE), and the micro:bit publishes to Adafruit IO via a BLE-to-WiFi gateway.

## Uploading to micro:bit v2

The micro:bit v2 with CircuitPython exposes a CIRCUITPY USB drive. Copy files directly:

```bash
# If USB drive is available, just copy
cp code.py /Volumes/CIRCUITPY/  # macOS
cp code.py /media/CIRCUITPY/    # Linux
copy code.py D:\                # Windows (check drive letter)
```

If using mpremote (serial can be flaky - interrupt running code first):

```bash
# Interrupt running code, then copy
python -c "import serial; s=serial.Serial('COM19',115200,timeout=1); s.write(b'\x03\x03\x03'); s.read(4096); s.close()"
mpremote connect COM19 fs cp code.py :/code.py
```

## License

MIT
