# oled_stats.py — status display and load light for the Yahboom CUBE NANO

A small Python service for a **Jetson Orin Nano (Super, 8 GB)** inside a **Yahboom CUBE NANO** case.
It does two things every 5 seconds:

1. Draws memory, swap, Docker and **Load** on the case's 128×32 OLED.
2. Colours the whole RGB light bar (blue / green / yellow / red) according to the same Load value, so you can tell at a glance, from across the room, whether the machine is busy.

Developed on Ubuntu 24.04, kernel `6.8.12-1021-tegra`. 

---

## What the display shows

<img width="4032" height="3024" alt="image" src="https://github.com/user-attachments/assets/f4bf1043-3216-4bcd-8c82-a81b39d77a36" />



```
+---------+---------+---------+---------+
|  Mem    |  Swap   |  Dock   |  Load   |   labels
+---------+---------+---------+---------+
|   42    |    1    |    3    |   100   |   values (7-segment digits)
+---------+---------+---------+---------+
```

| Cell | Value | Notes |
|---|---|---|
| **Mem** | RAM used, % | `psutil.virtual_memory().percent` |
| **Swap** | Swap used, % | `psutil.swap_memory().percent` |
| **Dock** | Running Docker containers | A `*` is appended if any container is in the *restarting* state; `?` if Docker can't be queried |
| **Load** | Occupancy index, 1–999+ | See [The Load index](#the-load-index) |

Digits are drawn as Casio-style 7-segment glyphs; the layout picks the largest digit and label sizes that fit 128 px.

---

## Requirements

Hardware: Jetson Orin Nano + Yahboom CUBE NANO (OLED SSD1306 at `0x3C`, RGB/fan controller at `0x0E`, both on I2C bus 7).

```bash
sudo apt install -y python3-pip python3-psutil python3-pil python3-smbus i2c-tools fonts-dejavu-core
sudo pip3 install --break-system-packages luma.oled
```

`tegrastats` (part of JetPack) must be on the `PATH` for the GPU term of the Load index. The script must run as **root** (I2C, `tegrastats`, Docker).

> No Yahboom library (`CubeNanoLib`) is needed. The RGB bar is driven directly over I2C.

Check that both devices are visible on the bus:

```bash
sudo i2cdetect -y -r 7      # expect 0e and 3c
```

---

## Running it

```bash
sudo python3 oled_stats.py            # normal
sudo python3 oled_stats.py --debug    # prints the Load terms and LED colour every 5 s
```

Example `--debug` line:

```
load=88 led=green  cpu=12  gpu=88  mem=n/a  stall=0
```

### At boot (systemd)

`/etc/systemd/system/oled-stats.service`:

```ini
[Unit]
Description=OLED system stats (Yahboom CUBE)
After=multi-user.target docker.service
Wants=docker.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /home/jetson/oled_stats.py
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now oled-stats.service
sudo systemctl restart oled-stats.service     # after editing the script
journalctl -u oled-stats -f                   # logs
```

If you installed the Python packages in a virtualenv, point `ExecStart` at that interpreter instead (create it with `--system-site-packages` so `python3-smbus`, `psutil` and `PIL` stay visible).

---

## The Load index

One number that answers *"is this machine busy right now?"*. It is the **busiest of four resources**, not an average, because on a Jetson the CPU, GPU and memory form one system: an LLM can saturate the GPU or the memory bus while the CPU is nearly idle, and an average would hide that.

```
Load = max( CPU, GPU, MEM, STALL )
```

| Term | What it measures | Source | Reaches 100 when |
|---|---|---|---|
| **CPU** | The larger of CPU busy % and *runnable tasks ÷ cores × 100* | `psutil`, `/proc/stat` | All cores busy; goes **above 100** when tasks queue for a core |
| **GPU** | Integrated GPU activity | `tegrastats` → `GR3D_FREQ` | GPU fully busy |
| **MEM** | Memory-controller bandwidth in use | `tegrastats` → `EMC_FREQ` | Memory bus saturated |
| **STALL** | How much time work spends waiting for RAM or disk | PSI, or a proxy (below) | See below; can exceed 100 |

Sampling: every 0.5 s during the 5 s refresh window, averaged. The displayed value is clamped to **1 … 999**, and shows `999+` above that.

### Reading the number

| Load | Level | Meaning |
|---|---|---|
| 1–10 | Idle | Almost nothing running |
| 10–35 | Light | Easy work, lots of headroom |
| 35–70 | Moderate | Real work, still margin |
| 70–100 | Busy | A resource is nearly full. **100 = fully used, nothing waiting** |
| 100–150 | Saturated | Queues or waits begin; things run slower |
| 150–300 | Overloaded | Tasks wait for CPU, or RAM/disk stalls are frequent |
| 300–999 | Extreme | Far more work than the machine can take |
| 999+ | Beyond extreme | |

### Fallbacks used on this board

Some sources aren't available on every build; each missing source simply counts as 0.

- **`MEM`** — this `tegrastats` build doesn't print `EMC_FREQ`, so the term is unavailable (`--debug` shows `mem=n/a`).
- **`STALL`** — this kernel has no PSI (`/proc/pressure/` is missing), so a proxy is used:
  `max( 10 × iowait %, 4 × MB/s swapped in+out )`.
  With PSI present it is `10 × max(memory some avg10, io some avg10)`.
- If `tegrastats` is missing entirely, only CPU and STALL remain.

> The factors in `STALL` (×10, ×4) are heuristics chosen so that "10 % of time stalled" or "25 MB/s of swapping" read as 100. GPU and EMC percentages are relative to the *current* clock, which the Jetson raises and lowers with load, so they can read high at low frequency. The index tells you if the machine is busy; it does not measure how fast any single task is running.

---

## The RGB light bar

The whole bar shows one solid colour for the current level. At power-on the CUBE runs a colour-cycling effect by default; the script turns that effect off and sets the colour.

| Colour | Load | RGB sent |
|---|---|---|
| Blue | 0–69 | `(0, 0, 255)` |
| Green | 70–129 | `(0, 255, 0)` |
| Yellow | 130–299 | `(255, 170, 0)` |
| Red | 300+ | `(255, 0, 0)` |

- **Hysteresis:** stepping *down* to a lower colour needs the Load to fall 8 points below the threshold (to 62, 122 or 292), so the bar doesn't flicker at a boundary.
- **Efficiency:** the bar is only rewritten when the colour changes, plus a refresh once a minute in case the board reset itself.
- **Fan:** register `0x08` is never written.

### I2C protocol used (address `0x0E`, write-only)

From Yahboom's "I2C Protocol" table:

| Register | Function | Values |
|---|---|---|
| `0x00` | Bead number | 0–13, `255` = all |
| `0x01` / `0x02` / `0x03` | Red / Green / Blue | 0–255 |
| `0x04` | Effect | 0 off, 1 breathing, 2 marquee, 3 rainbow, 4 dazzle, 5 flowing, 6 cyclic breathing |
| `0x05` | Effect speed | 1–3 |
| `0x06` | Effect colour | 0 red, 1 green, 2 blue, 3 yellow, 4 purple, 5 cyan, 6 white |
| `0x07` | Lights off | write 0 |
| `0x08` | Fan | *not used* |

A bead colour is written register by register (`0x00` → `0x03`). A single 4-byte block write (`RGB_WRITE = "block"`) was tested and did **not** work on this board.

---

## Configuration

Constants at the top of `oled_stats.py`:

| Name | Default | Purpose |
|---|---|---|
| `I2C_BUS` | `7` | Bus with the OLED and the RGB controller |
| `I2C_ADDR` | `0x3C` | OLED address |
| `REFRESH` | `5` | Seconds between updates (also the Load averaging window) |
| `LEVELS` | see above | Colour bands: `(min Load, name, (r, g, b))` |
| `LEVEL_HYSTERESIS` | `8` | Points the Load must drop below a threshold to step down |
| `RGB_WRITE` | `"bytes"` | How a colour is written: `"bytes"` or `"block"` |
| `DIGIT_SIZES`, `LABEL_SIZES` | — | Candidate sizes for the 7-segment digits and labels |

---

## Troubleshooting

| Symptom | Check |
|---|---|
| `No module named 'luma'` | `sudo pip3 install --break-system-packages luma.oled` (the plain `pip3 install` is blocked on Ubuntu 24.04) |
| Screen stays blank | `sudo i2cdetect -y -r 7` must show `3c`; run the script by hand to see the error |
| Light bar keeps cycling colours | Run with `--debug`: it prints `rgb=ok` or `rgb=OFF <reason>` at start (usually missing `smbus` or the wrong bus) |
| Load always low while the GPU is busy | Run `tegrastats` by hand; the script needs `GR3D_FREQ` in its output and to run as root |
| Dock shows `?` | `docker ps` fails for root — is the Docker service running? |


---

## Files

| File | Purpose |
|---|---|
| `oled_stats.py` | The display + light-bar service |

## References

- Yahboom CUBE NANO tutorials: <https://www.yahboom.net/study/CUBE_NANO>
- NVIDIA `tegrastats`: <https://docs.nvidia.com/jetson/archives/r36.5/DeveloperGuide/AT/JetsonLinuxDevelopmentTools/TegrastatsUtility.html>
