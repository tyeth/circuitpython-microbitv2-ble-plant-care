# Multi-Plant Watering Controller - Feather ESP32-S3 Reverse TFT
# Uses Adafruit I2C 8-Channel Solenoid Driver (MCP23017, product #6318)
# Solenoids 0-4 = 5 plant zones, 5-7 = spare/future
#
# Reads moisture from Adafruit IO (published by micro:bit BLE gateway).
# For now, one moisture value is used as proxy for all 5 zones.
# Eventually each zone gets its own sensor over I2C/WiFi/BLE.
# Uses BLE (on S3) to request micro:bit pump activation per zone.
#
# Requires libraries: adafruit_mcp230xx, adafruit_requests,
#   adafruit_display_text, adafruit_bitmap_font (optional), adafruit_ble
#
# WiFi credentials and Adafruit IO key go in settings.toml:
#   CIRCUITPY_WIFI_SSID = "yourssid"
#   CIRCUITPY_WIFI_PASSWORD = "yourpass"
#   ADAFRUIT_AIO_USERNAME = "youruser"
#   ADAFRUIT_AIO_KEY = "aio_xxxx"
#   MOISTURE_FEED = "plantbit.moisture"

import time
import os
import board
import busio
import displayio
import terminalio
import wifi
import ssl
import socketpool
import adafruit_requests
from adafruit_ble import BLERadio
from adafruit_ble.advertising.standard import ProvideServicesAdvertisement
from adafruit_ble.characteristics import Characteristic
from adafruit_ble.services import Service
from adafruit_ble.uuid import UUID
from adafruit_display_text import label
from adafruit_mcp230xx.mcp23017 import MCP23017

# --- Config ---
NUM_ZONES = 5
POLL_INTERVAL = 60          # seconds between Adafruit IO polls
WATER_THRESHOLD = 30        # moisture % below which to water
WATER_DURATION = 3          # seconds per solenoid activation (default)
WATER_COOLDOWN = 300        # minimum seconds between waterings per zone
SOLENOID_PINS = (0, 1, 2, 3, 4)  # MCP23017 pin numbers for zones 0-4

# Per-zone watering durations (seconds). Falls back to WATER_DURATION.
ZONE_WATER_SECONDS = [WATER_DURATION] * NUM_ZONES

# --- BLE (micro:bit pump service) ---
PLANTBIT_NAME = "PlantBit"
BLE_SCAN_SECONDS = 4
BLE_CONNECT_TIMEOUT = 6
BLE_CONNECT_RETRIES = 2
SKIP_WATER_IF_PUMP_FAIL = False
SVC_UUID = UUID("12340001-1234-5678-1234-56789abcdef0")
MOIST_UUID = UUID("12340002-1234-5678-1234-56789abcdef0")
PUMP_UUID = UUID("12340003-1234-5678-1234-56789abcdef0")
SLEEP_UUID = UUID("12340004-1234-5678-1234-56789abcdef0")

# --- Adafruit IO ---
AIO_USER = os.getenv("ADAFRUIT_AIO_USERNAME", "")
AIO_KEY = os.getenv("ADAFRUIT_AIO_KEY", "")
MOISTURE_FEED = os.getenv("MOISTURE_FEED", "plantbit.moisture")
AIO_URL = "https://io.adafruit.com/api/v2/{}/feeds/{}/data/last"

# --- Zone names (customize per plant) ---
ZONE_NAMES = ["Fern", "Basil", "Cactus", "Ivy", "Mint"]

# --- Colors ---
COL_BG = 0x000000
COL_DRY = 0xFF4444
COL_OK = 0x44FF44
COL_WET = 0x4488FF
COL_VALVE = 0xFFAA00
COL_TEXT = 0xCCCCCC
COL_HEAD = 0xFFFFFF


class SolenoidDriver:
    """Control solenoids via MCP23017 on the 8-channel driver board."""
    def __init__(self, i2c, address=0x27):
        self.mcp = MCP23017(i2c, address=address)
        self.pins = []
        for pin_num in SOLENOID_PINS:
            p = self.mcp.get_pin(pin_num)
            p.switch_to_output(value=False)
            self.pins.append(p)

    def turn_on(self, zone):
        if 0 <= zone < len(self.pins):
            print("SOLENOID", zone, "(", ZONE_NAMES[zone], ") ON")
            self.pins[zone].value = True

    def turn_off(self, zone):
        if 0 <= zone < len(self.pins):
            self.pins[zone].value = False
            print("SOLENOID", zone, "OFF")

    def activate(self, zone, seconds):
        """Turn on a solenoid for a given duration."""
        if 0 <= zone < len(self.pins):
            self.turn_on(zone)
            time.sleep(seconds)
            self.turn_off(zone)

    def all_off(self):
        for p in self.pins:
            p.value = False


