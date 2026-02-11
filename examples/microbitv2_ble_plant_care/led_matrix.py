import time
import board
import digitalio


ICON_S = [0b01110, 0b10000, 0b01110, 0b00001, 0b11110]


class LEDMatrix:
    """5x5 LED matrix driver (active-high rows, active-low cols)."""
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
