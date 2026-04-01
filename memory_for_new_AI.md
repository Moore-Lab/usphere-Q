# usphere-charge: Handover Document for New AI

## The Experiment

An optically levitated microsphere is held in a vacuum chamber at Yale University. The sphere acquires charge over time through various mechanisms. Controlling the net charge state (number and polarity of elementary charges) is critical to isolating the sphere from environmental electric field noise.

---

## Hardware

| Instrument | Role |
|---|---|
| NI PXIe-6363 DAQ card | Records all analog signals at up to 2 MS/s |
| NI PXIe-7856R FPGA | PID feedback control for sphere trapping |
| WG1 — INSTEK waveform generator | Continuous sine tone to drive electrodes (charge monitoring) |
| WG2 — RIGOL DG822 waveform generator | CH1: trigger pulses to flash lamp; CH2: DC bias to lamp electrodes |
| WG3 — RIGOL DG822 waveform generator | CH1: pulse output to solid-state relay that heats the filament |
| Lock-in amplifier (optional) | Can replace software correlation analysis |

**Three actuation mechanisms:**
- **Flash lamp** (WG2): photoelectric effect — UV flash knocks electrons off sphere, reduces negative / increases positive charge
- **Filament** (WG3): thermionic emission — heated filament boils off electrons onto sphere, reduces positive / increases negative charge
- **Drive electrode** (WG1): monitoring only — sine tone whose correlation with sphere Z response encodes net charge and sign

**DAQ analog channels (by convention — verify with lab notebook):**

| Channel | Signal |
|---|---|
| ai0 | Sphere X response |
| ai1 | Sphere Y response |
| ai2 | Sphere Z response |
| ai3 | Drive electrode monitor (WG1 output) |
| ai4 | WG2 trigger monitor (flash lamp) |
| ai5 | WG3 pulse monitor (filament) |

---

## Sister Project: usphere-DAQ

`usphere-DAQ` (separate repository, cloned into `resources/usphere-DAQ/`) handles data acquisition and storage. **usphere-charge must not depend on or modify usphere-DAQ internals.** The coupling is intentionally thin — filesystem only.

### H5 File Structure

```
beads/
  data/
    pos_data    float64, shape (32, n_samples)
      attrs:
        schema_version  int
        Fsamp           float64  (sample rate Hz)
        Time            float64  (Unix timestamp of file start)
    FPGA        shape (0,) — FPGA register values as attrs
    TIC         shape (0,) — pressure gauge values as attrs
```

- Files written sequentially: `<basename>_NNNN.h5`
- **Important:** The DAQ is being updated to store raw ADC counts as int, and `openh5.get_data()` converts to volts. Always use `openh5.get_data()` for reading — do not scale manually.

---

## Architecture Decisions (Already Made)

1. **Standalone package** — does not run inside usphere-DAQ, does not share a process, does not modify DAQ files.
2. **Filesystem coupling only** — watches DAQ output directory for new H5 files. When a new file appears, reads it, runs analysis, updates charge state.
3. **Abstract `ChargeStateSource`** — two concrete implementations:
   - `FileBasedSource`: watches for new H5 files, runs `checkQ` correlation analysis
   - `LockInSource`: stub for now; reads a DAQ channel or serial/GPIB from lock-in. **Must remain scaffolded so it can be added later without touching the control loop.**
4. **Flash lamp and filament must NEVER be active simultaneously** — hard interlock enforced in the control loop, not just the GUI.
5. **Existing WG drivers imported, not rewritten** — RIGOL drivers are in `resources/RIGOLDG822_controller/`. Wrap behind thin interface; do not duplicate.
6. **Bang-bang control loop** — not PID. Keep it simple and explicit.
7. **PyQt5** — the DAQ uses PyQt5; match it exactly (not PySide6).
8. **Flat script layout** — no package/setup.py needed.

---

## Project Layout

```
usphere-charge/
  charge_gui.py              # Main GUI entry point (DONE)
  wg_flashlamp.py            # WG2 RIGOL wrapper — flash lamp (DONE)
  wg_filament.py             # WG3 RIGOL wrapper — filament (DONE)
  wg_drive.py                # WG1 INSTEK stub — drive tone (DONE, stub only)
  charge_analysis.py         # Charge state analysis module (NOT STARTED)
  charge_control.py          # Closed-loop bang-bang controller (NOT STARTED)
  charge_session_log.jsonl   # Rolling JSON-lines config log (auto-created)
  memory_for_new_AI.md       # This file
  usphere_charge_CLAUDE.md   # Original project spec (read this too)

  resources/
    usphere-DAQ/             # DAQ sister project (read-only reference)
      daq_core.py
      daq_gui.py             # Model for GUI architecture
      daq_h5.py              # H5 schema — single source of truth
      daq_fpga.py            # Plugin example
      daq_edwards_tic.py     # Plugin example
    RIGOLDG822_controller/   # RIGOL WG driver (import, do not modify)
      dg822_controller.py    # High-level: DG822Controller
      dg822_connection.py    # VISA layer: DG822Connection
      dg822_waveform.py      # Waveform config: DG822Waveform
      dg822_output.py        # Output control: DG822Output
    Microsphere-Utility-Scripts/
      checkQ.py              # Charge analysis core (import, do not modify)
      checkQ_online.py       # File-watcher live analysis script (reference)
      openh5.py              # H5 reader (import, do not modify)
      checkQ_calibration.json # Calibration store
```

