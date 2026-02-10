# Plant Watering System - micro:bit v2 + Bonsai Buckaroo + BLE
# P1 = Soil moisture (analog), P2 = Pump MOSFET (digital)
#
# BLE Testing with nRF Connect:
#   Device name: "PlantBit"
#   Service:  12340001-1234-5678-1234-56789abcdef0
#   Moisture: 12340002-1234-5678-1234-56789abcdef0  READ/NOTIFY  (1 byte, 0-100%)
#   Pump:     12340003-1234-5678-1234-56789abcdef0  READ/WRITE   (1 byte = seconds, e.g. 0x03 = 3s)
#   Sleep:    12340004-1234-5678-1234-56789abcdef0  READ/WRITE   (uint16 LE, seconds)
#             nRF Connect examples: 3C00 = 60s, 2C01 = 300s (5 min)
#
# Buttons: A = re-read moisture (wakes from sleep), B = activate pump (wakes from sleep)
#
# LED 5x5 display: 5-column graph (1 col per day, newest=left)
#   Top 1-3 LEDs = daily high moisture, Bottom 1-3 LEDs = daily low
#   Status icons flash for 1s on pump/BLE/button events
#
# Sleep modes (set SLEEP_MODE in settings.toml):
#   "light" (default) - light sleep between cycles, buttons wake instantly,
#                        BLE/USB stay alive, program continues from where it left off
#   "deep"            - deep sleep between cycles, lowest power, buttons wake but
#                        program restarts from scratch (BLE reconnect needed)
#   "none"            - no sleep, just time.sleep() polling loop, useful for debugging
#
# settings.toml options:
#   SLEEP_MODE = "light"   # "light", "deep", or "none"
#   SLEEP_SECONDS = 60     # seconds between wake cycles (default 60)

import time
import board
import analogio
import digitalio
import alarm
import _bleio

# --- Config (overridable via settings.toml) ---
try:
    import os
    SLEEP_SECONDS = int(os.getenv("SLEEP_SECONDS", "60"))
    SLEEP_MODE = os.getenv("SLEEP_MODE", "light")
except (NotImplementedError, TypeError):
    SLEEP_SECONDS = 60
    SLEEP_MODE = "light"
print("config: sleep", SLEEP_SECONDS, "s, mode", SLEEP_MODE)
ACTIVE_SECONDS = 15
EXTEND_SECONDS = 5
BLE_EXTEND_SECONDS = 30
PUMP_SECONDS = 0.5
PUMP_COOLDOWN = 10  # skip moisture reads this many seconds after pump
N_SAMPLES = 10
WAKES_PER_DAY = (24 * 3600) // SLEEP_SECONDS  # 1440 at 60s

# --- BLE UUIDs ---
SVC_UUID = _bleio.UUID("12340001-1234-5678-1234-56789abcdef0")
MOIST_UUID = _bleio.UUID("12340002-1234-5678-1234-56789abcdef0")
PUMP_UUID = _bleio.UUID("12340003-1234-5678-1234-56789abcdef0")
SLEEP_UUID = _bleio.UUID("12340004-1234-5678-1234-56789abcdef0")

# --- Icons (5 rows, MSB=leftmost col) ---
ICON_PUMP = [0b00100, 0b01110, 0b11111, 0b11111, 0b01110]  # water drop
ICON_BLE = [0b00100, 0b01010, 0b10101, 0b01010, 0b00100]   # diamond/signal
ICON_READ = [0b01110, 0b10001, 0b10001, 0b10001, 0b01110]  # circle = sensor
ICON_SMILE = [0b00000, 0b01010, 0b00000, 0b10001, 0b01110] # smiley face

# --- Sleep memory layout ---
# [0]    : init marker (0xAA)
# [1-10] : 5 days x 2 bytes (high, low), day0=oldest day4=today
# [11-12]: wake counter (uint16 LE)
# [13]   : running high for current period
# [14]   : running low for current period
INIT_MARKER = 0xAA

# Track last pump time (not persisted, resets each boot)
last_pump_time = 0


