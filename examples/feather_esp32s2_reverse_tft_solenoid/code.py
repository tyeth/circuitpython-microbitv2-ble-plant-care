# Multi-Plant Watering Controller - Feather ESP32-S2 Reverse TFT
# Uses Adafruit I2C 8-Channel Solenoid Driver (MCP23017, product #6318)
# Solenoids 0-4 = 5 plant zones, 5-7 = spare/future
#
# Reads moisture from Adafruit IO (published by micro:bit BLE gateway).
# For now, one moisture value is used as proxy for all 5 zones.
# Eventually each zone gets its own sensor over I2C/WiFi/BLE.
#
# Requires libraries: adafruit_mcp230xx, adafruit_requests,
#   adafruit_display_text, adafruit_bitmap_font (optional)
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
from adafruit_display_text import label
from adafruit_mcp230xx.mcp23017 import MCP23017

# --- Config ---
NUM_ZONES = 5
POLL_INTERVAL = 60          # seconds between Adafruit IO polls
WATER_THRESHOLD = 30        # moisture % below which to water
WATER_DURATION = 3          # seconds per solenoid activation
WATER_COOLDOWN = 300        # minimum seconds between waterings per zone
SOLENOID_PINS = (0, 1, 2, 3, 4)  # MCP23017 pin numbers for zones 0-4

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

    def activate(self, zone, seconds):
        """Turn on a solenoid for a given duration."""
        if 0 <= zone < len(self.pins):
            print("SOLENOID", zone, "(", ZONE_NAMES[zone], ") ON for", seconds, "s")
            self.pins[zone].value = True
            time.sleep(seconds)
            self.pins[zone].value = False
            print("SOLENOID", zone, "OFF")

    def all_off(self):
        for p in self.pins:
            p.value = False


class PlantDisplay:
    """TFT display for the Feather ESP32-S2 Reverse TFT (240x135)."""
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
                disp.update_zone(z, m, watering=True)
                disp.set_status(f"Watering {ZONE_NAMES[z]}...")
                driver.activate(z, WATER_DURATION)
                last_watered[z] = time.monotonic()
                disp.update_zone(z, m, watering=False)
                disp.set_status(f"Watered {ZONE_NAMES[z]}")

        time.sleep(1)

main()
