import time
import microcontroller


def show_safe_mode():
    try:
        from led_matrix import LEDMatrix, ICON_S
        led = LEDMatrix()
        try:
            led.set_icon(ICON_S)
            end = time.monotonic() + 1.5
            while time.monotonic() < end:
                led.refresh()
        finally:
            led.deinit()
        return
    except Exception:
        try:
            from microbit import display, Image
            display.show(Image("09990:90009:09990:00009:09990"))
            time.sleep(1.5)
            display.clear()
        except Exception:
            pass


show_safe_mode()
microcontroller.reset()
