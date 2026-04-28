# SR530 Lock-In Amplifier Controller

Standalone PyQt5 GUI and RS232 driver for the **Stanford Research Systems SR530** lock-in amplifier.

Designed for use in the Yale Moore Lab optically levitated microsphere experiment (usphere-Q), and importable as a git submodule into any Python project.

---

## Hardware setup

- Connect the SR530 rear RS-232 port to a USB-to-RS232 adapter (DB-25 male → DB-9 female adapter required).
- Set baud rate, parity, and stop bits via the rear DIP switches on the SR530 (default: 9600 / 8N2 — see SR530 manual §6).

---

## Documentation

- **[docs/USER_GUIDE.md](docs/USER_GUIDE.md)** — practical guide for first-time SR530 users: physical connections, parameter selection, phase setting, calibration workflow, and common mistakes

---

## Quick start

```bash
pip install -r requirements.txt
python sr530_gui.py
```

---

## GUI tabs

| Tab | Description |
|-----|-------------|
| **Connection** | Enter COM port and baud rate, connect/disconnect, test with a frequency query |
| **Parameters** | Set and read sensitivity, phase, dynamic reserve, pre/post time constants |
| **Monitor** | Live X/Y/R/θ readout in both fraction-of-FS and real volts; overload and reference-lock indicators; optional live X plot (requires pyqtgraph) |

---

## Driver API

```python
from sr530_controller import SR530Controller

lia = SR530Controller("COM5")          # or /dev/ttyUSB0 on Linux
lia.connect()

# Read outputs
x_frac  = lia.get_x_output()          # fraction of full-scale
x_volts = lia.get_x_volts()           # real volts

# Set parameters
lia.set_sensitivity(12)               # index 12 = 1 mV FS
lia.set_phase(0.0)                    # degrees
lia.set_pre_time_constant(4)          # index 4 = 100 ms
lia.set_post_time_constant(4)
lia.set_dynamic_reserve(0)            # 0=Low Noise, 1=Normal, 2=High Reserve

# Full snapshot (one burst of serial queries)
snap = lia.snapshot()
# snap keys: x, y, r, x_v, y_v, r_v, theta, frequency, phase,
#            sensitivity_idx, sensitivity, sensitivity_v,
#            pre_tc_idx, pre_tc, status, overloaded, unlocked

lia.disconnect()
```

See `sr530_controller.py` for the full API and `SENSITIVITY_TABLE` / `TIME_CONSTANT_TABLE` constants.

---

## Use as a submodule

```bash
# From within the parent repo:
git submodule add https://github.com/Moore-Lab/SR530_controller resources/SR530_controller
git submodule update --init
```

Then in Python:
```python
import sys
sys.path.insert(0, "resources/SR530_controller")
from sr530_controller import SR530Controller
```
