#!/usr/bin/env python3
"""Yahboom CUBE OLED (SSD1306 128x32, monochrome) monitor. Refresh every 5 s.
  +---+-----+------+------+
  | A | B   | C    | D    |  <-- Top Row (A,B,C,D): White bg, black text
  +---+-----+------+------+
  | E | F   | G    | H    |  <-- Bottom Row (E,F,G,H): Black bg, white text
  +---+-----+------+------+
"""
import os
import re
import subprocess
import sys
import threading
import time

import psutil
from PIL import Image, ImageDraw, ImageFont

I2C_BUS = 7
I2C_ADDR = 0x3C
WIDTH, HEIGHT = 128, 32
REFRESH = 5
GIB = 1024 ** 3

# RGB light bar of the CUBE (same I2C bus as the OLED, address 0x0E), driven directly.
# (min Load index, name, (r, g, b)).  The whole bar shows one solid colour.
LEVELS = [
    (0,   "blue",   (0, 0, 255)),     # 0-69:    below the working zone
    (70,  "green",  (0, 255, 0)),     # 70-129:  around 100 = perfect use
    (130, "yellow", (255, 170, 0)),   # 130-299: high load
    (300, "red",    (255, 0, 0)),     # 300+:    extreme load
]
LEVEL_HYSTERESIS = 8   # must fall this far below a threshold to step down
RGB_WRITE = "bytes"    # how a bead colour is written: "bytes" or "block"
                       # (rgb_raw_test.py tells you which one your board accepts)

DIGIT_SIZES = [(9, 14, 2), (8, 13, 2), (7, 12, 2), (6, 10, 1)]
LABEL_SIZES = [10, 9, 8]
GAP = 1
PAD = 1

SEGMENTS = {
    "0": "abcdef", "1": "bc", "2": "abdeg", "3": "abcdg", "4": "bcfg",
    "5": "acdfg", "6": "acdefg", "7": "abc", "8": "abcdefg", "9": "abcdfg",
    "-": "g", " ": "",
}