class PlantDisplay:
    """TFT display for the Feather ESP32-S3 Reverse TFT (240x135)."""
    def __init__(self):
        self.display = board.DISPLAY
        self.group = displayio.Group()

        # Background
        bg = displayio.Bitmap(240, 135, 1)
        bg_pal = displayio.Palette(1)
        bg_pal[0] = COL_BG
        self.group.append(displayio.TileGrid(bg, pixel_shader=bg_pal))

        # Title
        self.title = label.Label(
            terminalio.FONT, text="PlantBit Controller",
            color=COL_HEAD, x=4, y=8)
        self.group.append(self.title)

        # Status line
        self.status = label.Label(
            terminalio.FONT, text="Starting...",
            color=COL_TEXT, x=4, y=122)
        self.group.append(self.status)

        # Zone labels (5 zones, stacked vertically)
        self.zone_labels = []
        for i in range(NUM_ZONES):
            y = 24 + i * 18
            lbl = label.Label(
                terminalio.FONT, text=self._zone_text(i, None, False),
                color=COL_TEXT, x=4, y=y)
            self.group.append(lbl)
            self.zone_labels.append(lbl)

        self.display.root_group = self.group

    def _zone_text(self, zone, moisture, watering):
        name = ZONE_NAMES[zone] if zone < len(ZONE_NAMES) else f"Zone {zone}"
        name = name[:6].ljust(6)
        if moisture is None:
            m_str = " ---%"
        else:
            m_str = f" {moisture:3d}%"
        valve = " [WATER]" if watering else ""
        return f"{name}{m_str}{valve}"

    def update_zone(self, zone, moisture, watering=False):
        if zone < len(self.zone_labels):
            lbl = self.zone_labels[zone]
            lbl.text = self._zone_text(zone, moisture, watering)
            if watering:
                lbl.color = COL_VALVE
            elif moisture is None:
                lbl.color = COL_TEXT
            elif moisture < WATER_THRESHOLD:
                lbl.color = COL_DRY
            elif moisture > 70:
                lbl.color = COL_WET
            else:
                lbl.color = COL_OK

    def set_status(self, text):
        self.status.text = text[:38]


class PlantBitService(Service):
    uuid = SVC_UUID
    moisture = Characteristic(
        MOIST_UUID,
        properties=Characteristic.READ | Characteristic.NOTIFY,
        read_perm=Characteristic.OPEN,
        write_perm=Characteristic.NO_ACCESS,
        max_length=1,
        fixed_length=True,
    )
    pump = Characteristic(
        PUMP_UUID,
        properties=Characteristic.READ | Characteristic.WRITE,
        read_perm=Characteristic.OPEN,
        write_perm=Characteristic.OPEN,
        max_length=1,
        fixed_length=True,
    )
    sleep = Characteristic(
        SLEEP_UUID,
        properties=Characteristic.READ | Characteristic.WRITE,
        read_perm=Characteristic.OPEN,
        write_perm=Characteristic.OPEN,
        max_length=2,
        fixed_length=True,
    )


class PlantBitBleClient:
    def __init__(self):
        self.ble = BLERadio()
        self.connection = None
        self.service = None

    def _find_advertisement(self):
        for adv in self.ble.start_scan(
            ProvideServicesAdvertisement,
            timeout=BLE_SCAN_SECONDS,
        ):
            if SVC_UUID in adv.services:
                if not adv.complete_name or adv.complete_name == PLANTBIT_NAME:
                    return adv
        return None

    def connect(self):
        if self.connection and self.connection.connected:
            return True
        adv = self._find_advertisement()
        self.ble.stop_scan()
        if not adv:
            print("BLE: no PlantBit found")
            return False
        try:
            self.connection = self.ble.connect(adv, timeout=BLE_CONNECT_TIMEOUT)
        except Exception as e:
            print("BLE: connect failed:", e)
            self.connection = None
            return False
        try:
            self.service = self.connection[PlantBitService]
        except Exception as e:
            print("BLE: service missing:", e)
            self.disconnect()
            return False
        print("BLE: connected to PlantBit")
        return True

    def disconnect(self):
        if self.connection:
            try:
                self.connection.disconnect()
            except Exception:
                pass
        self.connection = None
        self.service = None

    def request_pump(self, seconds):
        if seconds <= 0:
            return False
        duration = max(1, min(255, int(seconds)))
        payload = bytes([duration])
        for attempt in range(1, BLE_CONNECT_RETRIES + 2):
            if not self.connect():
                print("BLE: connect attempt", attempt, "failed")
                self.disconnect()
                continue
            try:
                self.service.pump = payload
                print("BLE: pump requested for", duration, "s")
                return True
            except Exception:
                try:
                    self.service.pump.value = payload
                    print("BLE: pump requested for", duration, "s")
                    return True
                except Exception as e:
                    print("BLE: pump write failed (attempt", attempt, "):", e)
                    self.disconnect()
        print("BLE: pump request failed after retries")
        return False