# ============================================================
# LED Matrix driver (5x5, active-high rows, active-low cols)
# ============================================================
class LEDMatrix:
    def __init__(self):
        self.rows = []
        self.cols = []
        for p in (board.ROW1, board.ROW2, board.ROW3, board.ROW4, board.ROW5):
            d = digitalio.DigitalInOut(p)
            d.direction = digitalio.Direction.OUTPUT
            d.value = False
            self.rows.append(d)
        for p in (board.COL1, board.COL2, board.COL3, board.COL4, board.COL5):
            d = digitalio.DigitalInOut(p)
            d.direction = digitalio.Direction.OUTPUT
            d.value = True  # HIGH = off (active low)
            self.cols.append(d)
        self.buf = bytearray(5)  # one byte per row, bits 4..0 = cols L-to-R

    def clear(self):
        for i in range(5):
            self.buf[i] = 0

    def pixel(self, r, c, on=True):
        if on:
            self.buf[r] |= (1 << (4 - c))
        else:
            self.buf[r] &= ~(1 << (4 - c))

    def set_icon(self, icon):
        for r in range(5):
            self.buf[r] = icon[r]

    def refresh(self):
        """Call repeatedly to multiplex. ~5ms per full scan."""
        for r in range(5):
            row_val = self.buf[r]
            for c in range(5):
                self.cols[c].value = not bool(row_val & (1 << (4 - c)))
            self.rows[r].value = True
            time.sleep(0.001)
            self.rows[r].value = False

    def off(self):
        for r in self.rows:
            r.value = False
        for c in self.cols:
            c.value = True

    def deinit(self):
        self.off()
        for d in self.rows + self.cols:
            d.deinit()


# ============================================================
# History (persisted in alarm.sleep_memory across deep sleeps)
# ============================================================
def hist_init():
    if alarm.sleep_memory[0] != INIT_MARKER:
        for i in range(15):
            alarm.sleep_memory[i] = 0
        alarm.sleep_memory[0] = INIT_MARKER

def hist_get():
    """Return [(high,low)] for 5 days, index 0=oldest 4=today."""
    return [(alarm.sleep_memory[1 + d * 2],
             alarm.sleep_memory[2 + d * 2]) for d in range(5)]

def _wake_count():
    return alarm.sleep_memory[11] | (alarm.sleep_memory[12] << 8)

def _set_wake_count(n):
    alarm.sleep_memory[11] = n & 0xFF
    alarm.sleep_memory[12] = (n >> 8) & 0xFF

def hist_update(moisture):
    cnt = _wake_count()
    rh = alarm.sleep_memory[13]
    rl = alarm.sleep_memory[14]
    if cnt == 0:
        rh = rl = moisture
    else:
        if moisture > rh:
            rh = moisture
        if moisture < rl:
            rl = moisture
    alarm.sleep_memory[13] = rh
    alarm.sleep_memory[14] = rl
    # always write running values into day-4 slot
    alarm.sleep_memory[9] = rh
    alarm.sleep_memory[10] = rl
    cnt += 1
    if cnt >= WAKES_PER_DAY:
        # shift days left
        for d in range(4):
            alarm.sleep_memory[1 + d * 2] = alarm.sleep_memory[3 + d * 2]
            alarm.sleep_memory[2 + d * 2] = alarm.sleep_memory[4 + d * 2]
        alarm.sleep_memory[13] = 0
        alarm.sleep_memory[14] = 0
        cnt = 0
    _set_wake_count(cnt)