---

## Module Protocol (GUI Plugin System)

Every hardware module (`wg_flashlamp`, `wg_filament`, `wg_drive`, and future modules) must expose:

```python
MODULE_NAME   : str         # internal key, used for config persistence
DEVICE_NAME   : str         # human-readable label in GUI
CONFIG_FIELDS : list[dict]  # auto-builds GUI form; keys: key, label, type, default
DEFAULTS      : dict        # fallback values when device is absent
test(config)  -> (bool, str) # stateless connection test; safe to call from worker thread
```

And a **Controller class** with:
```python
def connect(self) -> bool         # raises on driver/config error
def disconnect(self) -> None      # always safe to call
@property is_connected -> bool
def configure(self, config: dict) # re-applies waveform live if already connected
def enable(self) -> bool          # called by control loop to activate
def disable(self) -> bool         # called by control loop to deactivate
```

The `WGConnectionPanel` in `charge_gui.py` consumes `CONFIG_FIELDS` automatically, just like `ModulesWidget` in `daq_gui.py`.

---

## What Has Been Built

### `wg_flashlamp.py`
- Wraps WG2 (RIGOL DG822) for flash lamp control
- CH1 = pulse trigger (configurable rate, amplitude, pulse width)
- CH2 = DC bias to lamp electrodes
- `enable()` programs waveform then turns on both outputs
- `disable()` turns off both outputs
- `configure()` re-programs live if already connected
- `test()` is stateless and thread-safe
- `__main__` block for command-line hardware testing

### `wg_filament.py`
- Wraps WG3 (RIGOL DG822) for filament control
- CH1 = pulse to solid-state relay
- Same enable/disable/configure/test pattern as flash lamp

### `wg_drive.py`
- Minimal stub for WG1 (INSTEK — driver not yet written)
- Full module/controller protocol surface, all methods raise `NotImplementedError`
- GUI and control loop can reference it without error
- **Replace stub bodies once INSTEK driver is available**

### `charge_gui.py`
- Main PyQt5 GUI entry point
- **Connections tab**: one `WGConnectionPanel` per WG module, auto-built from `CONFIG_FIELDS`
  - Test, Connect, Disconnect, Enable, Disable buttons per module
  - Status labels with green/red/grey feedback
  - All hardware operations run in `_Worker` (QThread) to keep GUI responsive
  - Config saved to `charge_session_log.jsonl` on exit and via "Save current config" button
  - Config restored from log on startup
- **Placeholder tabs**: Flash Lamp, Filament, Drive, Analysis, Control (all say "coming soon")

---

## What Still Needs to Be Built

### 1. `charge_analysis.py` — **Next to build**

Wraps `checkQ.py` and `openh5.py`. Responsibilities:
- **`ChargeStateSource` abstract base class** with interface:
  ```python
  def get_latest(self) -> dict | None
  # returns: {charge_e, polarity, timestamp, ...} or None if no data yet
  def start(self) -> None
  def stop(self) -> None
  @property is_running -> bool
  ```
- **`FileBasedSource`**: watches a user-specified directory for new H5 files
  - Uses `checkQ.measure_charge(drive_signal, position_signal, sample_rate, calibration)`
  - Needs: watch directory, calibration file path, sphere diameter, drive/position channel indices
  - Mimics the loop in `checkQ_online.py` but without matplotlib — emits data via callback/signal
  - Should handle the "only one new file" wait (the `time.sleep(1)` in `checkQ_online.py`)
- **`LockInSource`**: stub — same interface, `NotImplementedError` bodies
- **Calibration management**: load/save calibration via `checkQ.load_calibration()` / `checkQ.write_calibration_entry()`
- **GUI tab** (Analysis tab in `charge_gui.py`):
  - Directory picker, calibration file picker, channel selectors, sphere diameter field
  - Live charge display (number + polarity, updating per file)
  - Start/Stop monitoring button
  - Small plot of charge vs time (use pyqtgraph or matplotlib Qt backend)

### 2. `charge_control.py` — **After analysis**

Bang-bang closed-loop controller. Responsibilities:
- Takes a `ChargeStateSource`, a `FlashLampController`, and a `FilamentController`
- User-configurable thresholds: outer band (begin correction) and inner band (stop correction)
- Logic:
  ```
  charge > +outer  →  flash lamp ON  until charge < +inner
  charge < −outer  →  filament ON    until charge > −inner
  otherwise        →  idle
  ```