def connect_wifi():
    """Connect to WiFi using settings.toml credentials."""
    ssid = os.getenv("CIRCUITPY_WIFI_SSID")
    password = os.getenv("CIRCUITPY_WIFI_PASSWORD")
    if not ssid:
        print("WiFi: no CIRCUITPY_WIFI_SSID in settings.toml")
        return None
    print("WiFi: connecting to", ssid)
    wifi.radio.connect(ssid, password)
    print("WiFi: connected, IP:", wifi.radio.ipv4_address)
    pool = socketpool.SocketPool(wifi.radio)
    return adafruit_requests.Session(pool, ssl.create_default_context())


def fetch_moisture(requests):
    """Fetch latest moisture value from Adafruit IO."""
    if not requests or not AIO_USER or not AIO_KEY:
        return None
    url = AIO_URL.format(AIO_USER, MOISTURE_FEED)
    try:
        resp = requests.get(url, headers={"X-AIO-Key": AIO_KEY})
        data = resp.json()
        resp.close()
        val = int(float(data.get("value", -1)))
        print("AIO moisture:", val)
        return max(0, min(100, val))
    except Exception as e:
        print("AIO fetch error:", e)
        return None


def main():
    print("PlantBit Controller starting")
    print("Zones:", NUM_ZONES, "| Threshold:", WATER_THRESHOLD,
          "% | Duration:", WATER_DURATION, "s")

    # Display
    disp = PlantDisplay()
    disp.set_status("Connecting WiFi...")

    # WiFi
    requests = connect_wifi()
    if requests:
        disp.set_status("WiFi OK")
    else:
        disp.set_status("WiFi failed - offline mode")

    # Solenoid driver via STEMMA QT I2C
    i2c = board.STEMMA_I2C()
    driver = SolenoidDriver(i2c)
    driver.all_off()

    # BLE client to micro:bit pump service
    ble_client = PlantBitBleClient()

    # Per-zone state
    last_watered = [0] * NUM_ZONES  # monotonic time of last watering
    zone_moisture = [None] * NUM_ZONES

    print("Entering main loop")
    last_poll = -POLL_INTERVAL  # poll immediately on first loop

    while True:
        now = time.monotonic()

        # Poll Adafruit IO for moisture
        if now - last_poll >= POLL_INTERVAL:
            last_poll = now
            moisture = fetch_moisture(requests)
            # Use single value as proxy for all zones (for now)
            for z in range(NUM_ZONES):
                zone_moisture[z] = moisture
                disp.update_zone(z, moisture)
            if moisture is not None:
                disp.set_status(f"Moisture: {moisture}% @ {int(now)}s")
            else:
                disp.set_status("No data from AIO")

        # Check each zone for watering
        for z in range(NUM_ZONES):
            m = zone_moisture[z]
            if m is None:
                continue
            since_last = now - last_watered[z]
            if m < WATER_THRESHOLD and since_last >= WATER_COOLDOWN:
                seconds = ZONE_WATER_SECONDS[z]
                disp.update_zone(z, m, watering=True)
                disp.set_status(f"Watering {ZONE_NAMES[z]}...")
                driver.turn_on(z)
                pump_ok = ble_client.request_pump(seconds)
                if not pump_ok and SKIP_WATER_IF_PUMP_FAIL:
                    disp.set_status(f"Pump failed for {ZONE_NAMES[z]}")
                    driver.turn_off(z)
                    ble_client.disconnect()
                    continue
                time.sleep(seconds)
                driver.turn_off(z)
                ble_client.disconnect()
                last_watered[z] = time.monotonic()
                disp.update_zone(z, m, watering=False)
                disp.set_status(f"Watered {ZONE_NAMES[z]}")

        time.sleep(1)

main()
