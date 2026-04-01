# Context for usphere-charge

This file is intended to be copied to the root of the usphere-charge project as
`CLAUDE.md` so that Claude Code loads it automatically on every session.

---

## The experiment

An optically levitated microsphere is held in a vacuum chamber at Yale University.
The sphere acquires charge over time through various mechanisms. Controlling the
net charge state (number and polarity of elementary charges) is critical to
isolating the sphere from environmental electric field noise.

Three actuation mechanisms are used:
- **Flash lamp** (photoelectric effect): UV flash knocks electrons off the sphere,
  reducing negative charge / increasing positive charge.
- **Filament** (thermionic emission): A heated filament boils off electrons that
  drift onto the sphere, reducing positive charge / increasing negative charge.
- **Drive electrode** (monitoring only): A single-frequency sine wave is applied to
  electrodes inside the chamber. The correlation between the drive field and the
  sphere's mechanical response encodes the net charge and its sign.

---

## Hardware

| Instrument | Purpose |
|---|---|
| NI PXIe-6363 DAQ card | Records all analog signals at up to 2 MS/s |
| NI PXIe-7856R FPGA | PID feedback control for sphere trapping |
| Waveform generator 1 (WG1) | Outputs a single sine tone to drive electrodes — used to monitor charge state |
| Waveform generator 2 (WG2) | Two-channel: trigger pulses to flash lamp + DC bias to lamp electrodes (controls flash rate and photons/flash) |
| Waveform generator 3 (WG3) | Pulse output to solid-state relay that heats the filament |
| Lock-in amplifier (optional) | Can replace software correlation analysis — outputs a DC voltage proportional to net charge. Keep this as a supported data source. |

All hardware is physically set up and tested. Python drivers / GUIs for the
waveform generators already exist and should be imported here, not rewritten.

---

## Sister project: usphere-DAQ

usphere-DAQ (separate repository) handles data acquisition and storage.
usphere-charge must not depend on or modify usphere-DAQ internals — the
coupling between the two projects is intentionally thin.

### What usphere-DAQ does

- Records analog channels from the NI PXIe-6363 to HDF5 files.
- Each file covers a fixed number of samples (configurable, typically ~1 second
  of data at the chosen sample rate).
- Files are written to a user-specified output directory with a sequential naming
  convention: `<basename>_NNNN.h5`.
- Each H5 file has the structure:

```
beads/
  data/
    pos_data          # float64, shape (32, n_samples) — 32-channel analog input
    FPGA              # shape (0,), attrs = {register_name: float, ...}
    TIC               # shape (0,), attrs = {"APGX": float, "WRG": float}  (pressures in mbar)
```

- `pos_data` dataset attributes include `Fsamp` (sample rate in Hz) and `Time`
  (Unix timestamp of file start).
- Channels are `ai0`–`ai31`; only channels selected in the GUI contain non-zero
  data. The `recorded_channels()` function in `daq_h5.py` returns which channels
  were actually recorded.

### Relevant channels (by convention — verify with lab notebook)

| Channel | Signal |
|---|---|
| ai0 | Sphere X response |
| ai1 | Sphere Y response |
| ai2 | Sphere Z response |
| ai3 | Drive electrode monitor (WG1 output) |
| ai4 | WG2 trigger monitor (flash lamp) |
| ai5 | WG3 pulse monitor (filament) |

(These are conventions — the actual channel assignments should be confirmed and
stored here once verified.)

### Reading usphere-DAQ files

The `daq_h5.py` module in usphere-DAQ is the single source of truth for the H5
schema. For reading in usphere-charge, either:
- Copy the relevant read functions, or
- Add usphere-DAQ to `sys.path` and import `daq_h5` directly.

Minimum read pattern:

```python
import h5py
import numpy as np

def read_channel(filepath, channel_index, fsamp_key="Fsamp"):
    with h5py.File(filepath, "r") as f:
        ds = f["beads/data/pos_data"]
        data = ds[channel_index, :]
        sr   = float(ds.attrs[fsamp_key])
    return data, sr
```

---

## Architecture decisions (already made)

1. **usphere-charge is a standalone package** — it does not run inside usphere-DAQ,
   does not share a process with it, and does not modify its files.

2. **Coupling via filesystem only** — usphere-charge watches the DAQ output
   directory for new H5 files. When a new file appears it reads it, runs analysis,
   and updates the charge state estimate. This is the "pseudo-live" path.

3. **Lock-in amplifier support must be kept open** — define an abstract
   `ChargeStateSource` with (at minimum) two concrete implementations:
   - `FileBasedSource`: watches for new H5 files, runs correlation analysis
   - `LockInSource`: reads a DAQ channel (or serial/GPIB from lock-in) in real time
   The control loop and actuation logic must not depend on which source is active.

4. **Failure isolation** — a crash or hang in the automation loop must not affect
   DAQ recording, and vice versa.

5. **Existing waveform generator code is imported, not rewritten** — wrap it
   behind a thin interface if needed, but do not duplicate it.

---

## Scope of usphere-charge

### Tabs / modules planned

| Module | Responsibility |
|---|---|
| `wg_drive.py` + tab | Configure and start/stop WG1 (drive tone frequency, amplitude) |
| `wg_flashlamp.py` + tab | Configure WG2 (flash rate, DC bias / photons per flash) |
| `wg_filament.py` + tab | Configure WG3 (pulse width, repetition rate for filament) |
| `charge_analysis.py` + tab | Live charge state display: net charge (electrons) and sign |
| `charge_control.py` + tab | Automation: set thresholds, enable closed-loop control |

### Charge control logic (closed-loop)

User sets:
- **Outer threshold** (e.g., ±100e): if charge exceeds this, begin correction
- **Inner threshold** (e.g., ±10e): stop correction once charge is within this band

Logic:
- Charge > +outer → activate flash lamp until charge < +inner
- Charge < −outer → activate filament until charge > −inner
- Otherwise → idle

This is a simple bang-bang controller. Keep the implementation simple and explicit
rather than using a general PID framework.

### Charge state analysis

The charge state is determined by computing the cross-correlation (or lock-in
demodulation) between:
- The drive electrode signal (WG1 monitor, e.g. ai3)
- The sphere Z response (e.g. ai2)

at the drive frequency. The in-phase component gives the charge magnitude; the
sign gives the polarity. This has been calibrated — the calibration constant
(electrons per unit correlation) should be stored as a user-configurable
parameter, not hardcoded.

---

## Key constraints

- The control loop timescale with file-based analysis is ~1–2 seconds minimum.
  Do not design for faster control unless the lock-in source is active.
- The sphere charge state changes slowly (seconds to minutes timescale under
  normal conditions). A 1–2 second loop is adequate for the file-based source.
- The flash lamp and filament must never be active simultaneously — enforce this
  as a hard interlock in the control logic.
- WG1 (drive tone) runs continuously during monitoring and is independent of
  the control loop state.