- **Hard interlock**: flash lamp and filament must never be active simultaneously — enforce this in code, not just by correct logic
- Runs in a background thread; communicates with GUI via Qt signals
- GUI tab (Control tab in `charge_gui.py`):
  - Outer/inner threshold spinboxes
  - Enable/disable automation toggle
  - Status display: current state (idle / correcting positive / correcting negative)
  - Log of correction events with timestamps

### 3. Individual WG control tabs

Each of the Flash Lamp, Filament, Drive placeholder tabs should become a real tab allowing manual control (separate from the Connections tab). These are low priority until the control loop is working.

### 4. INSTEK driver (`wg_drive.py` stub → real implementation)

Once the INSTEK driver repo is ready, fill in `DriveController` in `wg_drive.py`. The existing stub has the right interface — just replace the `NotImplementedError` bodies.

---

## Charge Analysis: Key Details

The charge state is determined by:
1. **Cross-correlation** between drive signal (ai3) and sphere Z response (ai2) at the drive frequency — gives charge magnitude and polarity via `checkQ.correlate_drive_position()`
2. **FFT tone response** — gives an independent magnitude estimate and phase-based polarity via `checkQ.get_tone_response()`

Both methods are in `checkQ.measure_charge()`, which returns:
```python
{
    'n_charges_corr': float,   # charge magnitude from correlation
    'polarity_corr':  float,   # +1.0 or -1.0
    'n_charges_pos':  float,   # charge magnitude from FFT
    'polarity_phase': float,   # +1.0 or -1.0
    'corr_at_lag':    float,
    'pos_response':   float,
    'phase':          float,
    'drive_amp':      float,
    'drive_scale':    float,   # ratio to calibration drive amplitude
    'f0':             float,   # detected drive frequency
}
```

**Calibration** is stored as a JSON file (`checkQ_calibration.json`). A calibration entry is keyed by `(sphere_diameter_um, drive_frequency_hz)`. Functions: `checkQ.load_calibration()`, `checkQ.write_calibration_entry()`, `checkQ.run_calibration()`.

The calibration constant (electrons per unit correlation) is **user-configurable, not hardcoded**.

---

## Control Loop Timescale

- With `FileBasedSource`: ~1–2 seconds minimum (one DAQ file = ~1 second of data)
- The charge state changes slowly (seconds to minutes under normal conditions)
- Do not design for faster control unless `LockInSource` is active

---

## Key Constraints (Do Not Violate)

1. Flash lamp and filament **must never be active simultaneously** — hard interlock
2. WG1 (drive tone) runs **continuously** during monitoring — independent of control loop state
3. A crash in the automation loop **must not affect DAQ recording** (they are separate processes)
4. **Do not rewrite the RIGOL driver** — import it from `resources/RIGOLDG822_controller/`
5. **Do not rewrite checkQ or openh5** — import from `resources/Microsphere-Utility-Scripts/`
6. **Do not modify usphere-DAQ** — read-only reference
7. **Use PyQt5** — not PySide6, not PyQt6
8. **Use `openh5.get_data()` for reading H5 files** — it handles ADC count → volts conversion

---

## Development Philosophy (User Preferences)

- **Cyclic bottom-up** — build one module at a time, fully, before moving to the next
- **No speculative abstractions** — build what is needed now
- **Modular with clean interfaces** — modules must be independently testable (that's why the Connections tab exists before the control loop)
- **Graceful degradation** — missing hardware should never crash the application; show "not connected" state instead
- **No extra features** — don't add error handling, fallbacks, or validation for scenarios that can't happen
- **Flat script layout** — no package structure needed
- **Session config persistence** — rolling JSON-lines log, restore on startup (same pattern as usphere-DAQ)

---

## Reading the Source Material

Before writing any new code, read these files:

| File | Why |
|---|---|
| `resources/usphere-DAQ/daq_core.py` | Plugin registry, worker thread, callback pattern |
| `resources/usphere-DAQ/daq_gui.py` | GUI architecture model (tabs, ModulesWidget, signal bridge) |
| `resources/usphere-DAQ/daq_h5.py` | H5 schema — single source of truth |
| `resources/Microsphere-Utility-Scripts/checkQ.py` | All analysis functions you will call |
| `resources/Microsphere-Utility-Scripts/checkQ_online.py` | The live file-watcher loop to replicate in Qt |
| `resources/Microsphere-Utility-Scripts/openh5.py` | H5 reader — use this, do not re-implement |
| `resources/RIGOLDG822_controller/dg822_controller.py` | High-level RIGOL interface |
| `wg_flashlamp.py` | Module/controller pattern to follow for new modules |
| `charge_gui.py` | GUI structure — how tabs, panels, workers fit together |