# ============================================================
# Display helpers
# ============================================================
def pct_to_leds(pct):
    """Map 0-100% to 0-3 LEDs."""
    if pct <= 0:
        return 0
    return min(3, (pct + 32) // 33)

def draw_graph(led, history):
    """Draw 5-day moisture graph. Col 0 = today (newest), col 4 = oldest."""
    led.clear()
    for i in range(5):
        h, l = history[4 - i]  # reverse: newest on left
        for r in range(pct_to_leds(h)):
            led.pixel(r, i)            # high from top
        for r in range(pct_to_leds(l)):
            led.pixel(4 - r, i)        # low from bottom

def flash_icon(led, icon, duration=1.0):
    """Show icon for duration, refreshing display."""
    led.set_icon(icon)
    end = time.monotonic() + duration
    while time.monotonic() < end:
        led.refresh()


# ============================================================
# Hardware
# ============================================================
def read_moisture():
    global last_pump_time
    since_pump = time.monotonic() - last_pump_time
    if since_pump < PUMP_COOLDOWN:
        print("moisture: skipped (pump", int(PUMP_COOLDOWN - since_pump), "s ago)")
        return None
    pin = analogio.AnalogIn(board.P1)
    total = 0
    for _ in range(N_SAMPLES):
        total += pin.value
        time.sleep(0.01)
    pin.deinit()
    avg = total // N_SAMPLES
    # Higher resistance (dry) = lower analog value on voltage divider
    # Adjust inversion if your wiring differs
    pct = 100 - (avg * 100 // 65535)
    return max(0, min(100, pct))

def pump_on(seconds=PUMP_SECONDS):
    global last_pump_time
    print("PUMP ON for", seconds, "s")
    p = digitalio.DigitalInOut(board.P2)
    p.direction = digitalio.Direction.OUTPUT
    p.value = True
    time.sleep(seconds)
    p.value = False
    p.deinit()
    last_pump_time = time.monotonic()
    print("PUMP OFF")


# ============================================================
# BLE
# ============================================================
def ble_setup():
    svc = _bleio.Service(SVC_UUID)
    mc = _bleio.Characteristic.add_to_service(
        svc, MOIST_UUID,
        properties=_bleio.Characteristic.READ | _bleio.Characteristic.NOTIFY,
        read_perm=_bleio.Attribute.OPEN,
        write_perm=_bleio.Attribute.NO_ACCESS,
        max_length=1, fixed_length=True,
        initial_value=bytes([0]))
    pc = _bleio.Characteristic.add_to_service(
        svc, PUMP_UUID,
        properties=_bleio.Characteristic.READ | _bleio.Characteristic.WRITE,
        read_perm=_bleio.Attribute.OPEN,
        write_perm=_bleio.Attribute.OPEN,
        max_length=1, fixed_length=True,
        initial_value=bytes([0]))
    sc = _bleio.Characteristic.add_to_service(
        svc, SLEEP_UUID,
        properties=_bleio.Characteristic.READ | _bleio.Characteristic.WRITE,
        read_perm=_bleio.Attribute.OPEN,
        write_perm=_bleio.Attribute.OPEN,
        max_length=2, fixed_length=True,
        initial_value=SLEEP_SECONDS.to_bytes(2, 'little'))
    _bleio.adapter.name = "PlantBit"
    return mc, pc, sc

def ble_adv_data():
    ad = bytearray()
    ad.extend(b'\x02\x01\x06')                    # flags
    name = b"PlantBit"
    ad.extend(bytes([len(name) + 1, 0x09]))        # complete local name
    ad.extend(name)
    ub = SVC_UUID.uuid128
    ad.extend(bytes([len(ub) + 1, 0x07]))          # 128-bit svc UUID list
    ad.extend(ub)
    return bytes(ad)

def ble_start_adv(adv):
    try:
        _bleio.adapter.start_advertising(
            data=adv, connectable=True, interval=0.1, timeout=0)
        print("BLE advertising started")
    except Exception as e:
        print("adv err:", e)

def ble_stop():
    try:
        _bleio.adapter.stop_advertising()
    except:
        pass
    for c in _bleio.adapter.connections:
        try:
            c.disconnect()
        except:
            pass


# ============================================================
# Main
# ============================================================
def do_read(mc, led):
    """Read moisture, update history+char+display. Returns (moisture, history)."""
    m = read_moisture()
    if m is None:
        return None, hist_get()
    print("moisture:", m, "% | wake:", _wake_count(), "/", WAKES_PER_DAY,
          "| hi:", alarm.sleep_memory[13], "lo:", alarm.sleep_memory[14])
    hist_update(m)
    history = hist_get()
    mc.value = bytes([m])
    draw_graph(led, history)
    return m, history


def wake_cycle(led, mc, pc, sc, btn_a, btn_b):
    """One wake cycle: read sensor, advertise, handle events."""
    global SLEEP_SECONDS, WAKES_PER_DAY
    print("\n--- wake ---")
    moisture, history = do_read(mc, led)

    adv = ble_adv_data()
    ble_start_adv(adv)

    was_connected = False
    deadline = time.monotonic() + ACTIVE_SECONDS

    while time.monotonic() < deadline:
        led.refresh()

        # --- BLE ---
        if _bleio.adapter.connected:
            if not was_connected:
                was_connected = True
                print("BLE: connected")
                flash_icon(led, ICON_BLE)
                draw_graph(led, history)
                deadline = max(deadline, time.monotonic() + BLE_EXTEND_SECONDS)
            pv = pc.value
            if pv and pv[0] > 0:
                duration = pv[0]
                print("BLE: pump requested for", duration, "s")
                flash_icon(led, ICON_PUMP)
                pump_on(duration)
                pc.value = bytes([0])
                moisture, history = do_read(mc, led)
                deadline = max(deadline, time.monotonic() + BLE_EXTEND_SECONDS)
            # Check for sleep interval change
            sv = sc.value
            if sv and len(sv) >= 2:
                new_sleep = sv[0] | (sv[1] << 8)
                if 10 <= new_sleep <= 3600 and new_sleep != SLEEP_SECONDS:
                    SLEEP_SECONDS = new_sleep
                    WAKES_PER_DAY = (24 * 3600) // SLEEP_SECONDS
                    print("BLE: sleep set to", SLEEP_SECONDS, "s, wakes/day:", WAKES_PER_DAY)
            # Any active connection keeps extending the deadline
            deadline = max(deadline, time.monotonic() + BLE_EXTEND_SECONDS)
        else:
            if was_connected:
                was_connected = False
                print("BLE: disconnected, +30s grace")
                deadline = max(deadline, time.monotonic() + BLE_EXTEND_SECONDS)
                try:
                    ble_start_adv(adv)
                except:
                    pass

        # --- Buttons ---
        if not btn_a.value:
            print("BTN A: reading sensor")
            flash_icon(led, ICON_READ)
            moisture, history = do_read(mc, led)
            deadline = max(deadline, time.monotonic() + EXTEND_SECONDS)
            while not btn_a.value:
                led.refresh()

        if not btn_b.value:
            print("BTN B: activating pump")
            flash_icon(led, ICON_PUMP)
            pump_on()
            moisture, history = do_read(mc, led)
            deadline = max(deadline, time.monotonic() + EXTEND_SECONDS)
            while not btn_b.value:
                led.refresh()

    ble_stop()
    print("--- active window closed ---")


# alarm module present but TimeAlarm/PinAlarm not yet implemented on micro:bit v2.
# Uncomment when alarm support is added to the nRF52833 CircuitPython port.
#
# def make_sleep_alarms():
#     """Create time + button pin alarms for sleeping."""
#     ta = alarm.time.TimeAlarm(monotonic_time=time.monotonic() + SLEEP_SECONDS)
#     pa = alarm.pin.PinAlarm(pin=board.BTN_A, value=False, pull=True)
#     pb = alarm.pin.PinAlarm(pin=board.BTN_B, value=False, pull=True)
#     return ta, pa, pb
#
# def deep_sleep():
#     """Enter deep sleep until timer or button press. Restarts program on wake."""
#     print("deep sleeping", SLEEP_SECONDS, "s")
#     alarm.exit_and_deep_sleep_until_alarms(*make_sleep_alarms())


def init_buttons():
    btn_a = digitalio.DigitalInOut(board.BTN_A)
    btn_a.direction = digitalio.Direction.INPUT
    btn_a.pull = digitalio.Pull.UP
    btn_b = digitalio.DigitalInOut(board.BTN_B)
    btn_b.direction = digitalio.Direction.INPUT
    btn_b.pull = digitalio.Pull.UP
    return btn_a, btn_b


def main():
    hist_init()
    print("PlantBit starting | sleep:", SLEEP_SECONDS,
          "s | mode:", SLEEP_MODE, "| wakes/day:", WAKES_PER_DAY)

    led = LEDMatrix()
    flash_icon(led, ICON_SMILE, 1.5)
    mc, pc, sc = ble_setup()

    while True:
        btn_a, btn_b = init_buttons()
        wake_cycle(led, mc, pc, sc, btn_a, btn_b)
        led.off()

        # alarm sleep not yet supported on micro:bit v2 - poll buttons instead
        # if SLEEP_MODE == "deep":
        #     btn_a.deinit(); btn_b.deinit()
        #     deep_sleep()
        # elif SLEEP_MODE == "light":
        #     btn_a.deinit(); btn_b.deinit()
        #     woke = alarm.light_sleep_until_alarms(*make_sleep_alarms())
        #     print("woke by:", woke)
        print("sleeping", SLEEP_SECONDS, "s (buttons wake)")
        end = time.monotonic() + SLEEP_SECONDS
        while time.monotonic() < end:
            if not btn_a.value or not btn_b.value:
                print("woke by: button")
                break
            time.sleep(0.1)
        btn_a.deinit()
        btn_b.deinit()

main()