def load_font(size):
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def procs_running():
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("procs_running"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 1


class Tegrastats:
    GPU_RE = re.compile(r"GR3D_FREQ (\d+)%")
    EMC_RE = re.compile(r"EMC_FREQ (\d+)%")

    def __init__(self):
        self.gpu = 0.0
        self.emc = 0.0
        self.ok = False
        self.has_emc = False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                p = subprocess.Popen(["tegrastats", "--interval", "500"],
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True)
                for line in p.stdout:
                    g, e = self.GPU_RE.search(line), self.EMC_RE.search(line)
                    if g:
                        self.gpu = float(g.group(1))
                    if e:
                        self.emc = float(e.group(1))
                    self.ok = bool(g or e)
                    if e:
                        self.has_emc = True
                p.wait()
            except OSError:
                self.ok = False
            time.sleep(5)


TEGRA = Tegrastats()
PAGE = os.sysconf("SC_PAGE_SIZE")


def swap_pages():
    total = 0
    try:
        with open("/proc/vmstat") as f:
            for line in f:
                if line.startswith(("pswpin ", "pswpout ")):
                    total += int(line.split()[1])
    except (OSError, ValueError):
        pass
    return total


def psi_stall(resource):
    try:
        with open(f"/proc/pressure/{resource}") as f:
            for line in f:
                if line.startswith("some"):
                    return float(line.split("avg10=")[1].split()[0])
    except (OSError, IndexError, ValueError):
        pass
    return 0.0


def measure_load(duration):
    cores = os.cpu_count() or 1
    psutil.cpu_percent(None)
    psutil.cpu_times_percent(None)
    swap0 = swap_pages()
    utils, queues, gpus, emcs = [], [], [], []
    t0 = time.time()
    end = t0 + duration
    while time.time() < end:
        time.sleep(0.5)
        utils.append(psutil.cpu_percent(None))
        queues.append(max(procs_running() - 1, 0))
        gpus.append(TEGRA.gpu)
        emcs.append(TEGRA.emc)
    n = len(utils)
    if os.path.exists("/proc/pressure/memory"):
        stall = 10 * max(psi_stall("memory"), psi_stall("io"))
    else:
        iowait = getattr(psutil.cpu_times_percent(None), "iowait", 0.0)
        swap_mbps = ((swap_pages() - swap0) * PAGE
                     / (1024 * 1024) / max(time.time() - t0, 0.1))
        stall = max(10 * iowait, 4 * swap_mbps)
    parts = {
        "cpu": max(sum(utils) / n, sum(queues) / n / cores * 100),
        "gpu": sum(gpus) / n,
        "mem": sum(emcs) / n,
        "stall": stall,
    }
    return max(parts.values()), parts


def format_load(index):
    v = round(index)
    if v > 999:
        return "999+"
    return str(max(v, 1))


def get_docker_status():
    """Returns count string, appending '*' if any container is currently restarting."""
    try:
        out_running = subprocess.run(["docker", "ps", "-q"], capture_output=True,
                                     text=True, timeout=3).stdout
        running_list = [l for l in out_running.splitlines() if l.strip()]
        running_count = len(running_list)

        out_all = subprocess.run(["docker", "ps", "-a", "--format", "{{.Status}}"], 
                                   capture_output=True, text=True, timeout=3).stdout
        
        restarting = False
        for line in out_all.splitlines():
            if "restarting" in line.lower():
                restarting = True
                break

        count_str = str(running_count)
        if restarting:
            count_str += "*"

        return count_str
    except Exception:
        return "?"


def char_width(ch, size):
    w, h, t = size
    if ch == ".":
        return t
    if ch == "/":
        return w - 2
    return w


def digit(d, x, y, size, ch, fg):
    w, h, t = size
    if ch == ".":
        d.rectangle((x, y + h - t, x + t - 1, y + h - 1), fill=fg)
        return
    if ch == "/":
        d.line((x, y + h - 1, x + w - 3, y), fill=fg, width=1)
        d.line((x + 1, y + h - 1, x + w - 2, y), fill=fg, width=1)
        return
    ym = y + (h - t) // 2
    if ch == "+":
        arm = w // 2 - 1
        vx = x + (w - t) // 2
        d.rectangle((x + 1, ym, x + w - 2, ym + t - 1), fill=fg)
        d.rectangle((vx, ym - arm, vx + t - 1, ym + t - 1 + arm), fill=fg)
        return
    rects = {
        "a": (x + 1, y, x + w - 2, y + t - 1),
        "g": (x + 1, ym, x + w - 2, ym + t - 1),
        "d": (x + 1, y + h - t, x + w - 2, y + h - 1),
        "f": (x, y + 1, x + t - 1, ym + t - 2),
        "b": (x + w - t, y + 1, x + w - 1, ym + t - 2),
        "e": (x, ym + 1, x + t - 1, y + h - 2),
        "c": (x + w - t, ym + 1, x + w - 1, y + h - 2),
    }
    for s in SEGMENTS.get(ch, ""):
        d.rectangle(rects[s], fill=fg)


def num_width_custom(text, size, font):
    w = 0
    for i, c in enumerate(text):
        if c in SEGMENTS:
            w += char_width(c, size)
        else:
            l, _, r, _ = font.getbbox(c)
            w += (r - l)
        if i < len(text) - 1:
            w += GAP
    return w


def text_width(d, font, text):
    l, _, r, _ = d.textbbox((0, 0), text, font=font)
    return r - l


def content_width(d, font, size, cell):
    kind, text = cell["kind"], cell["text"]
    if kind == "t":
        return text_width(d, font, text)
    return num_width_custom(text.rjust(cell.get("minlen", 0), "8"), size, font)


def choose_layout(d, columns):
    for size in DIGIT_SIZES:
        for fs in LABEL_SIZES:
            font = load_font(fs)
            widths = [max(content_width(d, font, size, c) for c in col) + 2 * PAD
                      for col in columns]
            if sum(widths) <= WIDTH:
                extra = WIDTH - sum(widths)
                widths[0] += extra // 4
                widths[1] += extra // 4
                widths[2] += extra // 4
                widths[3] += extra - 3 * (extra // 4)
                return size, font, widths
    size, font = DIGIT_SIZES[-1], load_font(LABEL_SIZES[-1])
    widths = [max(content_width(d, font, size, c) for c in col) + 2 * PAD
              for col in columns]
    widths[0] -= sum(widths) - WIDTH
    return size, font, widths


def draw_cell(d, box, cell, size, font, inverted):
    x0, y0, x1, y1 = box
    cw, ch = x1 - x0 + 1, y1 - y0 + 1
    fg = 0 if inverted else 255
    d.rectangle(box, fill=255 if inverted else 0, outline=255)

    if cell["kind"] == "t":
        l, t, r, b = d.textbbox((0, 0), cell["text"], font=font)
        x = x0 + (cw - (r - l)) // 2 - l
        y = y0 + (ch - (b - t)) // 2 - t
        d.text((x, y), cell["text"], font=font, fill=fg)
    else:
        text = cell["text"]
        total_w = num_width_custom(text, size, font)
        x = x0 + (cw - total_w) // 2
        y = y0 + (ch - size[1]) // 2
        for c in text:
            if c in SEGMENTS:
                digit(d, x, y, size, c, fg)
                x += char_width(c, size) + GAP
            else:
                l, t, r, b = font.getbbox(c)
                char_h = b - t
                char_y = y0 + (ch - char_h) // 2 - t
                d.text((x, char_y), c, font=font, fill=fg)
                x += (r - l) + GAP


def render(ram_val, swap_val, dock_val, load_val):
    N = lambda t, m=0: {"kind": "n", "text": str(t), "minlen": m}
    T = lambda t: {"kind": "t", "text": t}
    
    columns = [
        [T("Mem"), N(ram_val, 2)],   # A / E
        [T("Swap"), N(swap_val, 2)], # B / F
        [T("Dock"), N(dock_val, 1)], # C / G
        [T("Load"), N(load_val, 3)], # D / H
    ]

    image = Image.new("1", (WIDTH, HEIGHT), 0)
    d = ImageDraw.Draw(image)
    size, font, widths = choose_layout(d, columns)

    mid = HEIGHT // 2
    x = 0
    for col, w in zip(columns, widths):
        # A, B, C, D (Top row): White background, black text -> inverted = False
        draw_cell(d, (x, 0, x + w - 1, mid - 1), col[0], size, font, inverted=False)
        
        # E, F, G, H (Bottom row): Black background, white text -> inverted = True
        draw_cell(d, (x, mid, x + w - 1, HEIGHT - 1), col[1], size, font, inverted=True)
        
        d.line((x, mid - 1, x + w - 1, mid - 1), fill=255)
        x += w
    return image


def pick_level(index, prev):
    """Index into LEVELS for this Load value, with hysteresis so the LEDs
    don't flicker between two colours around a threshold."""
    new = max(i for i, lv in enumerate(LEVELS) if index >= lv[0])
    if prev is not None and new < prev and index > LEVELS[prev][0] - LEVEL_HYSTERESIS:
        return prev
    return new


class CubeRGB:
    """Talks to the CUBE expansion board directly over I2C (no CubeNanoLib).
    Register map from Yahboom's "I2C Protocol" table, device address 0x0E:

        0x00 bead 0-13 (255 = all)   0x04 effect 0-6 (0 = off)
        0x01 red 0-255               0x05 speed 1-3
        0x02 green 0-255             0x06 effect colour 0-6
        0x03 blue 0-255              0x07 write 0 = lights off
        (0x08 is the fan: deliberately never touched here)
    """
    ADDR = 0x0E
    DELAY = 0.002        # same pause Yahboom's library uses between writes

    def __init__(self, bus, mode="bytes"):
        try:
            import smbus                 # apt: python3-smbus
        except ImportError:
            import smbus2 as smbus       # pip: smbus2
        self.dev = smbus.SMBus(bus)
        self.mode = mode

    def _write(self, reg, value):
        self.dev.write_byte_data(self.ADDR, reg, int(value) & 0xFF)
        time.sleep(self.DELAY)

    def effect(self, e):                 # 0 off, 1 breathing, 3 rainbow, 5 flowing...
        self._write(0x04, e)

    def speed(self, s):                  # 1 low, 2 medium, 3 high
        self._write(0x05, s)

    def effect_color(self, c):           # 0 red 1 green 2 blue 3 yellow 4 purple 5 cyan 6 white
        self._write(0x06, c)

    def lights_off(self):
        self._write(0x07, 0)

    def single(self, index, r, g, b):    # index 0-13, or 255 = every bead
        if self.mode == "block":         # one 4-byte write starting at 0x00
            self.dev.write_i2c_block_data(self.ADDR, 0x00, [index, r, g, b])
            time.sleep(self.DELAY)
        else:                            # register by register
            self._write(0x00, index)
            self._write(0x01, r)
            self._write(0x02, g)
            self._write(0x03, b)


class LedBar:
    """Whole light bar in one solid colour: blue / green / yellow / red by level.
    Stops the default colour-cycling effect first."""

    def __init__(self, bus):
        self.bot, self.level, self.last, self.error = None, None, 0.0, None
        try:
            self.bot = CubeRGB(bus, RGB_WRITE)
        except Exception as e:           # no smbus module / bus wrong
            self.error = repr(e)

    def show(self, level):
        if not self.bot:
            return
        if level == self.level and time.time() - self.last < 60:
            return                       # unchanged; re-send once a minute
        rgb = LEVELS[level][2]
        try:
            self.bot.effect(0)           # off = stop the cycling effect
            self.bot.single(255, *rgb)   # 255 = every bead
            self.level, self.last = level, time.time()
            self.error = None
        except Exception as e:
            self.error = repr(e)


def main():
    from luma.core.interface.serial import i2c
    from luma.oled.device import ssd1306

    device = None
    while device is None:
        try:
            device = ssd1306(i2c(port=I2C_BUS, address=I2C_ADDR),
                             width=WIDTH, height=HEIGHT)
        except Exception:
            time.sleep(2)

    leds = LedBar(I2C_BUS)
    level = None
    if "--debug" in sys.argv:
        print(f"i2c bus={I2C_BUS}  rgb={'ok' if leds.bot else 'OFF ' + str(leds.error)}",
              flush=True)

    first = True
    while True:
        index, parts = measure_load(1 if first else REFRESH)
        load_str = format_load(index)
        level = pick_level(index, level)
        leds.show(level)

        first = False
        if "--debug" in sys.argv:
            shown = {k: f"{v:.0f}" for k, v in parts.items()}
            if not TEGRA.has_emc:
                shown["mem"] = "n/a"
            print(f"load={load_str} led={LEVELS[level][1]}  "
                  + "  ".join(f"{k}={v}" for k, v in shown.items()), flush=True)

        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        
        ram_pct = f"{vm.percent:.0f}"
        swap_pct = f"{sw.percent:.0f}"
        dock_str = get_docker_status()

        device.display(render(ram_pct, swap_pct, dock_str, load_str))


if __name__ == "__main__":
    main()
