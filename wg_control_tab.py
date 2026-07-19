"""
wg_control_tab.py

Paul trap electrode and actuator waveform control tab for charge_gui.py.

Sub-tabs:
    Electrode Map  — assign WG/CH to X, Y, Z electrode axes
    X Electrode    — full waveform control for X axis
    Y Electrode    — full waveform control for Y axis
    Z Electrode    — full waveform control for Z axis
    Filament       — pulse to SSR (0 V low, V_high high)
    Flash Lamp     — trigger pulse + DC control
    Sweep          — automated amplitude/frequency sweep with DAQ recording

Public attributes on WaveformControlTab (for control-loop wiring):
    electrode_map  : ElectrodeMapWidget
    nge_map        : NGEChannelMap
    x_drive        : ChannelControlWidget
    y_drive        : ChannelControlWidget
    z_drive        : ChannelControlWidget
    filament       : PulseGroup          (WG3-CH2 trigger to the SSR)
    filament_power : NGEControlGroup     (NGE filament power line)
    flash_trigger  : PulseGroup          (WG3-CH1 trigger pulse)
    flash_control  : NGEControlGroup     (NGE flash-lamp control voltage)
    flashlamp      : FlashLampAdapter    (trigger + NGE control)
    sweep          : SweepTab
"""

from __future__ import annotations

import math
import queue
import threading as _threading
import time
from collections import deque

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

try:
    import numpy as np
    import pyqtgraph as pg
    pg.setConfigOption("background", "w")
    pg.setConfigOption("foreground", "k")
    _PLOT_AVAILABLE = True
except ImportError:
    _PLOT_AVAILABLE = False

try:
    import sys as _sys
    import os as _os
    _ZMQ_BASE = _os.path.join(_os.path.dirname(__file__))
    if _ZMQ_BASE not in _sys.path:
        _sys.path.insert(0, _ZMQ_BASE)
    from zmq_base import ModuleClient as _ModuleClient
    _ZMQ_AVAILABLE = True
except Exception:
    _ZMQ_AVAILABLE = False

try:
    import sys as _sys
    import os as _os
    _AFG_CTRL_DIR = _os.path.join(_os.path.dirname(__file__),
                                   "resources", "GWINSTEKAFG2225_controller")
    if _AFG_CTRL_DIR not in _sys.path:
        _sys.path.insert(0, _AFG_CTRL_DIR)
    from afg2225_arbitrarywf import WaveformGenerator as _WaveformGenerator
    from afg2225_arbitrarywf import compute_optimal_points_for_comb as _comb_pts
    _ARB_AVAILABLE = True
except Exception:
    _ARB_AVAILABLE = False

_WG_OPTIONS = ["WG1", "WG2", "WG3"]
_CH_OPTIONS = ["CH1", "CH2"]
# Used by PulseGroup / DCGroup selector rows
_IMP_OPTIONS = ["50 Ω", "High Z"]

_GREEN = "background-color: #4CAF50; color: white;"
_RED   = "background-color: #F44336; color: white;"
_HINT  = "color: #9E9E9E; font-size: 11px;"
_WARN  = "color: #F44336; font-weight: bold; font-size: 11px;"

_PW_UNITS = ["ns", "µs", "ms", "s"]
_PW_MULTS = {"ns": 1e-9, "µs": 1e-6, "ms": 1e-3, "s": 1.0}

_WF_DISPLAY = {"SIN": "Sine", "SQU": "Square", "RAMP": "Ramp",
               "NOIS": "Noise", "ARB": "ARB", "PULS": "Pulse"}


def _fmt_freq(hz: float) -> str:
    if hz >= 1e6:
        return f"{hz/1e6:.4g} MHz"
    if hz >= 1e3:
        return f"{hz/1e3:.4g} kHz"
    return f"{hz:.4g} Hz"


def _query_ch_status(afg, ch: int) -> str:
    """Read current channel state from hardware. May raise on serial error."""
    on   = afg.is_output_on(ch)
    raw  = afg.get_waveform_type(ch) or ""
    wf   = _WF_DISPLAY.get(raw.upper().strip(), raw.strip() or "?")
    freq = afg.get_frequency(ch)
    amp  = afg.get_amplitude(ch)
    off  = afg.get_offset(ch)
    parts = ["ON" if on else "OFF", wf]
    if freq is not None:
        parts.append(_fmt_freq(freq))
    if amp is not None:
        parts.append(f"{amp:.3g} Vpp")
    if off is not None and abs(off) > 0.001:
        parts.append(f"offset {off:+.3g} V")
    return "  ".join(parts)


# One lock per AFG connection object — prevents concurrent serial writes when
# multiple electrode tabs share the same physical WG (e.g. WG1-CH1 and WG1-CH2).
_afg_serial_locks: "dict[int, _threading.Lock]" = {}


def _afg_lock(afg) -> "_threading.Lock":
    """Return the per-connection serial lock for an AFG object."""
    key = id(afg)
    if key not in _afg_serial_locks:
        _afg_serial_locks[key] = _threading.Lock()
    return _afg_serial_locks[key]


def _poll_hw_async(afg, ch: int, on_result) -> None:
    """
    Query AFG channel status in a daemon thread; call on_result(text) on the
    Qt main thread when done.  Serialises concurrent callers on the same AFG
    connection so serial commands never interleave.
    """
    lock = _afg_lock(afg)

    def _run():
        with lock:
            try:
                txt = _query_ch_status(afg, ch)
            except Exception:
                txt = None
        if txt is not None:
            QTimer.singleShot(0, lambda: on_result(txt))

    _threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# Helpers used by PulseGroup / DCGroup
# ---------------------------------------------------------------------------

def _hint_label() -> QLabel:
    lbl = QLabel("")
    lbl.setStyleSheet(_HINT)
    return lbl


def _add_selector_row(grid: QGridLayout, row: int,
                      wg_default: str, ch_default: str,
                      imp_default: str = "High Z"):
    """Add WG / CH / Load combos to *grid* at *row*. Returns (wg, ch, imp)."""
    grid.addWidget(QLabel("WG:"), row, 0, Qt.AlignRight)
    wg = QComboBox()
    wg.addItems(_WG_OPTIONS)
    wg.setCurrentText(wg_default)
    wg.setMaximumWidth(70)
    grid.addWidget(wg, row, 1)

    grid.addWidget(QLabel("CH:"), row, 2, Qt.AlignRight)
    ch = QComboBox()
    ch.addItems(_CH_OPTIONS)
    ch.setCurrentText(ch_default)
    ch.setMaximumWidth(70)
    grid.addWidget(ch, row, 3)

    grid.addWidget(QLabel("Load:"), row, 4, Qt.AlignRight)
    imp = QComboBox()
    imp.addItems(_IMP_OPTIONS)
    imp.setCurrentText(imp_default)
    imp.setMaximumWidth(80)
    grid.addWidget(imp, row, 5)

    return wg, ch, imp


def _add_buttons(grid: QGridLayout, row: int, apply_fn, on_fn, off_fn):
    btn_row = QHBoxLayout()
    apply_btn = QPushButton("Apply")
    apply_btn.clicked.connect(apply_fn)
    btn_row.addWidget(apply_btn)

    on_btn = QPushButton("Output ON")
    on_btn.setStyleSheet(_GREEN)
    on_btn.clicked.connect(on_fn)
    btn_row.addWidget(on_btn)

    off_btn = QPushButton("Output OFF")
    off_btn.setStyleSheet(_RED)
    off_btn.clicked.connect(off_fn)
    btn_row.addWidget(off_btn)

    btn_row.addStretch()
    grid.addLayout(btn_row, row, 0, 1, 8)


# ---------------------------------------------------------------------------
# Electrode Map
# ---------------------------------------------------------------------------

class ElectrodeMapWidget(QGroupBox):
    """
    Maps electrode axes (X, Y, Z) to WG/CH pairs.

    Default mapping (2026-07-15 hardware, verified against the DAQ):
        X → WG2-CH1   (AI18)
        Y → WG1-CH1   (AI19)
        Z → WG2-CH2   (AI20)
    WG1-CH2 is the lock-in reference (fixed amplitude into SR530 REF IN — not
    an electrode, so it is not in this map); WG3 carries the flash-lamp trigger
    (CH1) and filament trigger (CH2).

    The electrode drive tabs read from this map at call time, so changes
    take effect immediately without restarting.
    """

    _DEFAULTS: dict[str, tuple[str, str]] = {
        "x": ("WG2", "CH1"),
        "y": ("WG1", "CH1"),
        "z": ("WG2", "CH2"),
    }

    def __init__(self, get_afg, parent=None):
        super().__init__("Electrode Channel Map", parent)
        self._get_afg = get_afg
        self._wg: dict[str, QComboBox] = {}
        self._ch: dict[str, QComboBox] = {}
        self._build()

    def _build(self):
        g = QGridLayout(self)
        g.setColumnStretch(3, 1)

        for col, hdr in enumerate(["Axis", "WG", "CH"]):
            lbl = QLabel(hdr)
            lbl.setStyleSheet("font-weight: bold;")
            g.addWidget(lbl, 0, col, Qt.AlignCenter)

        for r, (axis, (wg_def, ch_def)) in enumerate(self._DEFAULTS.items(), 1):
            g.addWidget(QLabel(f"{axis.upper()} electrode:"), r, 0, Qt.AlignRight)

            wg_cb = QComboBox()
            wg_cb.addItems(_WG_OPTIONS)
            wg_cb.setCurrentText(wg_def)
            wg_cb.setMaximumWidth(70)
            g.addWidget(wg_cb, r, 1)
            self._wg[axis] = wg_cb

            ch_cb = QComboBox()
            ch_cb.addItems(_CH_OPTIONS)
            ch_cb.setCurrentText(ch_def)
            ch_cb.setMaximumWidth(70)
            g.addWidget(ch_cb, r, 2)
            self._ch[axis] = ch_cb

        note = QLabel(
            "Changes take effect immediately — electrode tabs read from this map.\n"
            "Default: X → WG2-CH1,  Y → WG1-CH1,  Z → WG2-CH2   "
            "(WG1-CH2 = lock-in reference)"
        )
        note.setStyleSheet(_HINT)
        g.addWidget(note, 4, 0, 1, 4)

        # --- Combined output (both channels of one WG simultaneously) ---
        sep = QLabel("Both-channel output")
        sep.setStyleSheet("font-weight: bold; margin-top: 8px;")
        g.addWidget(sep, 5, 0, 1, 4)

        self._both_status: dict[int, QLabel] = {}
        for r, wg_n in enumerate((1, 2, 3), 6):
            g.addWidget(QLabel(f"WG{wg_n}:"), r, 0, Qt.AlignRight)

            on_btn = QPushButton(f"CH1 + CH2  ON")
            on_btn.setStyleSheet(_GREEN)
            on_btn.setMaximumWidth(140)
            on_btn.clicked.connect(lambda checked, n=wg_n: self._both_on(n))
            g.addWidget(on_btn, r, 1)

            off_btn = QPushButton("Both OFF")
            off_btn.setStyleSheet(_RED)
            off_btn.setMaximumWidth(100)
            off_btn.clicked.connect(lambda checked, n=wg_n: self._both_off(n))
            g.addWidget(off_btn, r, 2)

            st = QLabel("—")
            st.setStyleSheet("color: gray;")
            g.addWidget(st, r, 3)
            self._both_status[wg_n] = st

    def _both_on(self, wg_n: int):
        afg = self._get_afg(wg_n)
        st  = self._both_status[wg_n]
        if afg is None or not afg.is_connected:
            st.setText("WG not connected")
            st.setStyleSheet("color: red;")
            return
        try:
            afg.output_on(1)
            afg.output_on(2)
            st.setText("CH1 + CH2 ON")
            st.setStyleSheet("color: green;")
        except Exception as e:
            st.setText(str(e))
            st.setStyleSheet("color: red;")

    def _both_off(self, wg_n: int):
        afg = self._get_afg(wg_n)
        st  = self._both_status[wg_n]
        if afg is None or not afg.is_connected:
            st.setText("WG not connected")
            st.setStyleSheet("color: red;")
            return
        try:
            afg.output_off(1)
            afg.output_off(2)
            st.setText("Both OFF")
            st.setStyleSheet("color: gray;")
        except Exception as e:
            st.setText(str(e))
            st.setStyleSheet("color: red;")

    # -- Accessors -----------------------------------------------------------

    def get_wg_n(self, axis: str) -> int:
        """Return WG index (1/2/3) for axis."""
        return self._wg[axis].currentIndex() + 1

    def get_ch(self, axis: str) -> int:
        """Return channel number (1/2) for axis."""
        return self._ch[axis].currentIndex() + 1

    def get_afg_ch(self, axis: str) -> tuple:
        """Return (AFG2225Controller | None, ch: int) for axis."""
        wg_n = self.get_wg_n(axis)
        ch   = self.get_ch(axis)
        afg  = self._get_afg(wg_n)
        return afg, ch

    def assignment_str(self, axis: str) -> str:
        return f"WG{self.get_wg_n(axis)}-CH{self.get_ch(axis)}"

    # -- Config persistence --------------------------------------------------

    def get_config(self) -> dict:
        return {
            axis: {"wg": self._wg[axis].currentText(),
                   "ch": self._ch[axis].currentText()}
            for axis in ("x", "y", "z")
        }

    def restore_config(self, cfg: dict):
        for axis in ("x", "y", "z"):
            if axis in cfg:
                d = cfg[axis]
                if "wg" in d:
                    self._wg[axis].setCurrentText(str(d["wg"]))
                if "ch" in d:
                    self._ch[axis].setCurrentText(str(d["ch"]))


# ---------------------------------------------------------------------------
# Frequency comb background worker
# ---------------------------------------------------------------------------

class _CombWorker(QThread):
    """Upload a random-phase frequency comb (ARB) to the AFG in a background thread."""

    log      = pyqtSignal(str)
    finished = pyqtSignal(bool, str)   # success, message

    def __init__(self, afg, ch: int, frequencies: list, amplitude: float,
                 offset: float, n_mc: int, parent=None):
        super().__init__(parent)
        self._afg       = afg
        self._ch        = ch
        self._freqs     = frequencies
        self._amplitude = amplitude
        self._offset    = offset
        self._n_mc      = n_mc

    def run(self):
        import numpy as np
        try:
            freqs = self._freqs
            n_pts, info = _comb_pts(freqs)
            if info.get("warning"):
                self.log.emit(f"Warning: {info['warning']}")
            fundamental = info["fundamental"]
            self.log.emit(
                f"Comb: {len(freqs)} tone(s), {n_pts} pts, "
                f"fundamental {fundamental:.4g} Hz"
            )

            n = len(freqs)
            if self._n_mc > 0 and n > 1:
                self.log.emit(f"MC phase optimization ({self._n_mc} iters)…")
                best_phases = list(np.random.uniform(0, 360, n))
                best_wf = _WaveformGenerator.frequency_comb(
                    freqs, phases=best_phases, num_points=n_pts)
                best_rms = float(np.sqrt(np.mean(best_wf.data ** 2)))

                for _ in range(self._n_mc):
                    new_phases = best_phases[:]
                    idx = int(np.random.randint(n))
                    new_phases[idx] = float(np.random.uniform(0, 360))
                    wf = _WaveformGenerator.frequency_comb(
                        freqs, phases=new_phases, num_points=n_pts)
                    rms = float(np.sqrt(np.mean(wf.data ** 2)))
                    if rms > best_rms:
                        best_rms = rms
                        best_phases = new_phases
                        best_wf = wf

                crest = 1.0 / best_rms if best_rms > 0 else float("inf")
                self.log.emit(
                    f"MC done — RMS={best_rms:.4f} crest={crest:.3f}"
                )
                wf = best_wf
            else:
                wf = _WaveformGenerator.frequency_comb(freqs, num_points=n_pts)

            self.log.emit(f"Uploading {n_pts} points to CH{self._ch}…")
            ok = self._afg.waveform.upload_arbitrary_waveform(self._ch, wf.data)
            if not ok:
                self.finished.emit(False, "Upload failed — check serial connection")
                return

            ok2 = self._afg.waveform.apply_arbitrary(
                self._ch, fundamental, self._amplitude, self._offset)
            msg = (
                f"Comb applied — {len(freqs)} tones, "
                f"{fundamental:.4g} Hz fundamental, {self._amplitude:.3g} Vpp"
            )
            self.finished.emit(ok2, msg if ok2 else "apply_arbitrary failed")
        except Exception as exc:
            self.finished.emit(False, str(exc))


# ---------------------------------------------------------------------------
# Per-axis full waveform control
# ---------------------------------------------------------------------------

class ChannelControlWidget(QWidget):
    """
    Full waveform control for one electrode axis (X / Y / Z).

    Supports: Sine, Square, Pulse, Ramp, Noise.

    The WG/CH assignment is read from ``electrode_map`` at call time so that
    it always reflects the current map without requiring re-instantiation.
    """

    WAVEFORMS = ["Sine", "Square", "Pulse", "Ramp", "Noise"]

    def __init__(self, axis: str, electrode_map: ElectrodeMapWidget, parent=None):
        super().__init__(parent)
        self._axis = axis
        self._map  = electrode_map
        # Optional callback(axis) fired after a successful Apply — used to
        # re-mirror the lock-in reference onto this channel if it mirrors it.
        self._on_applied = None
        # Optional callback(axis, is_on) fired after Output ON/OFF — used to
        # mirror this channel's output state onto the lock-in reference.
        self._on_output_changed = None
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(6)
        outer.setContentsMargins(6, 6, 6, 6)

        # Current assignment info (updates when map changes)
        self._assign_lbl = QLabel()
        self._assign_lbl.setStyleSheet(_HINT)
        self._refresh_assign_lbl()
        outer.addWidget(self._assign_lbl)
        for cb in (self._map._wg[self._axis], self._map._ch[self._axis]):
            cb.currentIndexChanged.connect(self._refresh_assign_lbl)

        # === Waveform type ===
        wf_box = QGroupBox("Waveform type")
        wf_h = QHBoxLayout(wf_box)
        self._wf_btns: dict[str, QRadioButton] = {}
        self._wf_bg = QButtonGroup(self)
        for wf in self.WAVEFORMS:
            rb = QRadioButton(wf)
            self._wf_btns[wf] = rb
            self._wf_bg.addButton(rb)
            wf_h.addWidget(rb)
        self._wf_btns["Sine"].setChecked(True)
        wf_h.addStretch()
        outer.addWidget(wf_box)

        # === Parameters ===
        param_box = QGroupBox("Parameters")
        g = QGridLayout(param_box)
        g.setColumnStretch(4, 1)
        row = 0

        # Frequency
        g.addWidget(QLabel("Frequency:"), row, 0, Qt.AlignRight)
        self._freq = QDoubleSpinBox()
        self._freq.setRange(0.001, 25e6)
        self._freq.setDecimals(3)
        self._freq.setValue(100.0)
        self._freq.setMinimumWidth(110)
        g.addWidget(self._freq, row, 1)
        self._freq_unit = QComboBox()
        self._freq_unit.addItems(["Hz", "kHz", "MHz"])
        self._freq_unit.setMaximumWidth(60)
        g.addWidget(self._freq_unit, row, 2)
        row += 1

        # Amplitude
        g.addWidget(QLabel("Amplitude:"), row, 0, Qt.AlignRight)
        self._amp = QDoubleSpinBox()
        self._amp.setRange(0.001, 20.0)
        self._amp.setDecimals(3)
        self._amp.setValue(8.0)
        self._amp.setSuffix(" Vpp")
        self._amp.setMinimumWidth(110)
        g.addWidget(self._amp, row, 1, 1, 2)
        row += 1

        # Offset
        g.addWidget(QLabel("Offset:"), row, 0, Qt.AlignRight)
        self._offset = QDoubleSpinBox()
        self._offset.setRange(-10.0, 10.0)
        self._offset.setDecimals(3)
        self._offset.setValue(0.0)
        self._offset.setSuffix(" V")
        self._offset.setMinimumWidth(110)
        g.addWidget(self._offset, row, 1, 1, 2)
        row += 1

        # Phase
        g.addWidget(QLabel("Phase:"), row, 0, Qt.AlignRight)
        self._phase = QDoubleSpinBox()
        self._phase.setRange(-180.0, 180.0)
        self._phase.setDecimals(2)
        self._phase.setValue(0.0)
        self._phase.setSuffix(" °")
        self._phase.setMinimumWidth(110)
        g.addWidget(self._phase, row, 1, 1, 2)
        row += 1

        # Duty cycle (Square only)
        self._duty_lbl = QLabel("Duty cycle:")
        g.addWidget(self._duty_lbl, row, 0, Qt.AlignRight)
        self._duty = QDoubleSpinBox()
        self._duty.setRange(1.0, 99.0)
        self._duty.setDecimals(1)
        self._duty.setValue(50.0)
        self._duty.setSuffix(" %")
        self._duty.setMinimumWidth(110)
        g.addWidget(self._duty, row, 1, 1, 2)
        row += 1

        # Pulse width (Pulse only)
        self._pw_lbl = QLabel("Pulse width:")
        g.addWidget(self._pw_lbl, row, 0, Qt.AlignRight)
        self._pw = QDoubleSpinBox()
        self._pw.setRange(0.001, 1e6)
        self._pw.setDecimals(3)
        self._pw.setValue(100.0)
        self._pw.setMinimumWidth(110)
        g.addWidget(self._pw, row, 1)
        self._pw_unit = QComboBox()
        self._pw_unit.addItems(_PW_UNITS)
        self._pw_unit.setCurrentText("µs")
        self._pw_unit.setMaximumWidth(55)
        g.addWidget(self._pw_unit, row, 2)
        row += 1

        self._pw_info_lbl = QLabel("Pulse info:")
        g.addWidget(self._pw_info_lbl, row, 0, Qt.AlignRight)
        self._pw_info = QLabel("")
        self._pw_info.setStyleSheet(_HINT)
        g.addWidget(self._pw_info, row, 1, 1, 3)
        row += 1

        # Symmetry (Ramp only)
        self._symm_lbl = QLabel("Symmetry:")
        g.addWidget(self._symm_lbl, row, 0, Qt.AlignRight)
        self._symm = QDoubleSpinBox()
        self._symm.setRange(0.0, 100.0)
        self._symm.setDecimals(1)
        self._symm.setValue(50.0)
        self._symm.setSuffix(" %")
        self._symm.setMinimumWidth(110)
        g.addWidget(self._symm, row, 1, 1, 2)

        outer.addWidget(param_box)

        # === Load impedance ===
        load_box = QGroupBox("Load impedance")
        load_h = QHBoxLayout(load_box)
        self._load_bg = QButtonGroup(self)
        self._load_highz = QRadioButton("High Z")
        self._load_50 = QRadioButton("50 Ω")
        self._load_highz.setChecked(True)
        self._load_bg.addButton(self._load_highz)
        self._load_bg.addButton(self._load_50)
        load_h.addWidget(self._load_highz)
        load_h.addWidget(self._load_50)
        load_h.addStretch()
        outer.addWidget(load_box)

        # === Buttons ===
        btn_h = QHBoxLayout()
        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(self._apply)
        on_btn = QPushButton("Output ON")
        on_btn.setStyleSheet(_GREEN)
        on_btn.clicked.connect(self._output_on)
        off_btn = QPushButton("Output OFF")
        off_btn.setStyleSheet(_RED)
        off_btn.clicked.connect(self._output_off)
        btn_h.addWidget(apply_btn)
        btn_h.addWidget(on_btn)
        btn_h.addWidget(off_btn)
        btn_h.addStretch()
        outer.addLayout(btn_h)

        self._status = QLabel("—")
        self._status.setStyleSheet("color: gray;")
        outer.addWidget(self._status)

        self._hw_lbl = QLabel("")
        self._hw_lbl.setStyleSheet(_HINT)
        self._hw_lbl.setWordWrap(True)
        outer.addWidget(self._hw_lbl)

        # === Frequency Comb (ARB) ===
        self._comb_worker: "_CombWorker | None" = None
        if _ARB_AVAILABLE:
            comb_box = QGroupBox("Frequency Comb (ARB)")
            cl = QVBoxLayout(comb_box)

            cr1 = QHBoxLayout()
            cr1.addWidget(QLabel("Frequencies (Hz):"))
            self._comb_freqs = QLineEdit()
            self._comb_freqs.setPlaceholderText("e.g.  100, 200, 500")
            cr1.addWidget(self._comb_freqs)
            cl.addLayout(cr1)

            # arange-style helper row
            cr_range = QHBoxLayout()
            cr_range.addWidget(QLabel("Range:"))
            self._comb_start = QDoubleSpinBox()
            self._comb_start.setRange(0.001, 25e6)
            self._comb_start.setDecimals(3)
            self._comb_start.setValue(100.0)
            self._comb_start.setMinimumWidth(90)
            self._comb_start.setPrefix("start ")
            cr_range.addWidget(self._comb_start)
            self._comb_stop = QDoubleSpinBox()
            self._comb_stop.setRange(0.001, 25e6)
            self._comb_stop.setDecimals(3)
            self._comb_stop.setValue(1000.0)
            self._comb_stop.setMinimumWidth(90)
            self._comb_stop.setPrefix("stop ")
            cr_range.addWidget(self._comb_stop)
            self._comb_step = QDoubleSpinBox()
            self._comb_step.setRange(0.001, 25e6)
            self._comb_step.setDecimals(3)
            self._comb_step.setValue(100.0)
            self._comb_step.setMinimumWidth(90)
            self._comb_step.setPrefix("step ")
            cr_range.addWidget(self._comb_step)
            range_btn = QPushButton("→ List")
            range_btn.setMaximumWidth(60)
            range_btn.clicked.connect(self._comb_range_to_list)
            cr_range.addWidget(range_btn)
            cr_range.addStretch()
            cl.addLayout(cr_range)

            cr2 = QHBoxLayout()
            cr2.addWidget(QLabel("Amplitude:"))
            self._comb_amp = QDoubleSpinBox()
            self._comb_amp.setRange(0.001, 20.0)
            self._comb_amp.setDecimals(3)
            self._comb_amp.setValue(1.0)
            self._comb_amp.setSuffix(" Vpp")
            self._comb_amp.setMinimumWidth(100)
            cr2.addWidget(self._comb_amp)
            cr2.addSpacing(12)
            cr2.addWidget(QLabel("MC iter:"))
            self._comb_mc = QSpinBox()
            self._comb_mc.setRange(0, 20000)
            self._comb_mc.setValue(500)
            self._comb_mc.setMinimumWidth(75)
            cr2.addWidget(self._comb_mc)
            cr2.addStretch()
            cl.addLayout(cr2)

            cr3 = QHBoxLayout()
            self._comb_btn = QPushButton("Apply Comb")
            self._comb_btn.clicked.connect(self._apply_comb)
            cr3.addWidget(self._comb_btn)
            self._comb_status = QLabel("—")
            self._comb_status.setStyleSheet("color: gray;")
            cr3.addWidget(self._comb_status, 1)
            cl.addLayout(cr3)

            outer.addWidget(comb_box)

        # === Waveform preview ===
        if _PLOT_AVAILABLE:
            self._plot_widget = pg.PlotWidget()
            self._plot_widget.setMinimumHeight(170)
            self._plot_widget.setMaximumHeight(210)
            self._plot_widget.showGrid(x=True, y=True, alpha=0.25)
            self._plot_widget.setLabel("left", "V")
            self._plot_widget.setLabel("bottom", "time")
            self._plot_widget.getAxis("left").setWidth(45)
            self._plot_curve = self._plot_widget.plot(
                [], [], pen=pg.mkPen(color="#1565C0", width=2)
            )
            outer.addWidget(self._plot_widget)
            self._plot_timer = QTimer(self)
            self._plot_timer.setSingleShot(True)
            self._plot_timer.setInterval(80)
            self._plot_timer.timeout.connect(self._update_plot)
        else:
            self._plot_widget = None
            self._plot_timer = None

        outer.addStretch()

        self._hw_poll_running = False
        self._hw_poll_timer = QTimer(self)
        self._hw_poll_timer.setInterval(8000)
        self._hw_poll_timer.timeout.connect(self._update_hw_lbl)
        self._hw_poll_timer.start()

        # Connect waveform radio buttons now that all param widgets exist
        for rb in self._wf_btns.values():
            rb.toggled.connect(self._on_wf_change)

        # Signals for pulse period/duty display
        self._freq.valueChanged.connect(self._update_pw_info)
        self._freq_unit.currentIndexChanged.connect(self._update_pw_info)
        self._pw.valueChanged.connect(self._update_pw_info)
        self._pw_unit.currentIndexChanged.connect(self._update_pw_info)

        # Signals for waveform preview
        for sb in (self._freq, self._amp, self._offset, self._phase,
                   self._duty, self._pw, self._symm):
            sb.valueChanged.connect(self._schedule_plot_update)
        for cb in (self._freq_unit, self._pw_unit):
            cb.currentIndexChanged.connect(self._schedule_plot_update)

        self._on_wf_change()

    # -- Assignment label ----------------------------------------------------

    def _refresh_assign_lbl(self):
        self._assign_lbl.setText(
            f"Currently assigned to: {self._map.assignment_str(self._axis)}"
        )

    # -- Frequency helper ----------------------------------------------------

    def _get_freq_hz(self) -> float:
        v = self._freq.value()
        u = self._freq_unit.currentText()
        if u == "kHz":
            return v * 1e3
        if u == "MHz":
            return v * 1e6
        return v

    def _get_pw_s(self) -> float:
        return self._pw.value() * _PW_MULTS.get(self._pw_unit.currentText(), 1e-6)

    # -- Pulse info display --------------------------------------------------

    def _update_pw_info(self):
        try:
            f = self._get_freq_hz()
            if f <= 0:
                return
            period = 1.0 / f
            width  = self._get_pw_s()
            duty   = (width / period) * 100

            if period >= 1:
                p = f"{period:.3f} s"
            elif period >= 1e-3:
                p = f"{period*1e3:.3f} ms"
            elif period >= 1e-6:
                p = f"{period*1e6:.3f} µs"
            else:
                p = f"{period*1e9:.3f} ns"

            if duty > 100:
                self._pw_info.setStyleSheet(_WARN)
                self._pw_info.setText(f"Period: {p}   Duty: >100% ⚠")
            else:
                self._pw_info.setStyleSheet(_HINT)
                self._pw_info.setText(f"Period: {p}   Duty: {duty:.1f}%")
        except Exception:
            pass

    # -- Waveform type change ------------------------------------------------

    def _on_wf_change(self):
        wf = self._current_wf()
        is_sq    = (wf == "Square")
        is_pulse = (wf == "Pulse")
        is_ramp  = (wf == "Ramp")
        is_noise = (wf == "Noise")

        self._phase.setEnabled(not is_noise)
        self._freq.setEnabled(not is_noise)
        self._freq_unit.setEnabled(not is_noise)

        for w in (self._duty_lbl, self._duty):
            w.setVisible(is_sq)
        for w in (self._pw_lbl, self._pw, self._pw_unit,
                  self._pw_info_lbl, self._pw_info):
            w.setVisible(is_pulse)
        for w in (self._symm_lbl, self._symm):
            w.setVisible(is_ramp)

        if is_pulse:
            self._update_pw_info()

        self._schedule_plot_update()

    def _current_wf(self) -> str:
        for wf, rb in self._wf_btns.items():
            if rb.isChecked():
                return wf
        return "Sine"

    # -- Hardware helpers ----------------------------------------------------

    def _afg_ch(self):
        afg, ch = self._map.get_afg_ch(self._axis)
        if afg is None or not afg.is_connected:
            self._set_status_err(f"WG{self._map.get_wg_n(self._axis)} not connected")
            return None, None
        return afg, ch

    def _set_impedance(self, afg, ch: int):
        if self._load_highz.isChecked():
            afg.set_load_high_z(ch)
        else:
            afg.set_load_50_ohm(ch)

    def _set_status_ok(self, msg: str):
        self._status.setText(msg)
        self._status.setStyleSheet("color: green;")

    def _set_status_err(self, msg: str):
        self._status.setText(msg)
        self._status.setStyleSheet("color: red;")

    def _update_hw_lbl(self):
        """Trigger an async hardware status poll; update label on main thread when done."""
        if self._comb_worker is not None:
            return
        if self._hw_poll_running:
            return
        afg, ch = self._map.get_afg_ch(self._axis)
        if afg is None or not afg.is_connected:
            return
        self._hw_poll_running = True

        def _done(txt):
            self._hw_lbl.setText(txt)
            self._hw_poll_running = False

        _poll_hw_async(afg, ch, _done)

    # -- Button slots --------------------------------------------------------

    def _apply(self):
        afg, ch = self._afg_ch()
        if afg is None:
            return
        wf     = self._current_wf()
        freq   = self._get_freq_hz()
        amp    = self._amp.value()
        offset = self._offset.value()
        phase  = self._phase.value()
        try:
            self._set_impedance(afg, ch)
            if wf == "Sine":
                afg.setup_sine(ch, freq, amp, offset)
                afg.set_phase(ch, phase)
            elif wf == "Square":
                afg.setup_square(ch, freq, amp, offset,
                                 duty_cycle=self._duty.value())
                afg.set_phase(ch, phase)
            elif wf == "Pulse":
                afg.setup_pulse(ch, freq, amp, offset)
                afg.waveform.set_pulse_width(ch, self._get_pw_s())
                afg.set_phase(ch, phase)
            elif wf == "Ramp":
                afg.setup_ramp(ch, freq, amp, offset,
                               symmetry=self._symm.value())
                afg.set_phase(ch, phase)
            elif wf == "Noise":
                afg.setup_noise(ch, amp, offset)
            self._set_status_ok(
                f"Applied — {wf}, {freq:.4g} Hz, {amp:.3g} Vpp"
            )
            self._update_hw_lbl()
            if self._on_applied:
                try:
                    self._on_applied(self._axis)
                except Exception:
                    pass
        except Exception as e:
            self._set_status_err(f"Error: {e}")

    def _output_on(self):
        afg, ch = self._afg_ch()
        if afg is None:
            return
        try:
            afg.output_on(ch)
            self._set_status_ok("Output ON")
            self._update_hw_lbl()
            self._notify_output(True)
        except Exception as e:
            self._set_status_err(f"Error: {e}")

    def _output_off(self):
        afg, ch = self._afg_ch()
        if afg is None:
            return
        try:
            afg.output_off(ch)
            self._status.setText("Output OFF")
            self._status.setStyleSheet("color: gray;")
            self._update_hw_lbl()
            self._notify_output(False)
        except Exception as e:
            self._set_status_err(f"Error: {e}")

    def _notify_output(self, is_on: bool):
        if self._on_output_changed:
            try:
                self._on_output_changed(self._axis, is_on)
            except Exception:
                pass

    # -- Frequency comb ------------------------------------------------------

    def _comb_range_to_list(self):
        import numpy as np
        start = self._comb_start.value()
        stop  = self._comb_stop.value()
        step  = self._comb_step.value()
        if step <= 0 or start >= stop:
            self._comb_status.setText("Invalid range (need start < stop, step > 0)")
            self._comb_status.setStyleSheet("color: red;")
            return
        freqs = np.arange(start, stop, step)
        if len(freqs) == 0:
            self._comb_status.setText("Range produced no frequencies")
            self._comb_status.setStyleSheet("color: red;")
            return
        self._comb_freqs.setText(", ".join(f"{f:.6g}" for f in freqs))
        self._comb_status.setText(f"{len(freqs)} frequencies loaded")
        self._comb_status.setStyleSheet("color: gray;")

    def _apply_comb(self):
        if not _ARB_AVAILABLE:
            return
        if self._comb_worker is not None:
            return
        afg, ch = self._afg_ch()
        if afg is None:
            return

        raw = self._comb_freqs.text().strip()
        if not raw:
            self._comb_status.setText("Enter frequencies first")
            self._comb_status.setStyleSheet("color: red;")
            return
        try:
            freqs = [float(x.strip()) for x in raw.split(",") if x.strip()]
        except ValueError:
            self._comb_status.setText("Invalid frequency list")
            self._comb_status.setStyleSheet("color: red;")
            return
        if not freqs:
            self._comb_status.setText("No valid frequencies")
            self._comb_status.setStyleSheet("color: red;")
            return

        self._comb_btn.setEnabled(False)
        self._comb_status.setText("Working…")
        self._comb_status.setStyleSheet("color: gray;")

        self._comb_worker = _CombWorker(
            afg, ch, freqs,
            amplitude=self._comb_amp.value(),
            offset=0.0,
            n_mc=self._comb_mc.value(),
            parent=self,
        )
        self._comb_worker.log.connect(
            lambda msg: self._comb_status.setText(msg))
        self._comb_worker.finished.connect(self._on_comb_done)
        self._comb_worker.start()

    def _on_comb_done(self, ok: bool, msg: str):
        self._comb_btn.setEnabled(True)
        self._comb_worker = None
        if ok:
            self._comb_status.setText(msg)
            self._comb_status.setStyleSheet("color: green;")
            self._update_hw_lbl()
        else:
            self._comb_status.setText(f"Error: {msg}")
            self._comb_status.setStyleSheet("color: red;")

    # -- Waveform preview ----------------------------------------------------

    def _schedule_plot_update(self):
        if self._plot_timer is not None:
            self._plot_timer.start()

    def _update_plot(self):
        if not _PLOT_AVAILABLE or self._plot_widget is None:
            return
        wf     = self._current_wf()
        amp    = self._amp.value()
        offset = self._offset.value()
        N      = 600

        if wf == "Noise":
            y = offset + (amp / 2.0) * (2 * np.random.rand(N) - 1)
            self._plot_curve.setData(np.arange(N, dtype=float), y)
            self._plot_widget.setLabel("bottom", "sample")
            self._plot_widget.enableAutoRange("y", True)
            return

        freq = self._get_freq_hz()
        if freq <= 0:
            return
        period    = 1.0 / freq
        t         = np.linspace(0, 2.5 * period, N)
        phase_rad = math.radians(self._phase.value())

        if wf == "Sine":
            y = offset + (amp / 2.0) * np.sin(2 * math.pi * freq * t + phase_rad)

        elif wf == "Square":
            duty  = self._duty.value() / 100.0
            t_mod = (t + phase_rad / (2 * math.pi * freq)) % period
            y     = np.where(t_mod < duty * period,
                             offset + amp / 2.0, offset - amp / 2.0)

        elif wf == "Pulse":
            width = self._get_pw_s()
            t_mod = (t + phase_rad / (2 * math.pi * freq)) % period
            y     = np.where(t_mod < width,
                             offset + amp / 2.0, offset - amp / 2.0)

        elif wf == "Ramp":
            symm  = self._symm.value() / 100.0
            t_mod = (t + phase_rad / (2 * math.pi * freq)) % period
            tn    = t_mod / period  # 0–1
            if symm <= 0:
                y = offset + amp / 2.0 - amp * tn
            elif symm >= 1:
                y = offset - amp / 2.0 + amp * tn
            else:
                y = np.where(
                    tn < symm,
                    offset - amp / 2.0 + amp * (tn / symm),
                    offset + amp / 2.0 - amp * ((tn - symm) / (1.0 - symm)),
                )
        else:
            return

        # Choose display time units
        if period < 1e-3:
            t_disp, unit = t * 1e6, "µs"
        elif period < 1:
            t_disp, unit = t * 1e3, "ms"
        else:
            t_disp, unit = t, "s"

        self._plot_curve.setData(t_disp, y)
        self._plot_widget.setLabel("bottom", f"time ({unit})")
        pad = amp / 2.0 * 0.3 + 0.01
        self._plot_widget.setYRange(offset - amp / 2.0 - pad,
                                    offset + amp / 2.0 + pad)

    # -- Public API ----------------------------------------------------------

    def get_freq_hz(self) -> float:
        return self._get_freq_hz()

    def get_afg_ch(self) -> tuple:
        return self._map.get_afg_ch(self._axis)


# ---------------------------------------------------------------------------
# Pulse group — Filament and Flash Trigger
# Waveform: 0 V when low, V_high when high.
# Offset = V_high / 2, Amplitude Vpp = V_high  →  low = 0, high = V_high.
# ---------------------------------------------------------------------------

class PulseGroup(QGroupBox):
    """
    Pulse waveform control (Filament / Flash Trigger).

    Output levels:
        low  = dc_offset
        high = dc_offset + v_high
    With dc_offset = 0 (default) the low level is always 0 V.
    """

    def __init__(self, title: str, get_afg,
                 wg_default: str, ch_default: str,
                 parent=None):
        super().__init__(title, parent)
        self._get_afg = get_afg
        self._wg_default = wg_default
        self._ch_default = ch_default
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(4)

        g = QGridLayout()
        g.setColumnStretch(7, 1)
        outer.addLayout(g)
        row = 0

        self._wg, self._ch, self._imp = _add_selector_row(
            g, row, self._wg_default, self._ch_default, "High Z"
        )
        row += 1

        # Pulse swing
        g.addWidget(QLabel("Pulse high (V):"), row, 0, Qt.AlignRight)
        self._amp = QDoubleSpinBox()
        self._amp.setRange(0.001, 20.0)
        self._amp.setDecimals(3)
        self._amp.setValue(5.0)
        self._amp.setSuffix(" V")
        self._amp.setMinimumWidth(130)
        g.addWidget(self._amp, row, 1, 1, 3)
        row += 1

        # DC offset (baseline)
        g.addWidget(QLabel("DC offset:"), row, 0, Qt.AlignRight)
        self._dc = QDoubleSpinBox()
        self._dc.setRange(-10.0, 10.0)
        self._dc.setDecimals(3)
        self._dc.setValue(0.0)
        self._dc.setSuffix(" V")
        self._dc.setMinimumWidth(130)
        g.addWidget(self._dc, row, 1, 1, 3)
        self._levels_lbl = _hint_label()
        g.addWidget(self._levels_lbl, row, 4, 1, 4)
        row += 1

        # Frequency
        g.addWidget(QLabel("Frequency:"), row, 0, Qt.AlignRight)
        self._freq = QDoubleSpinBox()
        self._freq.setRange(0.001, 25e6)
        self._freq.setDecimals(3)
        self._freq.setValue(10.0)
        self._freq.setSuffix(" Hz")
        self._freq.setMinimumWidth(130)
        g.addWidget(self._freq, row, 1, 1, 3)
        self._freq_hint = _hint_label()
        g.addWidget(self._freq_hint, row, 4, 1, 4)
        row += 1

        # Pulse width
        g.addWidget(QLabel("Pulse width:"), row, 0, Qt.AlignRight)
        self._width_ms = QDoubleSpinBox()
        self._width_ms.setRange(0.001, 1e6)
        self._width_ms.setDecimals(3)
        self._width_ms.setValue(10.0)
        self._width_ms.setSuffix(" ms")
        self._width_ms.setMinimumWidth(130)
        g.addWidget(self._width_ms, row, 1, 1, 3)
        self._width_hint = _hint_label()
        g.addWidget(self._width_hint, row, 4, 1, 4)
        row += 1

        self._warn = QLabel("")
        self._warn.setStyleSheet(_WARN)
        g.addWidget(self._warn, row, 0, 1, 8)
        row += 1

        _add_buttons(g, row, self._apply, self._output_on, self._output_off)
        row += 1

        self._status = QLabel("—")
        self._status.setStyleSheet("color: gray;")
        g.addWidget(self._status, row, 0, 1, 8)

        # Waveform preview
        if _PLOT_AVAILABLE:
            self._plot_widget = pg.PlotWidget()
            self._plot_widget.setMinimumHeight(160)
            self._plot_widget.setMaximumHeight(200)
            self._plot_widget.showGrid(x=True, y=True, alpha=0.25)
            self._plot_widget.setLabel("left", "V")
            self._plot_widget.setLabel("bottom", "time")
            self._plot_widget.getAxis("left").setWidth(45)
            self._plot_curve = self._plot_widget.plot(
                [], [], pen=pg.mkPen(color="#1565C0", width=2)
            )
            outer.addWidget(self._plot_widget)
            self._plot_timer = QTimer()
            self._plot_timer.setSingleShot(True)
            self._plot_timer.setInterval(80)
            self._plot_timer.timeout.connect(self._update_plot)
        else:
            self._plot_widget = None
            self._plot_timer = None

        for sb in (self._amp, self._dc, self._freq, self._width_ms):
            sb.valueChanged.connect(self._update_hints)
            sb.valueChanged.connect(self._schedule_plot_update)

        self._update_hints()

    def _schedule_plot_update(self):
        if self._plot_timer is not None:
            self._plot_timer.start()

    def _update_plot(self):
        if not _PLOT_AVAILABLE or self._plot_widget is None:
            return
        freq_hz = self._freq.value()
        width_s = self._width_ms.value() * 1e-3
        v_high  = self._amp.value()
        dc      = self._dc.value()
        if freq_hz <= 0:
            return
        period = 1.0 / freq_hz
        N = 600
        t = np.linspace(0, 2.5 * period, N)
        t_mod = t % period
        y = np.where(t_mod < width_s, dc + v_high, dc)

        if period < 1e-3:
            t_disp, unit = t * 1e6, "µs"
        elif period < 1:
            t_disp, unit = t * 1e3, "ms"
        else:
            t_disp, unit = t, "s"

        self._plot_curve.setData(t_disp, y)
        self._plot_widget.setLabel("bottom", f"time ({unit})")
        pad = v_high * 0.2 + 0.05
        self._plot_widget.setYRange(dc - pad, dc + v_high + pad)

    def _update_hints(self):
        freq  = self._freq.value()
        width = self._width_ms.value() * 1e-3
        period = 1.0 / freq if freq > 0 else float("inf")
        max_width_ms = period * 1e3
        max_freq_hz  = 1.0 / width if width > 0 else float("inf")

        self._width_hint.setText(f"  max: {max_width_ms:.3f} ms")
        if max_freq_hz < 1e6:
            self._freq_hint.setText(f"  max: {max_freq_hz:.3f} Hz")
        else:
            self._freq_hint.setText(f"  max: {max_freq_hz:.3e} Hz")

        dc = self._dc.value()
        v_high = self._amp.value()
        self._levels_lbl.setText(f"  low: {dc:+.3f} V   high: {dc + v_high:+.3f} V")

        if width >= period:
            self._warn.setText(
                f"⚠  Pulse width ({width*1e3:.3f} ms) ≥ period ({period*1e3:.3f} ms)"
            )
        else:
            self._warn.setText("")

    def _afg_ch(self):
        wg_n = self._wg.currentIndex() + 1
        ch   = self._ch.currentIndex() + 1
        afg  = self._get_afg(wg_n)
        if afg is None or not afg.is_connected:
            self._status.setText(f"WG{wg_n} not connected")
            self._status.setStyleSheet("color: red;")
            return None, None
        return afg, ch

    def _set_impedance(self, afg, ch):
        if self._imp.currentIndex() == 1:
            afg.set_load_high_z(ch)
        else:
            afg.set_load_50_ohm(ch)

    def _status_ok(self, msg):
        self._status.setText(msg)
        self._status.setStyleSheet("color: green;")

    def _status_err(self, msg):
        self._status.setText(msg)
        self._status.setStyleSheet("color: red;")

    def _get_offset(self) -> float:
        """AFG offset = midpoint of pulse swing + DC baseline."""
        return self._amp.value() / 2.0 + self._dc.value()

    def _apply(self):
        afg, ch = self._afg_ch()
        if afg is None:
            return
        freq_hz = self._freq.value()
        width_s = self._width_ms.value() * 1e-3
        v_high  = self._amp.value()
        dc      = self._dc.value()

        if width_s >= 1.0 / freq_hz:
            self._status_err("Pulse width ≥ period — not applied")
            return
        try:
            self._set_impedance(afg, ch)
            afg.setup_pulse(ch, frequency=freq_hz,
                            amplitude=v_high, offset=self._get_offset(),
                            width=width_s)
            self._status_ok(
                f"Applied — {freq_hz:.3f} Hz, {width_s*1e3:.3f} ms, "
                f"{dc:+.3f} V – {dc + v_high:+.3f} V"
            )
        except Exception as e:
            self._status_err(f"Error: {e}")

    def _output_on(self):
        afg, ch = self._afg_ch()
        if afg is None:
            return
        try:
            afg.output_on(ch)
            self._status_ok("Output ON")
        except Exception as e:
            self._status_err(f"Error: {e}")

    def _output_off(self):
        afg, ch = self._afg_ch()
        if afg is None:
            return
        try:
            afg.output_off(ch)
            self._status.setText("Output OFF")
            self._status.setStyleSheet("color: gray;")
        except Exception as e:
            self._status_err(f"Error: {e}")

    def enable(self):
        """Called by ChargeController to start pulsing."""
        afg, ch = self._afg_ch()
        if afg is None:
            return False
        freq_hz = self._freq.value()
        width_s = self._width_ms.value() * 1e-3
        v_high  = self._amp.value()
        afg.setup_pulse(ch, frequency=freq_hz,
                        amplitude=v_high, offset=self._get_offset(),
                        width=width_s)
        return afg.output_on(ch)

    def disable(self):
        """Called by ChargeController to stop pulsing."""
        afg, ch = self._afg_ch()
        if afg is None:
            return False
        return afg.output_off(ch)

    @property
    def is_connected(self) -> bool:
        wg_n = self._wg.currentIndex() + 1
        afg  = self._get_afg(wg_n)
        return afg is not None and afg.is_connected


# ---------------------------------------------------------------------------
# DC group — Flash Lamp Control
# ---------------------------------------------------------------------------

class DCGroup(QGroupBox):

    def __init__(self, title: str, get_afg,
                 wg_default: str, ch_default: str,
                 parent=None):
        super().__init__(title, parent)
        self._get_afg = get_afg
        self._wg_default = wg_default
        self._ch_default = ch_default
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(4)

        g = QGridLayout()
        g.setColumnStretch(7, 1)
        outer.addLayout(g)
        row = 0

        self._wg, self._ch, self._imp = _add_selector_row(
            g, row, self._wg_default, self._ch_default, "High Z"
        )
        row += 1

        g.addWidget(QLabel("Voltage:"), row, 0, Qt.AlignRight)
        self._voltage = QDoubleSpinBox()
        self._voltage.setRange(-10.0, 10.0)
        self._voltage.setDecimals(3)
        self._voltage.setValue(0.0)
        self._voltage.setSuffix(" V")
        self._voltage.setMinimumWidth(130)
        g.addWidget(self._voltage, row, 1, 1, 3)
        row += 1

        _add_buttons(g, row, self._apply, self._output_on, self._output_off)
        row += 1

        self._status = QLabel("—")
        self._status.setStyleSheet("color: gray;")
        g.addWidget(self._status, row, 0, 1, 8)

        # Waveform preview (flat DC line)
        if _PLOT_AVAILABLE:
            self._plot_widget = pg.PlotWidget()
            self._plot_widget.setMinimumHeight(120)
            self._plot_widget.setMaximumHeight(160)
            self._plot_widget.showGrid(x=False, y=True, alpha=0.25)
            self._plot_widget.setLabel("left", "V")
            self._plot_widget.hideAxis("bottom")
            self._plot_widget.getAxis("left").setWidth(45)
            self._plot_curve = self._plot_widget.plot(
                [], [], pen=pg.mkPen(color="#1565C0", width=2)
            )
            outer.addWidget(self._plot_widget)
            self._plot_timer = QTimer(self)
            self._plot_timer.setSingleShot(True)
            self._plot_timer.setInterval(80)
            self._plot_timer.timeout.connect(self._update_plot)
        else:
            self._plot_widget = None
            self._plot_timer = None

        self._voltage.valueChanged.connect(self._schedule_plot_update)
        self._schedule_plot_update()

    def _schedule_plot_update(self):
        if self._plot_timer is not None:
            self._plot_timer.start()

    def _update_plot(self):
        if not _PLOT_AVAILABLE or self._plot_widget is None:
            return
        v = self._voltage.value()
        self._plot_curve.setData([0.0, 1.0], [v, v])
        pad = max(abs(v) * 0.3, 0.5)
        self._plot_widget.setYRange(v - pad, v + pad, padding=0)
        self._plot_widget.setXRange(0, 1, padding=0.05)

    def _afg_ch(self):
        wg_n = self._wg.currentIndex() + 1
        ch   = self._ch.currentIndex() + 1
        afg  = self._get_afg(wg_n)
        if afg is None or not afg.is_connected:
            self._status.setText(f"WG{wg_n} not connected")
            self._status.setStyleSheet("color: red;")
            return None, None
        return afg, ch

    def _set_impedance(self, afg, ch):
        if self._imp.currentIndex() == 1:
            afg.set_load_high_z(ch)
        else:
            afg.set_load_50_ohm(ch)

    def _status_ok(self, msg):
        self._status.setText(msg)
        self._status.setStyleSheet("color: green;")

    def _status_err(self, msg):
        self._status.setText(msg)
        self._status.setStyleSheet("color: red;")

    def _apply(self):
        afg, ch = self._afg_ch()
        if afg is None:
            return
        v = self._voltage.value()
        try:
            self._set_impedance(afg, ch)
            afg.setup_sine(ch, frequency=1.0, amplitude=0.001, offset=v)
            self._status_ok(f"DC {v:+.3f} V applied")
        except Exception as e:
            self._status_err(f"Error: {e}")

    def _output_on(self):
        afg, ch = self._afg_ch()
        if afg is None:
            return
        try:
            afg.output_on(ch)
            self._status_ok("Output ON")
        except Exception as e:
            self._status_err(f"Error: {e}")

    def _output_off(self):
        afg, ch = self._afg_ch()
        if afg is None:
            return
        try:
            afg.output_off(ch)
            self._status.setText("Output OFF")
            self._status.setStyleSheet("color: gray;")
        except Exception as e:
            self._status_err(f"Error: {e}")

    def set_voltage(self, v: float):
        """Called externally to change the DC level and apply it."""
        self._voltage.setValue(v)
        self._apply()


# ---------------------------------------------------------------------------
# NGE100 power-supply DC control (replaces the AFG-DC "flash control" line,
# and adds a controllable filament power line).
# ---------------------------------------------------------------------------

_NGE_ROLES = [
    ("flash_control",  "Flash-lamp control"),
    ("filament_power", "Filament power"),
]
_NGE_CH_OPTIONS = ["CH1", "CH2", "CH3"]


class NGEChannelMap(QGroupBox):
    """
    Maps power-supply roles to NGE100 channels — the PSU analogue of the
    electrode channel map.

    Default (2026-07-15 hardware):
        Flash-lamp control → NGE CH1   (wired to DAQ AI23)
        Filament power     → NGE CH2   (no ADC monitor — readback via NGE)

    Control groups read from this map at call time, so reassignment takes
    effect immediately.
    """

    _DEFAULTS: dict[str, str] = {
        "flash_control":  "CH1",
        "filament_power": "CH2",
    }

    def __init__(self, get_nge, parent=None):
        super().__init__("Power-Supply Channel Map (NGE100)", parent)
        self._get_nge = get_nge
        self._ch: dict[str, QComboBox] = {}
        self._build()

    def _build(self):
        g = QGridLayout(self)
        g.setColumnStretch(2, 1)
        for col, hdr in enumerate(["Role", "NGE channel"]):
            lbl = QLabel(hdr)
            lbl.setStyleSheet("font-weight: bold;")
            g.addWidget(lbl, 0, col, Qt.AlignLeft)

        for r, (role, label) in enumerate(_NGE_ROLES, 1):
            g.addWidget(QLabel(f"{label}:"), r, 0, Qt.AlignRight)
            cb = QComboBox()
            cb.addItems(_NGE_CH_OPTIONS)
            cb.setCurrentText(self._DEFAULTS[role])
            cb.setMaximumWidth(80)
            g.addWidget(cb, r, 1)
            self._ch[role] = cb

        note = QLabel(
            "Default: flash-lamp control → NGE CH1,  filament power → NGE CH2.\n"
            "Flash-lamp control is monitored on DAQ AI23; filament power has no "
            "ADC — verify it from the NGE readback."
        )
        note.setStyleSheet(_HINT)
        g.addWidget(note, len(_NGE_ROLES) + 1, 0, 1, 3)

    # -- Accessors -----------------------------------------------------------

    def get_ch(self, role: str) -> int:
        """NGE channel number (1/2/3) for a role."""
        return self._ch[role].currentIndex() + 1

    def get_nge(self):
        """Live NGESupplyController, or None."""
        return self._get_nge()

    def assignment_str(self, role: str) -> str:
        return f"NGE-{self._ch[role].currentText()}"

    # -- Config persistence --------------------------------------------------

    def get_config(self) -> dict:
        return {role: cb.currentText() for role, cb in self._ch.items()}

    def restore_config(self, cfg: dict):
        for role, cb in self._ch.items():
            if role in cfg:
                cb.setCurrentText(str(cfg[role]))


class NGEControlGroup(QGroupBox):
    """
    DC control for one NGE role (flash-lamp control or filament power).

    Voltage / current-limit setpoints + Apply / Output ON / Output OFF, with a
    live measured V/I readback.  The NGE channel is read from the shared
    NGEChannelMap at call time.  All instrument I/O runs in short-lived daemon
    threads (NGE serial round-trips are ~0.2–0.5 s); results are marshalled back
    to the GUI thread through signals.
    """

    _status_ready = pyqtSignal(bool, str)
    _meas_ready = pyqtSignal(object)   # dict | None

    def __init__(self, title: str, role: str, nge_map: NGEChannelMap,
                 default_current_a: float = 0.1, default_voltage_v: float = 0.0,
                 parent=None):
        super().__init__(title, parent)
        self._role = role
        self._map = nge_map
        self._default_current = default_current_a
        self._default_voltage = default_voltage_v
        self._busy = False   # guard: at most one in-flight serial op per group
        self._build()
        self._status_ready.connect(self._on_status)
        self._meas_ready.connect(self._on_meas)
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start(2000)

    # -- UI ------------------------------------------------------------------

    def _build(self):
        g = QGridLayout(self)
        g.setColumnStretch(4, 1)
        row = 0

        self._assign_lbl = QLabel()
        self._assign_lbl.setStyleSheet(_HINT)
        g.addWidget(self._assign_lbl, row, 0, 1, 5)
        row += 1

        g.addWidget(QLabel("Voltage:"), row, 0, Qt.AlignRight)
        self._voltage = QDoubleSpinBox()
        self._voltage.setRange(0.0, 32.0)
        self._voltage.setDecimals(3)
        self._voltage.setValue(self._default_voltage)
        self._voltage.setSuffix(" V")
        self._voltage.setMinimumWidth(110)
        g.addWidget(self._voltage, row, 1)

        g.addWidget(QLabel("Current limit:"), row, 2, Qt.AlignRight)
        self._current = QDoubleSpinBox()
        self._current.setRange(0.0, 3.0)
        self._current.setDecimals(3)
        self._current.setValue(self._default_current)
        self._current.setSuffix(" A")
        self._current.setMinimumWidth(110)
        g.addWidget(self._current, row, 3)
        row += 1

        btn_row = QHBoxLayout()
        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(self._apply)
        btn_row.addWidget(apply_btn)
        on_btn = QPushButton("Output ON")
        on_btn.setStyleSheet(_GREEN)
        on_btn.clicked.connect(self._output_on)
        btn_row.addWidget(on_btn)
        off_btn = QPushButton("Output OFF")
        off_btn.setStyleSheet(_RED)
        off_btn.clicked.connect(self._output_off)
        btn_row.addWidget(off_btn)
        btn_row.addStretch()
        g.addLayout(btn_row, row, 0, 1, 5)
        row += 1

        self._meas_lbl = QLabel("measured: —")
        self._meas_lbl.setStyleSheet("color: gray;")
        g.addWidget(self._meas_lbl, row, 0, 1, 5)
        row += 1

        self._status = QLabel("—")
        self._status.setStyleSheet("color: gray;")
        g.addWidget(self._status, row, 0, 1, 5)

        self._refresh_assign_lbl()

    def _refresh_assign_lbl(self):
        self._assign_lbl.setText(f"Assigned: {self._map.assignment_str(self._role)}")

    # -- hardware helpers ----------------------------------------------------

    def _nge_ch(self):
        """(NGESupplyController | None, channel:int)."""
        return self._map.get_nge(), self._map.get_ch(self._role)

    def _run(self, fn):
        """Run fn() in a daemon thread; fn returns (ok, msg) via _status_ready.
        User actions take priority: they wait briefly for an in-flight poll to
        clear rather than being dropped."""
        def _work():
            for _ in range(50):            # up to ~1 s waiting for a poll to finish
                if not self._busy:
                    break
                time.sleep(0.02)
            self._busy = True
            try:
                ok, msg = fn()
            except Exception as e:
                ok, msg = False, f"{type(e).__name__}: {e}"
            finally:
                self._busy = False
            self._status_ready.emit(ok, msg)
        _threading.Thread(target=_work, daemon=True).start()

    def _apply(self):
        self._refresh_assign_lbl()
        v, i = self._voltage.value(), self._current.value()
        nge, ch = self._nge_ch()
        if nge is None or not nge.is_connected:
            self._on_status(False, "NGE not connected")
            return

        def _do():
            ok = nge.set_channel(ch, v, i)
            return ok, (f"set {v:.3f} V, {i:.3f} A limit on CH{ch}"
                        if ok else "set failed")
        self._run(_do)

    def _output_on(self):
        nge, ch = self._nge_ch()
        if nge is None or not nge.is_connected:
            self._on_status(False, "NGE not connected")
            return

        def _do():
            # Program the setpoint first, then enable the output.
            nge.set_channel(ch, self._voltage.value(), self._current.value())
            ok = nge.output_on(ch)
            return ok, (f"CH{ch} output ON" if ok else "output-on failed")
        self._run(_do)

    def _output_off(self):
        nge, ch = self._nge_ch()
        if nge is None or not nge.is_connected:
            self._on_status(False, "NGE not connected")
            return

        def _do():
            ok = nge.output_off(ch)
            return ok, (f"CH{ch} output OFF" if ok else "output-off failed")
        self._run(_do)

    def _poll(self):
        self._refresh_assign_lbl()
        nge, ch = self._nge_ch()
        if nge is None or not nge.is_connected:
            self._meas_ready.emit(None)
            return
        if self._busy:
            return   # a user action (or prior poll) is using the serial port

        def _work():
            self._busy = True
            try:
                m = nge.measure(ch)
                m["output_on"] = nge.is_output_on(ch)
            except Exception:
                m = None
            finally:
                self._busy = False
            self._meas_ready.emit(m)
        _threading.Thread(target=_work, daemon=True).start()

    # -- signal slots (GUI thread) ------------------------------------------

    def _on_status(self, ok: bool, msg: str):
        self._status.setText(msg)
        self._status.setStyleSheet("color: green;" if ok else "color: red;")

    def _on_meas(self, m):
        if not m:
            self._meas_lbl.setText("measured: — (NGE not connected)")
            self._meas_lbl.setStyleSheet("color: gray;")
            return
        v, i = m.get("voltage"), m.get("current")
        on = m.get("output_on")
        state = "ON" if on else "off"
        self._meas_lbl.setText(
            f"measured: {v:.3f} V, {i:.4f} A   [output {state}]"
            if v is not None else "measured: —"
        )
        self._meas_lbl.setStyleSheet(
            "color: #1565C0;" if on else "color: gray;"
        )

    # -- external control (FlashLampAdapter / experiment scripts) -----------

    def set_voltage(self, v: float):
        """Set the DC setpoint from code (thread-safe; commands hardware
        directly and does not toggle the output — turn the output on first)."""
        nge, ch = self._nge_ch()
        if nge is not None and nge.is_connected:
            nge.set_voltage(ch, v)

    def set_voltage_live(self, v: float):
        """Set the output voltage on hardware NOW on a background thread (for the
        filament power ramp — doesn't block the GUI).  Reflects in the UI too."""
        nge, ch = self._nge_ch()
        if nge is None or not nge.is_connected:
            return
        self._voltage.setValue(v)              # GUI thread
        cur = self._current.value()

        def _do():
            ok = nge.set_channel(ch, v, cur)
            return bool(ok), (f"CH{ch} → {v:.3f} V" if ok else "set failed")
        self._run(_do)

    def set_easyramp(self, duration_ms: float, enabled: bool = True):
        """Enable/disable the NGE EasyRamp soft-start (background thread)."""
        nge, ch = self._nge_ch()
        if nge is None or not nge.is_connected:
            return

        def _do():
            fn = getattr(nge, "set_easyramp", None)
            ok = fn(ch, duration_ms, enabled) if fn else False
            return bool(ok), ("EasyRamp " + ("on" if enabled else "off")
                              if ok else "EasyRamp unavailable")
        self._run(_do)

    def get_voltage(self) -> float:
        return self._voltage.value()

    @property
    def is_connected(self) -> bool:
        nge, _ = self._nge_ch()
        return nge is not None and nge.is_connected

    # -- config --------------------------------------------------------------

    def get_config(self) -> dict:
        return {"voltage": self._voltage.value(), "current": self._current.value()}

    def restore_config(self, cfg: dict):
        if "voltage" in cfg:
            self._voltage.setValue(float(cfg["voltage"]))
        if "current" in cfg:
            self._current.setValue(float(cfg["current"]))


# ---------------------------------------------------------------------------
# FlashLampAdapter
# Wraps flash_trigger (PulseGroup) + flash_control (NGEControlGroup) into a
# single object matching the FlashLampController interface expected by
# ChargeController and PhotonOrderExperiment.  The trigger is the gating
# actuator (enable/disable); the control voltage is an NGE setpoint.
# ---------------------------------------------------------------------------

class FlashLampAdapter:
    """Combines a trigger PulseGroup and an NGEControlGroup into a
    FlashLampController-compatible object.

    The flash trigger AFG serial I/O (enable = setup_pulse + output_on, disable =
    output_off) runs on a background _AfgActionQueue so the control loop never
    blocks on it — the same off-GUI-thread treatment as the filament."""

    def __init__(self, trigger: PulseGroup, control: NGEControlGroup):
        self._trigger = trigger
        self._control = control
        self._worker: "_AfgActionQueue | None" = None

    def _wk(self) -> "_AfgActionQueue":
        if self._worker is None:
            self._worker = _AfgActionQueue()
        if not self._worker.isRunning():
            self._worker.start()
        return self._worker

    def enable(self) -> bool:
        """Program the flash pulse (rate/width) + turn the output on, on the
        background worker.  Captures the settings on the calling thread and
        returns immediately."""
        trig = self._trigger
        afg, ch = trig._afg_ch()
        if afg is None:
            return False
        freq = trig._freq.value()
        width_s = trig._width_ms.value() * 1e-3
        amp = trig._amp.value()
        offset = trig._get_offset()
        hi_z = (trig._imp.currentIndex() == 1)

        def op():
            with _afg_lock(afg):
                (afg.set_load_high_z if hi_z else afg.set_load_50_ohm)(ch)
                afg.setup_pulse(ch, frequency=freq, amplitude=amp,
                                offset=offset, width=width_s)
                afg.output_on(ch)
        self._wk().submit(op)
        return True

    def disable(self) -> bool:
        """Turn the flash output off (ordered through the worker so it can't
        race a pending enable)."""
        afg, ch = self._trigger._afg_ch()
        if afg is None:
            return False

        def op():
            with _afg_lock(afg):
                afg.output_off(ch)
        if self._worker is not None and self._worker.isRunning():
            self._worker.submit(op)
        else:
            op()
        return True

    @property
    def is_connected(self) -> bool:
        return self._trigger.is_connected

    def arm(self):
        """Session on: turn the NGE flash-control output on at its setpoint, so
        the trigger pulses actually flash.  Called by ChargeController.start()."""
        self._control._output_on()

    def disarm(self):
        """Session off: turn the NGE flash-control output off."""
        self._control._output_off()

    def set_flash_rate(self, rate_hz: float):
        """Set the flash rate.  Applied on the next enable() (no immediate serial
        I/O), so it doesn't block; callers always enable() afterwards."""
        self._trigger._freq.setValue(rate_hz)

    def set_electrode_voltage(self, voltage_v: float):
        self._control.set_voltage(voltage_v)

    def get_flash_rate(self) -> float:
        return self._trigger._freq.value()

    def get_electrode_voltage(self) -> float:
        return self._control.get_voltage()

    def shutdown(self):
        """Stop the background worker (call on GUI close)."""
        if self._worker is not None:
            self._worker.shutdown()
            self._worker.wait(2000)
            self._worker = None


# ---------------------------------------------------------------------------
# Single-pulse firing (pulse-wait-read ramp) — background thread
# ---------------------------------------------------------------------------

# Very-low-frequency carrier for single-pulse firing.  At this rate the period
# is ~1000 s, so the *next* pulse is ~1000 s away: enabling the output fires
# exactly ONE hardware-timed pulse of the requested width, and the caller turns
# the output off shortly after — long before any second pulse could arrive.
# This gives an accurate pulse WIDTH (AFG-timed) with no burst/single-shot mode
# in the driver.  (User's "belt and suspenders" 1 mHz design.)
FILAMENT_PULSE_CARRIER_HZ = 0.001


class _FilamentPulser(QThread):
    """Background thread that owns the filament trigger AFG's serial I/O.

    Each AFG write blocks ~80 ms (a *synced* apply is ~230 ms).  Firing the
    pulse-wait-read ramp on the GUI thread — reprogramming the whole waveform
    (setup_pulse) every step — meant ~0.6 s of blocking I/O per step on the GUI
    thread, which froze/crashed the UI.  This thread fixes that two ways:

      1. It runs OFF the GUI thread, so the control loop never blocks; fire()/
         off() just enqueue a request and return immediately.
      2. It sets the pulse up ONCE per channel, then per fire only changes the
         WIDTH when it actually changes (one cheap ``SOUR:PULS:WIDT`` write) and
         toggles the output — no per-step full reprogram.

    Requests are executed under the shared per-AFG lock.  Under backlog it skips
    to the most recent request (the ramp has advanced), so it can't lag the loop.
    """

    def __init__(self, carrier_hz: float = FILAMENT_PULSE_CARRIER_HZ):
        super().__init__()
        self._carrier = carrier_hz
        self._q: "queue.Queue" = queue.Queue()
        self._key = None            # (id(afg), ch) the pulse is set up for
        self._cur_width = None      # width_ms last programmed
        self.setObjectName("FilamentPulser")

    # -- GUI thread: capture handles + enqueue (non-blocking) ----------------

    def fire(self, afg, ch, amp, offset, hi_z, width_ms):
        self._q.put(("fire", (afg, ch, amp, offset, hi_z, float(width_ms))))

    def hold(self, afg, ch, dc_level, hi_z):
        """Hold the output at a constant DC ``dc_level`` so the SSR stays closed."""
        self._q.put(("hold", (afg, ch, dc_level, hi_z)))

    def off(self, afg, ch):
        self._q.put(("off", (afg, ch)))

    def shutdown(self):
        self._q.put(("quit", None))

    # -- worker thread -------------------------------------------------------

    def run(self):
        while True:
            op, payload = self._q.get()
            # Coalesce a backlog to the most recent request so the pulser never
            # lags the control loop (a newer fire = ramp advanced; an off/quit
            # supersedes pending fires).
            while True:
                try:
                    op, payload = self._q.get_nowait()
                except queue.Empty:
                    break
            if op == "quit":
                break
            try:
                if op == "fire":
                    self._fire(*payload)
                elif op == "hold":
                    self._hold(*payload)
                elif op == "off":
                    afg, ch = payload
                    with _afg_lock(afg):
                        afg.output_off(ch)
            except Exception:
                pass

    def _hold(self, afg, ch, dc_level, hi_z):
        """Hold the SSR closed with a constant DC output (the AFG's DC offset —
        a ~0-amplitude sine at ``dc_level``, as the old flash-control DC used)."""
        with _afg_lock(afg):
            (afg.set_load_high_z if hi_z else afg.set_load_50_ohm)(ch)
            afg.setup_sine(ch, frequency=1.0, amplitude=0.001, offset=dc_level)
            afg.output_on(ch)
        self._key = None          # invalidate the single-pulse setup cache
        self._cur_width = None

    def _fire(self, afg, ch, amp, offset, hi_z, width_ms):
        width_s = max(width_ms, 0.0) * 1e-3
        with _afg_lock(afg):
            key = (id(afg), ch)
            if key != self._key or self._cur_width is None:
                # First fire on this channel: one-time full setup.
                (afg.set_load_high_z if hi_z else afg.set_load_50_ohm)(ch)
                afg.setup_pulse(ch, frequency=self._carrier, amplitude=amp,
                                offset=offset, width=width_s)
                self._key = key
                self._cur_width = width_ms
            elif width_ms != self._cur_width:
                # Only the width changed: one cheap write, no full reprogram.
                wf = getattr(afg, "waveform", None)
                if wf is not None:
                    wf.set_pulse_width(ch, width_s)
                else:
                    afg.setup_pulse(ch, frequency=self._carrier, amplitude=amp,
                                    offset=offset, width=width_s)
                self._cur_width = width_ms
            afg.output_off(ch)   # reset so output_on restarts the pulse at phase 0
            afg.output_on(ch)    # fires one pulse of width_ms

    def reset(self):
        """Forget the cached setup so the next fire re-programs (e.g. after the
        channel/amplitude changed)."""
        self._key = None
        self._cur_width = None


class _AfgActionQueue(QThread):
    """Runs submitted callables (each does AFG serial I/O) on a worker thread so
    the caller never blocks on the ~80 ms-per-write serial port.  Used by the
    flash lamp (continuous enable/disable) — see FlashLampAdapter."""

    def __init__(self):
        super().__init__()
        self._q: "queue.Queue" = queue.Queue()

    def submit(self, fn):
        self._q.put(fn)

    def shutdown(self):
        self._q.put(None)

    def run(self):
        while True:
            fn = self._q.get()
            if fn is None:
                break
            try:
                fn()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# FilamentAdapter
# Wraps the filament trigger (PulseGroup, WG3-CH2) + the filament power
# (NGEControlGroup) into a single actuator matching the enable/disable +
# arm/disarm interface ChargeController uses.  The trigger gates heating; the
# NGE power is the session-on DC supply.
# ---------------------------------------------------------------------------

class FilamentAdapter:
    """Combines the filament trigger PulseGroup and its NGE power group.

    Pulse-wait-read fires go through a background _FilamentPulser so the control
    loop never blocks on the AFG serial port (see _FilamentPulser)."""

    PULSE_CARRIER_HZ = FILAMENT_PULSE_CARRIER_HZ

    def __init__(self, trigger: PulseGroup, power: NGEControlGroup):
        self._trigger = trigger
        self._power = power
        self._pulser: "_FilamentPulser | None" = None

    def _ensure_pulser(self) -> "_FilamentPulser":
        if self._pulser is None:
            self._pulser = _FilamentPulser(self.PULSE_CARRIER_HZ)
        if not self._pulser.isRunning():
            self._pulser.start()
        return self._pulser

    def enable(self) -> bool:
        return self._trigger.enable()

    def disable(self) -> bool:
        """Turn the trigger output off.  Routed through the pulser (when one is
        running) so it is ordered AFTER any pending fires — otherwise a queued
        fire could re-enable the output right after a stop."""
        afg, ch = self._trigger._afg_ch()
        if afg is None:
            return False
        if self._pulser is not None and self._pulser.isRunning():
            self._pulser.off(afg, ch)
            return True
        with _afg_lock(afg):
            return afg.output_off(ch)

    @property
    def is_connected(self) -> bool:
        return self._trigger.is_connected

    def arm(self):
        """Session on: turn the NGE filament-power output on at its setpoint."""
        self._power._output_on()

    def disarm(self):
        """Session off: turn the NGE filament-power output off."""
        self._power._output_off()

    def set_pulse(self, freq_hz: float, width_ms: float) -> bool:
        """Program the filament trigger pulse (freq + width) and keep it running.
        Kept for manual/continuous use; call from the GUI thread."""
        self._trigger._freq.setValue(freq_hz)
        self._trigger._width_ms.setValue(width_ms)
        return self._trigger.enable()   # re-programs at the new freq/width, output on

    def fire_pulse(self, width_ms: float) -> bool:
        """Fire ONE filament pulse of the given width (pulse-wait-read ramp).
        Non-blocking: captures the AFG handle on the GUI thread and hands the
        serial I/O to the background pulser."""
        trig = self._trigger
        afg, ch = trig._afg_ch()
        if afg is None:
            return False
        hi_z = (trig._imp.currentIndex() == 1)
        self._ensure_pulser().fire(afg, ch, trig._amp.value(),
                                   trig._get_offset(), hi_z, width_ms)
        return True

    def pulse_off(self) -> bool:
        """Turn the filament trigger output off (ends the single-pulse window /
        opens the SSR).  Non-blocking (ordered through the pulser)."""
        afg, ch = self._trigger._afg_ch()
        if afg is None:
            return False
        if self._pulser is not None:
            self._pulser.off(afg, ch)
        return True

    # -- power-ramp mode: hold the SSR closed + ramp the NGE voltage ---------

    def hold_ssr_on(self) -> bool:
        """Hold the filament trigger at a constant DC high (SSR closed) so the
        NGE power can be ramped continuously.  Uses the AFG's DC offset at the
        pulse HIGH level.  Non-blocking (background pulser)."""
        trig = self._trigger
        afg, ch = trig._afg_ch()
        if afg is None:
            return False
        hi_z = (trig._imp.currentIndex() == 1)
        dc_level = trig._get_offset() + trig._amp.value() / 2.0   # pulse HIGH level
        self._ensure_pulser().hold(afg, ch, dc_level, hi_z)
        return True

    def set_power_voltage(self, v: float) -> bool:
        """Set the filament power-supply (NGE) voltage now (background thread)."""
        self._power.set_voltage_live(v)
        return True

    def set_power_easyramp(self, duration_ms: float, enabled: bool = True) -> bool:
        """Enable the NGE EasyRamp soft-start for smooth voltage steps."""
        self._power.set_easyramp(duration_ms, enabled)
        return True

    def shutdown(self):
        """Stop the background pulser (call on GUI close)."""
        if self._pulser is not None:
            self._pulser.shutdown()
            self._pulser.wait(2000)
            self._pulser = None


# ---------------------------------------------------------------------------
# Filament ramp — reusable config editor + manual (out-of-loop) runner
# ---------------------------------------------------------------------------

class CycleLog(QTextEdit):
    """Grayed-out, read-only diagnostic log of the last few pulse-ramp read
    cycles: pulse width and Δcharge added since the previous read.

    The filament tends to charge all at once, so this history is how you tune the
    ramp: a Δq in the *wrong* direction means "waited too few cycles" (read before
    the charge moved), and Δq ≈ 0 up to some width means "charging doesn't start
    until ~that width" (so start there next time to cut deadtime).  Reused for the
    flash lamp (charge removed per read) too."""

    def __init__(self, maxlines: int = 5, empty: str = "(no cycles yet)", parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumHeight(24 + 15 * maxlines)
        self.setStyleSheet("color: gray; font-family: monospace; font-size: 11px;")
        self._empty = empty
        self._lines: "deque[str]" = deque(maxlen=maxlines)
        self._render()

    def clear(self):
        self._lines.clear(); self._render()

    def add(self, cyc):
        """Append a RampCycle (ramped value + unit, Δq, charge, met)."""
        unit = getattr(cyc, "unit", "ms")
        tag = "V" if unit == "V" else "w"
        self._lines.append(
            f"{tag}={cyc.width_ms:6.3g} {unit:<2}  Δq={cyc.delta_q:+6.2f} e   "
            f"q={cyc.charge:+6.1f} e{'   ✓ target' if cyc.met else ''}"
        )
        self._render()

    def add_text(self, line: str):
        self._lines.append(line); self._render()

    def _render(self):
        self.setPlainText("\n".join(self._lines) if self._lines else self._empty)


class FilamentRampConfig(QWidget):
    """
    Reusable editor for a pulse-wait-read FilamentRamp.  Used both by the Control
    tab (drives the ChargeController's in-loop ramp) and by the Filament tab's
    manual runner (out-of-loop play).

    The ramp fires ONE pulse of the current width, waits ``timeout_cycles`` read
    cycles with the filament OFF so the lock-in settles, reads the charge and
    evaluates the stop condition, then (if not met) increments the pulse width
    and fires again.  The read is clean because the filament is off — immune to
    the heating noise.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    _MODES = [("pulse", "Pulse the SSR (pulse-wait-read)"),
              ("power", "Ramp the power supply (SSR held on)")]

    def _build(self):
        g = QGridLayout(self)
        g.setContentsMargins(0, 0, 0, 0)

        self._enable = QCheckBox("Filament ramp")
        self._enable.setToolTip(
            "Ramp the filament until the target/Δq is reached.  Pick the method\n"
            "with the mode selector; off = the loop uses a fixed heat pulse.")
        g.addWidget(self._enable, 0, 0, 1, 2)
        g.addWidget(QLabel("Mode:"), 0, 2, Qt.AlignRight)
        self._mode = QComboBox()
        for _, label in self._MODES:
            self._mode.addItem(label)
        self._mode.setToolTip(
            "Pulse: fire one SSR pulse, wait N clean (filament-off) reads, read,\n"
            "increment the pulse width.  Power: hold the SSR closed and ramp the\n"
            "NGE filament-power voltage — smoother, may not need the wait.")
        self._mode.currentIndexChanged.connect(self._on_mode)
        g.addWidget(self._mode, 0, 3)

        # -- pulse-mode params --
        self._pl_start = QLabel("Start width:"); g.addWidget(self._pl_start, 1, 0, Qt.AlignRight)
        self._start = self._spin(0.001, 1e5, 3, 5.0, " ms")
        self._start.setToolTip("Width of the first pulse (SSR minimum ~5 ms).")
        g.addWidget(self._start, 1, 1)
        self._pl_inc = QLabel("Increment:"); g.addWidget(self._pl_inc, 1, 2, Qt.AlignRight)
        self._inc = self._spin(0.001, 1e5, 3, 5.0, " ms")
        g.addWidget(self._inc, 1, 3)
        self._pl_max = QLabel("Max width:"); g.addWidget(self._pl_max, 2, 0, Qt.AlignRight)
        self._max = self._spin(0.001, 1e5, 3, 200.0, " ms")
        self._max.setToolTip("Pulse width is clamped here (safety ceiling).")
        g.addWidget(self._max, 2, 1)

        # -- power-mode params --
        self._pw_start = QLabel("Start voltage:"); g.addWidget(self._pw_start, 3, 0, Qt.AlignRight)
        self._start_v = self._spin(0.0, 32.0, 3, 1.0, " V")
        self._start_v.setToolTip("Filament power-supply voltage at the first step.")
        g.addWidget(self._start_v, 3, 1)
        self._pw_inc = QLabel("Increment:"); g.addWidget(self._pw_inc, 3, 2, Qt.AlignRight)
        self._inc_v = self._spin(0.001, 32.0, 3, 0.5, " V")
        g.addWidget(self._inc_v, 3, 3)
        self._pw_max = QLabel("Max voltage:"); g.addWidget(self._pw_max, 4, 0, Qt.AlignRight)
        self._max_v = self._spin(0.0, 32.0, 3, 5.0, " V")
        self._max_v.setToolTip("Power voltage is clamped here (safety ceiling).")
        g.addWidget(self._max_v, 4, 1)
        self._pw_er = QLabel("EasyRamp:"); g.addWidget(self._pw_er, 4, 2, Qt.AlignRight)
        self._easyramp = self._spin(0.0, 10000.0, 0, 0.0, " ms")
        self._easyramp.setToolTip(
            "NGE EasyRamp soft-start per voltage step (0 = step jumps; e.g. 500 ms\n"
            "= each step ramps smoothly).")
        g.addWidget(self._easyramp, 4, 3)

        # -- shared --
        g.addWidget(QLabel("Timeout cycles:"), 5, 0, Qt.AlignRight)
        self._cycles = QSpinBox(); self._cycles.setRange(1, 1000)
        self._cycles.setValue(6); self._cycles.setMaximumWidth(100)
        self._cycles.setToolTip(
            "Read cycles to wait before reading.  Pulse mode: the filament is off\n"
            "during the wait (clean read).  Power mode: a smooth ramp may not need\n"
            "a wait — set 1 to read every step.")
        g.addWidget(self._cycles, 5, 1)
        g.setColumnStretch(4, 1)
        self._on_mode()

    def _on_mode(self, *args):
        power = self._MODES[self._mode.currentIndex()][0] == "power"
        for w in (self._pl_start, self._start, self._pl_inc, self._inc,
                  self._pl_max, self._max):
            w.setVisible(not power)
        for w in (self._pw_start, self._start_v, self._pw_inc, self._inc_v,
                  self._pw_max, self._max_v, self._pw_er, self._easyramp):
            w.setVisible(power)

    @staticmethod
    def _spin(lo, hi, dec, val, suffix):
        s = QDoubleSpinBox(); s.setRange(lo, hi); s.setDecimals(dec)
        s.setValue(val); s.setSuffix(suffix); s.setMaximumWidth(100)
        return s

    def get_ramp(self):
        from charge_control import FilamentRamp
        return FilamentRamp(
            enabled=self._enable.isChecked(),
            mode=self._MODES[self._mode.currentIndex()][0],
            start_width_ms=self._start.value(),
            increment_ms=self._inc.value(),
            max_width_ms=self._max.value(),
            start_v=self._start_v.value(),
            increment_v=self._inc_v.value(),
            max_v=self._max_v.value(),
            easyramp_ms=self._easyramp.value(),
            timeout_cycles=self._cycles.value(),
        )

    def get_config(self) -> dict:
        return {
            "enabled": self._enable.isChecked(),
            "mode": self._MODES[self._mode.currentIndex()][0],
            "start_width_ms": self._start.value(),
            "increment_ms": self._inc.value(),
            "max_width_ms": self._max.value(),
            "start_v": self._start_v.value(),
            "increment_v": self._inc_v.value(),
            "max_v": self._max_v.value(),
            "easyramp_ms": self._easyramp.value(),
            "timeout_cycles": self._cycles.value(),
        }

    def restore_config(self, cfg: dict):
        if "enabled" in cfg:
            self._enable.setChecked(bool(cfg["enabled"]))
        mode = cfg.get("mode")
        keys = [k for k, _ in self._MODES]
        if mode in keys:
            self._mode.setCurrentIndex(keys.index(mode))
        for key, spin in (("start_width_ms", self._start),
                          ("increment_ms", self._inc),
                          ("max_width_ms", self._max),
                          ("start_v", self._start_v),
                          ("increment_v", self._inc_v),
                          ("max_v", self._max_v),
                          ("easyramp_ms", self._easyramp)):
            if key in cfg:
                spin.setValue(float(cfg[key]))
        if "timeout_cycles" in cfg:
            self._cycles.setValue(int(cfg["timeout_cycles"]))
        self._on_mode()


class FilamentRampWidget(QGroupBox):
    """
    Manual (out-of-loop) pulse-wait-read filament ramp runner for the Filament
    tab: play with the ramp without the charge loop.  A timer stands in for the
    lock-in read cycle and drives the same pulse → wait N cycles → increment
    sequence the control loop uses, so you can watch the filament and find good
    settings.  With no charge feedback it just walks the width from start to max
    (then stops).
    """

    _TICK_MS = 33   # nominal read-cycle period for the manual clock (~30 Hz)

    def __init__(self, get_filament, parent=None):
        super().__init__("Filament ramp (manual)", parent)
        self._get_filament = get_filament     # -> PulseGroup
        self._runner = None
        self._ramp = None
        self._pulser = None                   # background _FilamentPulser
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._build()

    def _fire(self, fil, width_ms):
        """Dispatch one pulse to the background pulser (non-blocking)."""
        afg, ch = fil._afg_ch()
        if afg is None:
            return
        if self._pulser is None:
            self._pulser = _FilamentPulser()
        if not self._pulser.isRunning():
            self._pulser.start()
        hi_z = (fil._imp.currentIndex() == 1)
        self._pulser.fire(afg, ch, fil._amp.value(), fil._get_offset(), hi_z, width_ms)

    def _off(self, fil):
        afg, ch = fil._afg_ch()
        if afg is not None and self._pulser is not None:
            self._pulser.off(afg, ch)

    def _build(self):
        v = QVBoxLayout(self)
        self._cfg = FilamentRampConfig()
        v.addWidget(self._cfg)
        row = QHBoxLayout()
        self._start_btn = QPushButton("Start ramp"); self._start_btn.setStyleSheet(_GREEN)
        self._start_btn.clicked.connect(self._start)
        row.addWidget(self._start_btn)
        self._stop_btn = QPushButton("Stop"); self._stop_btn.setStyleSheet(_RED)
        self._stop_btn.setEnabled(False); self._stop_btn.clicked.connect(self._stop)
        row.addWidget(self._stop_btn)
        self._status = QLabel("—"); self._status.setStyleSheet("color: gray;")
        row.addWidget(self._status); row.addStretch()
        v.addLayout(row)
        self._log = CycleLog()
        v.addWidget(self._log)

    def _start(self):
        from charge_control import PulseRampRunner
        self._ramp = self._cfg.get_ramp()
        if not self._ramp.enabled:
            self._status.setText("Tick the filament-ramp box first")
            self._status.setStyleSheet("color: #C62828;")
            return
        if self._ramp.mode == "power":
            # This manual runner only drives the SSR trigger — it has no NGE
            # power handle.  Run the power ramp from the Control tab.
            self._status.setText("Power mode: run it from the Control tab")
            self._status.setStyleSheet("color: #C62828;")
            return
        fil = self._get_filament()
        if fil is None or not getattr(fil, "is_connected", False):
            self._status.setText("Filament WG not connected")
            self._status.setStyleSheet("color: #C62828;")
            return
        # No charge feedback in manual mode: never "met", just walk to max.
        self._runner = PulseRampRunner(self._ramp, condition=lambda q: False)
        self._log.clear()
        self._start_btn.setEnabled(False); self._stop_btn.setEnabled(True)
        self._timer.start(self._TICK_MS)

    def _tick(self):
        runner = self._runner
        fil = self._get_filament()
        if runner is None or fil is None:
            self._stop(); return
        r = runner.step(0.0)
        try:
            if r["off"]:
                self._off(fil)
            if r["fire"] is not None:
                self._fire(fil, r["fire"])
                self._status.setText(f"pulse {r['fire']:.4g} ms")
                self._status.setStyleSheet("color: #1565C0;")
        except Exception as e:
            self._status.setText(f"Error: {e}"); self._stop(); return
        cyc = r["cycle"]
        if cyc is not None:
            self._log.add(cyc)
            if cyc.width_ms >= self._ramp.max_width_ms:
                self._status.setText(f"Reached max width ({cyc.width_ms:.4g} ms) — stopped")
                self._status.setStyleSheet("color: gray;")
                self._stop(keep_status=True)

    def _stop(self, keep_status: bool = False):
        self._timer.stop()
        self._runner = None
        fil = self._get_filament()
        if fil is not None:
            try:
                self._off(fil)
            except Exception:
                pass
        if self._pulser is not None:
            self._pulser.shutdown()
            self._pulser.wait(2000)
            self._pulser = None
        self._start_btn.setEnabled(True); self._stop_btn.setEnabled(False)
        if not keep_status:
            self._status.setText("Stopped"); self._status.setStyleSheet("color: gray;")

    def get_config(self) -> dict:
        return self._cfg.get_config()

    def restore_config(self, cfg: dict):
        self._cfg.restore_config(cfg)


# ---------------------------------------------------------------------------
# Lock-in reference — a WG channel that mirrors a drive channel
# ---------------------------------------------------------------------------

class LockInReferenceGroup(QGroupBox):
    """
    Lock-in reference output — a WG channel that mirrors the drive on the
    opposite channel of the same WG.

    It is a *true mirror*: the reference copies the mirrored drive's waveform
    type (sine/square/pulse/ramp/noise), frequency and shape, and is
    phase-locked to it (AFG ``sync_phases``).  The only user knob is the
    reference amplitude — held fixed so the SR530 keeps its reference lock even
    when the drive amplitude is reduced (e.g. during charging).

    Assignment lives here (the WG analogue of the electrode / NGE channel
    maps): pick the reference WG/CH and the drive WG/CH it mirrors.  A
    phase-locked mirror is only possible between the two channels of one AFG,
    so the reference must be the opposite channel of the same WG as its drive —
    enforced with a validation note.

    Re-mirrors automatically whenever that drive channel is (re)applied, and
    follows the drive's output on/off (turning the drive off turns the
    reference off; turning it on syncs and enables the reference); press Sync
    (or Output ON) after changing the reference amplitude.
    """

    _status_ready = pyqtSignal(bool, str)
    _info_ready = pyqtSignal(str)

    _DEFAULT_REF = ("WG1", "CH2")
    _DEFAULT_MIRROR = ("WG1", "CH1")

    def __init__(self, get_afg, electrode_map=None, parent=None):
        super().__init__("Lock-in Reference", parent)
        self._get_afg = get_afg
        self._electrode_map = electrode_map
        self._busy = False
        self._build()
        self._status_ready.connect(self._on_status)
        self._info_ready.connect(lambda t: self._info.setText(t))

    # -- UI ------------------------------------------------------------------

    def _build(self):
        g = QGridLayout(self)
        g.setColumnStretch(5, 1)
        row = 0

        g.addWidget(QLabel("Reference output:"), row, 0, Qt.AlignRight)
        self._ref_wg = QComboBox(); self._ref_wg.addItems(_WG_OPTIONS)
        self._ref_wg.setCurrentText(self._DEFAULT_REF[0]); self._ref_wg.setMaximumWidth(70)
        self._ref_ch = QComboBox(); self._ref_ch.addItems(_CH_OPTIONS)
        self._ref_ch.setCurrentText(self._DEFAULT_REF[1]); self._ref_ch.setMaximumWidth(70)
        g.addWidget(self._ref_wg, row, 1); g.addWidget(self._ref_ch, row, 2)
        row += 1

        g.addWidget(QLabel("Mirrors drive:"), row, 0, Qt.AlignRight)
        self._mir_wg = QComboBox(); self._mir_wg.addItems(_WG_OPTIONS)
        self._mir_wg.setCurrentText(self._DEFAULT_MIRROR[0]); self._mir_wg.setMaximumWidth(70)
        self._mir_ch = QComboBox(); self._mir_ch.addItems(_CH_OPTIONS)
        self._mir_ch.setCurrentText(self._DEFAULT_MIRROR[1]); self._mir_ch.setMaximumWidth(70)
        g.addWidget(self._mir_wg, row, 1); g.addWidget(self._mir_ch, row, 2)
        self._valid_lbl = QLabel(); g.addWidget(self._valid_lbl, row, 3, 1, 2)
        row += 1

        g.addWidget(QLabel("Amplitude:"), row, 0, Qt.AlignRight)
        self._amp = QDoubleSpinBox(); self._amp.setRange(0.001, 10.0)
        self._amp.setDecimals(3); self._amp.setValue(8.0); self._amp.setSuffix(" Vpp")
        self._amp.setMinimumWidth(110)
        g.addWidget(self._amp, row, 1, 1, 2)
        row += 1

        btn = QHBoxLayout()
        sync_btn = QPushButton("Sync now"); sync_btn.clicked.connect(self.sync_reference)
        btn.addWidget(sync_btn)
        on_btn = QPushButton("Output ON"); on_btn.setStyleSheet(_GREEN)
        on_btn.clicked.connect(self._output_on); btn.addWidget(on_btn)
        off_btn = QPushButton("Output OFF"); off_btn.setStyleSheet(_RED)
        off_btn.clicked.connect(self._output_off); btn.addWidget(off_btn)
        btn.addStretch()
        g.addLayout(btn, row, 0, 1, 6)
        row += 1

        self._status = QLabel("—"); self._status.setStyleSheet("color: gray;")
        g.addWidget(self._status, row, 0, 1, 6); row += 1
        self._info = QLabel("mirroring: —"); self._info.setStyleSheet("color: gray;")
        g.addWidget(self._info, row, 0, 1, 6); row += 1

        note = QLabel(
            "The reference mirrors the drive on the opposite channel of the same "
            "WG — same waveform type,\nfrequency and phase (phase-locked). Only "
            "the amplitude is set here (kept fixed so the lock-in\nstays locked). "
            "Re-syncs when that drive is applied, and follows its output on/off "
            "(drive off → reference off)."
        )
        note.setStyleSheet(_HINT)
        g.addWidget(note, row, 0, 1, 6)

        self._ref_wg.currentIndexChanged.connect(self._on_ref_changed)
        self._ref_ch.currentIndexChanged.connect(self._on_ref_changed)
        self._mir_wg.currentIndexChanged.connect(self._validate)
        self._mir_ch.currentIndexChanged.connect(self._validate)
        self._validate()

    def _on_ref_changed(self):
        # Auto-suggest the mirror = same WG, opposite CH (the only valid choice).
        self._mir_wg.blockSignals(True); self._mir_ch.blockSignals(True)
        self._mir_wg.setCurrentText(self._ref_wg.currentText())
        self._mir_ch.setCurrentText("CH1" if self._ref_ch.currentText() == "CH2" else "CH2")
        self._mir_wg.blockSignals(False); self._mir_ch.blockSignals(False)
        self._validate()

    def _is_valid(self) -> bool:
        return (self._ref_wg.currentText() == self._mir_wg.currentText()
                and self._ref_ch.currentText() != self._mir_ch.currentText())

    def _drive_name(self) -> str:
        wg, ch = self._mir_wg.currentText(), self._mir_ch.currentText()
        if self._electrode_map is not None:
            for axis in ("x", "y", "z"):
                if (self._electrode_map._wg[axis].currentText() == wg
                        and self._electrode_map._ch[axis].currentText() == ch):
                    return f"{wg}-{ch} = {axis.upper()} drive"
        return f"{wg}-{ch}"

    def _validate(self):
        if self._is_valid():
            self._valid_lbl.setText(f"✓ {self._drive_name()}")
            self._valid_lbl.setStyleSheet("color: green;")
        else:
            self._valid_lbl.setText("⚠ must be the opposite CH on the same WG")
            self._valid_lbl.setStyleSheet("color: #C62828;")

    # -- hardware ------------------------------------------------------------

    def _program(self, afg, ref_ch: int, mir_ch: int, amp: float):
        """Mirror the mirror-channel waveform onto ref_ch at *amp* and
        phase-lock.  Caller holds the AFG serial lock.  Returns (ok, msg)."""
        wf = (afg.get_waveform_type(mir_ch) or "").upper()
        freq = afg.get_frequency(mir_ch)
        if freq is None:
            return False, "cannot read mirrored channel"
        if "SIN" in wf:
            afg.setup_sine(ref_ch, freq, amp, 0.0); shape = "Sine"
        elif "SQU" in wf:
            afg.setup_square(ref_ch, freq, amp, 0.0,
                             duty_cycle=afg.waveform.get_square_duty_cycle(mir_ch))
            shape = "Square"
        elif "PULS" in wf:
            afg.setup_pulse(ref_ch, freq, amp, 0.0,
                            width=afg.waveform.get_pulse_width(mir_ch))
            shape = "Pulse"
        elif "RAMP" in wf:
            afg.setup_ramp(ref_ch, freq, amp, 0.0,
                           symmetry=afg.waveform.get_ramp_symmetry(mir_ch))
            shape = "Ramp"
        elif "NOIS" in wf:
            afg.setup_noise(ref_ch, amp, 0.0); shape = "Noise"
        else:
            return False, f"cannot mirror '{wf}' (e.g. ARB / comb) — set reference manually"
        afg.waveform.sync_phases()
        return True, f"{shape} @ {freq:.4g} Hz"

    def _resolve(self):
        """(afg, ref_ch, mir_ch, amp) or (None, reason)."""
        if not self._is_valid():
            return None, "invalid mirror (opposite CH, same WG)"
        afg = self._get_afg(self._ref_wg.currentIndex() + 1)
        if afg is None or not afg.is_connected:
            return None, f"{self._ref_wg.currentText()} not connected"
        return afg, (self._ref_ch.currentIndex() + 1,
                     self._mir_ch.currentIndex() + 1, self._amp.value())

    def _run(self, worker):
        def _job():
            for _ in range(50):
                if not self._busy:
                    break
                time.sleep(0.02)
            self._busy = True
            try:
                worker()
            finally:
                self._busy = False
        _threading.Thread(target=_job, daemon=True).start()

    def sync_reference(self, enable: bool = False):
        afg, rest = self._resolve()
        if afg is None:
            self._on_status(False, rest)
            return
        ref_ch, mir_ch, amp = rest

        def _work():
            try:
                with _afg_lock(afg):
                    ok, msg = self._program(afg, ref_ch, mir_ch, amp)
                    if ok and enable:
                        afg.output_on(ref_ch)
            except Exception as e:
                ok, msg = False, f"{type(e).__name__}: {e}"
            if ok:
                self._status_ready.emit(
                    True, ("output ON — " if enable else "synced — ") + msg)
                self._info_ready.emit(
                    f"mirroring CH{mir_ch}: {msg} at {amp:.3g} Vpp (phase-locked)")
            else:
                self._status_ready.emit(False, msg)
        self._run(_work)

    def _output_on(self):
        self.sync_reference(enable=True)

    def _output_off(self):
        afg, rest = self._resolve()
        if afg is None:
            self._on_status(False, rest)
            return
        ref_ch = rest[0]

        def _work():
            try:
                with _afg_lock(afg):
                    ok = afg.output_off(ref_ch)
            except Exception as e:
                ok = False
                self._status_ready.emit(False, f"{type(e).__name__}: {e}")
                return
            self._status_ready.emit(bool(ok), "output OFF" if ok else "output-off failed")
        self._run(_work)

    def notify_drive_applied(self, wg_n: int, ch: int):
        """Called when a drive channel is applied; re-mirror if it is ours."""
        if self._mirrors(wg_n, ch):
            self.sync_reference(enable=False)

    def notify_drive_output(self, wg_n: int, ch: int, is_on: bool):
        """Called when a drive channel's output is toggled; mirror the state
        onto the reference (drive off -> reference off, drive on -> sync +
        reference on) if that channel is the one we mirror."""
        if not self._mirrors(wg_n, ch):
            return
        if is_on:
            self.sync_reference(enable=True)
        else:
            self._output_off()

    def _mirrors(self, wg_n: int, ch: int) -> bool:
        return (self._is_valid()
                and wg_n == self._mir_wg.currentIndex() + 1
                and ch == self._mir_ch.currentIndex() + 1)

    # -- signal slots --------------------------------------------------------

    def _on_status(self, ok: bool, msg: str):
        self._status.setText(msg)
        self._status.setStyleSheet("color: green;" if ok else "color: red;")

    # -- config --------------------------------------------------------------

    def get_config(self) -> dict:
        return {
            "ref_wg": self._ref_wg.currentText(),
            "ref_ch": self._ref_ch.currentText(),
            "mirror_wg": self._mir_wg.currentText(),
            "mirror_ch": self._mir_ch.currentText(),
            "amplitude": self._amp.value(),
        }

    def restore_config(self, cfg: dict):
        for key, wg, ch in (("ref", self._ref_wg, self._ref_ch),
                            ("mirror", self._mir_wg, self._mir_ch)):
            if f"{key}_wg" in cfg:
                wg.setCurrentText(str(cfg[f"{key}_wg"]))
            if f"{key}_ch" in cfg:
                ch.setCurrentText(str(cfg[f"{key}_ch"]))
        if "amplitude" in cfg:
            self._amp.setValue(float(cfg["amplitude"]))
        self._validate()


# ---------------------------------------------------------------------------
# Drive setback — park the drive tone at a low amplitude while charging
# ---------------------------------------------------------------------------

class DriveSetbackAdapter:
    """
    Actuator wrapper that parks the electrode drive tone at a low amplitude
    while the wrapped actuator (the filament) is on.

    enable():  read the setback params; if enabled, drop the monitored axis'
               drive to the charging amplitude *before* enabling the actuator
               (hardware only — the drive widget's amplitude spinbox keeps
               the measurement setpoint) and report the new absolute drive
               amplitude via on_drive_amp(charging_vpp).  If the actuator then
               fails to enable, the drive is restored immediately.
    disable(): disable the wrapped actuator *first* (the field comes back up
               only once the electron source is off), then restore the drive
               to the amplitude captured at reduce time and report it via
               on_drive_amp(measure_vpp).

    Reduce/restore are idempotent, so ChargeController's
    stop-everything-before-acting pattern (which calls disable() on inactive
    actuators) is safe.  Wire on_drive_amp to AnalysisTab.set_current_drive_amp
    so the lock-in charge readout stays normalized while the drive is reduced.
    effective_amplitude() gives the current actual drive amplitude for a poller
    to push periodically (catches manual drive-amplitude changes too).

    If the drive AFG is disconnected mid-actuation the restore is skipped on
    hardware (nothing to talk to) but the scale is still reset — re-Apply the
    drive channel after reconnecting.
    """

    def __init__(self, actuator, get_drive_widget, get_params, on_drive_amp=None):
        self._actuator = actuator
        self._get_drive = get_drive_widget   # -> ChannelControlWidget
        # -> {"enabled": bool, "charging_vpp": float, "apply_flash"/"apply_filament": bool}
        self._default_get_params = get_params
        self._get_params = get_params
        self._on_drive_amp = on_drive_amp    # callback(absolute_vpp)
        self._lock = _threading.Lock()
        self._reduced = False
        self._measure_vpp: float = 0.0       # amplitude captured at reduce time
        self._charging_vpp: float = 0.0      # amplitude while parked

    # -- actuator protocol ----------------------------------------------

    def enable(self):
        self._park("filament")
        ok = False
        try:
            ok = self._actuator.enable()
        finally:
            if not ok:
                self._restore()
        return ok

    def disable(self):
        try:
            return self._actuator.disable()
        finally:
            self._restore()

    @property
    def is_connected(self) -> bool:
        return self._actuator.is_connected

    def arm(self):
        """Delegate session-on (NGE filament power on) to the wrapped actuator."""
        fn = getattr(self._actuator, "arm", None)
        if fn:
            fn()

    def disarm(self):
        fn = getattr(self._actuator, "disarm", None)
        if fn:
            fn()

    def set_pulse(self, freq_hz: float, width_ms: float):
        """Ramp support: park the drive (like enable) on the first pulse of a
        heating session, then program the filament pulse.  Reduce is idempotent,
        so subsequent ramp steps only reprogram the pulse."""
        self._park("filament")
        fn = getattr(self._actuator, "set_pulse", None)
        return fn(freq_hz, width_ms) if fn else False

    def fire_pulse(self, width_ms: float):
        """Pulse-wait-read ramp: park the drive (idempotent) then fire one
        hardware-timed filament pulse via the wrapped actuator."""
        self._park("filament")
        fn = getattr(self._actuator, "fire_pulse", None)
        return fn(width_ms) if fn else False

    def pulse_off(self):
        """Turn the filament pulse output off (the drive is restored on disable/
        restore, not here — the ramp keeps parking between pulses)."""
        fn = getattr(self._actuator, "pulse_off", None)
        return fn() if fn else False

    def hold_ssr_on(self):
        """Power ramp: park the drive (idempotent) then hold the SSR closed."""
        self._park("filament")
        fn = getattr(self._actuator, "hold_ssr_on", None)
        return fn() if fn else False

    def set_power_voltage(self, v):
        fn = getattr(self._actuator, "set_power_voltage", None)
        return fn(v) if fn else False

    def set_power_easyramp(self, duration_ms, enabled=True):
        fn = getattr(self._actuator, "set_power_easyramp", None)
        return fn(duration_ms, enabled) if fn else False

    def set_params_source(self, fn):
        """Temporarily source setback params from `fn` instead of the wired
        default (the Power-sweep tab uses this so its own setback settings
        drive the shared adapter during a sweep).  Pass None to restore."""
        self._get_params = fn or self._default_get_params

    def clear_params_source(self):
        """Restore the setback params source wired at construction."""
        self._get_params = self._default_get_params

    def park(self, tool: str = "filament"):
        """Park the drive low if the setback is enabled for `tool` (the
        ChargeController calls park("flash") around flash actuation).
        Idempotent."""
        self._park(tool)

    def restore(self):
        """Restore the drive to the measurement setpoint.  Idempotent."""
        self._restore()

    def _park(self, tool: str = "filament"):
        try:
            params = self._get_params() or {}
        except Exception:
            params = {}
        if not params.get("enabled"):
            return
        # per-tool gate; default to applying (True) if the key is absent so an
        # older two-tool params dict still parks for both.
        key = "apply_flash" if tool == "flash" else "apply_filament"
        if not params.get(key, True):
            return
        self._reduce(float(params.get("charging_vpp", 0.0)))

    def effective_amplitude(self):
        """Current actual drive amplitude (Vpp): the charging value while
        parked, else the monitored drive widget's measurement setpoint.
        Returns None if no drive widget is available."""
        with self._lock:
            if self._reduced:
                return self._charging_vpp
        try:
            return self._get_drive()._amp.value()
        except Exception:
            return None

    # -- internal ---------------------------------------------------------

    def _reduce(self, charging_vpp: float):
        with self._lock:
            if self._reduced or charging_vpp <= 0:
                return
            drive = self._get_drive()
            afg, ch = drive.get_afg_ch()
            if afg is None:
                return                        # no drive connected — nothing to park
            measure_vpp = drive._amp.value()  # widget = measurement setpoint
            if measure_vpp <= 0 or charging_vpp >= measure_vpp:
                return                        # nothing to gain by "reducing"
            with _afg_lock(afg):
                afg.set_amplitude(ch, charging_vpp)
            self._measure_vpp = measure_vpp
            self._charging_vpp = charging_vpp
            self._reduced = True
            cb, amp = self._on_drive_amp, charging_vpp
        if cb:
            cb(amp)                           # report absolute amplitude (outside lock)

    def _restore(self):
        with self._lock:
            if not self._reduced:
                return
            drive = self._get_drive()
            afg, ch = drive.get_afg_ch()
            if afg is not None:
                try:
                    with _afg_lock(afg):
                        afg.set_amplitude(ch, self._measure_vpp)
                except Exception:
                    pass
            self._reduced = False
            cb, amp = self._on_drive_amp, self._measure_vpp
        if cb:
            cb(amp)                           # report restored amplitude (outside lock)


# ---------------------------------------------------------------------------
# ChargeSequencerActuators — resolved-at-start hardware driver for the sequencer
# ---------------------------------------------------------------------------

class ChargeSequencerActuators:
    """
    Actuator driver for ChargeSequencer.

    prepare() runs on the GUI thread and snapshots the resolved hardware
    handles (AFG controllers + channels + pulse amplitudes, NGE controllers +
    channels + current limits) from the flash/filament trigger PulseGroups and
    the flash-control / filament-power NGEControlGroups.  start_discharge /
    start_recharge / stop_all / all_off then run on the sequencer worker thread
    and program those controllers directly (no widget access), so the sequencer
    is thread-safe.  AFG access is serialized with the shared per-AFG lock.
    """

    def __init__(self, wg_tab):
        self._wg = wg_tab
        self._h: dict = {}

    # -- GUI thread ----------------------------------------------------------

    def prepare(self):
        """Resolve and capture all hardware handles.  Call on the GUI thread."""
        wg = self._wg
        ft_afg, ft_ch = wg.flash_trigger._afg_ch()
        fc_nge, fc_ch = wg.flash_control._nge_ch()
        fl_afg, fl_ch = wg.filament._afg_ch()
        fp_nge, fp_ch = wg.filament_power._nge_ch()
        self._h = {
            "ft_afg": ft_afg, "ft_ch": ft_ch,
            "ft_vhigh": wg.flash_trigger._amp.value(),
            "ft_off": wg.flash_trigger._get_offset(),
            "ft_width_s": wg.flash_trigger._width_ms.value() * 1e-3,
            "fc_nge": fc_nge, "fc_ch": fc_ch,
            "fc_ilim": wg.flash_control._current.value(),
            "fl_afg": fl_afg, "fl_ch": fl_ch,
            "fl_vhigh": wg.filament._amp.value(),
            "fl_off": wg.filament._get_offset(),
            "fp_nge": fp_nge, "fp_ch": fp_ch,
            "fp_ilim": wg.filament_power._current.value(),
        }

    # -- worker thread -------------------------------------------------------

    def start_discharge(self, flash_rate_hz: float, ctrl_v: float):
        h = self._h
        nge, ch = h.get("fc_nge"), h.get("fc_ch")
        if nge is not None and nge.is_connected:
            nge.set_channel(ch, max(0.0, ctrl_v), h["fc_ilim"])
            nge.output_on(ch)
        afg, ach = h.get("ft_afg"), h.get("ft_ch")
        if afg is None:
            raise RuntimeError("flash-lamp trigger WG not connected")
        with _afg_lock(afg):
            afg.setup_pulse(ach, frequency=flash_rate_hz, amplitude=h["ft_vhigh"],
                            offset=h["ft_off"], width=h["ft_width_s"])
            afg.output_on(ach)

    def start_recharge(self, fil_freq_hz: float, fil_width_ms: float,
                       power_v: float = 0.0):
        h = self._h
        if power_v > 0:
            nge, ch = h.get("fp_nge"), h.get("fp_ch")
            if nge is not None and nge.is_connected:
                nge.set_channel(ch, power_v, h["fp_ilim"])
                nge.output_on(ch)
        afg, ach = h.get("fl_afg"), h.get("fl_ch")
        if afg is None:
            raise RuntimeError("filament trigger WG not connected")
        with _afg_lock(afg):
            afg.setup_pulse(ach, frequency=fil_freq_hz, amplitude=h["fl_vhigh"],
                            offset=h["fl_off"], width=fil_width_ms * 1e-3)
            afg.output_on(ach)

    def set_filament_power(self, power_v: float):
        """Turn the filament-power NGE on at power_v (session DC for the ramp)."""
        h = self._h
        nge, ch = h.get("fp_nge"), h.get("fp_ch")
        if power_v > 0 and nge is not None and nge.is_connected:
            nge.set_channel(ch, power_v, h["fp_ilim"])
            nge.output_on(ch)

    def fire_filament_pulse(self, width_ms: float):
        """Fire ONE hardware-timed filament pulse of the given width (pulse-wait-
        read recharge).  Uses the very-low-freq carrier so the next pulse is
        ~1000 s away; call filament_pulse_off() before then.  Worker thread."""
        h = self._h
        afg, ach = h.get("fl_afg"), h.get("fl_ch")
        if afg is None:
            raise RuntimeError("filament trigger WG not connected")
        with _afg_lock(afg):
            afg.output_off(ach)                # clean off edge
            afg.setup_pulse(ach, frequency=FILAMENT_PULSE_CARRIER_HZ,
                            amplitude=h["fl_vhigh"], offset=h["fl_off"],
                            width=max(float(width_ms), 0.0) * 1e-3)
            afg.output_on(ach)                 # fires one pulse of width_ms

    def filament_pulse_off(self):
        """Turn the filament trigger output off (ends a single-pulse window)."""
        h = self._h
        afg, ach = h.get("fl_afg"), h.get("fl_ch")
        if afg is not None:
            try:
                with _afg_lock(afg):
                    afg.output_off(ach)
            except Exception:
                pass

    def stop_all(self):
        """Stop actuation (both triggers off — gating stops all flashing/heating)."""
        for afg_key, ch_key in (("ft_afg", "ft_ch"), ("fl_afg", "fl_ch")):
            afg, ch = self._h.get(afg_key), self._h.get(ch_key)
            if afg is not None:
                try:
                    with _afg_lock(afg):
                        afg.output_off(ch)
                except Exception:
                    pass

    def all_off(self):
        """Full quiescent state — triggers off and NGE control/power outputs off."""
        self.stop_all()
        for nge_key, ch_key in (("fc_nge", "fc_ch"), ("fp_nge", "fp_ch")):
            nge, ch = self._h.get(nge_key), self._h.get(ch_key)
            if nge is not None:
                try:
                    nge.output_off(ch)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Sweep automation — worker thread
# ---------------------------------------------------------------------------

class _SweepWorker(QThread):
    """
    Runs an amplitude or frequency sweep in a background thread.

    Uses the AFG connected via the electrode map — no second serial
    connection needed.  Controls the DAQ via ZMQ.
    """

    log      = pyqtSignal(str)
    progress = pyqtSignal(int, int)   # (step, total)
    finished = pyqtSignal(bool)       # success flag

    def __init__(self, afg, channel: int, mode: str,
                 values: list, settle_s: float, files_per_step: int,
                 prefix: str, output_dir: str,
                 sample_rate: float, n_bits: int,
                 daq_host: str, daq_rep_port: int,
                 fixed_freq: float = 0.0, fixed_amp: float = 0.0,
                 parent=None):
        super().__init__(parent)
        self._afg            = afg
        self._channel        = channel
        self._mode           = mode
        self._values         = values
        self._settle_s       = settle_s
        self._files_per_step = files_per_step
        self._prefix         = prefix
        self._output_dir     = output_dir
        self._sample_rate    = sample_rate
        self._n_bits         = n_bits
        self._daq_host       = daq_host
        self._daq_rep_port   = daq_rep_port
        self._fixed_freq     = fixed_freq
        self._fixed_amp      = fixed_amp
        self._cancel         = False

    def cancel(self):
        self._cancel = True

    def run(self):
        import time

        if not _ZMQ_AVAILABLE:
            self.log.emit("ERROR: zmq not available — cannot control DAQ.")
            self.finished.emit(False)
            return

        daq = _ModuleClient(
            "daq",
            rep_port=self._daq_rep_port,
            pub_port=self._daq_rep_port + 1,
            host=self._daq_host,
            timeout_ms=8000,
        )

        try:
            try:
                self._afg.output_on(self._channel)
                self.log.emit("Output ON")
            except Exception as exc:
                self.log.emit(f"WARNING: could not turn output on: {exc}")

            n = len(self._values)
            for i, val in enumerate(self._values):
                if self._cancel:
                    self.log.emit("Sweep cancelled.")
                    break

                self.progress.emit(i + 1, n)

                # --- Set AFG parameter ---
                if self._mode == "amplitude":
                    self.log.emit(f"--- Step {i+1}/{n}: amp={val:.4g} Vpp  freq={self._fixed_freq:.4g} Hz ---")
                    try:
                        self._afg.set_amplitude(self._channel, val)
                    except Exception as exc:
                        self.log.emit(f"  ERROR setting amplitude: {exc}")
                        self.finished.emit(False)
                        return
                    basename = f"{self._prefix}_amp{val:.4g}V_f{self._fixed_freq:.4g}Hz"
                else:
                    self.log.emit(f"--- Step {i+1}/{n}: freq={val:.4g} Hz  amp={self._fixed_amp:.4g} Vpp ---")
                    try:
                        self._afg.set_frequency(self._channel, val)
                    except Exception as exc:
                        self.log.emit(f"  ERROR setting frequency: {exc}")
                        self.finished.emit(False)
                        return
                    basename = f"{self._prefix}_f{val:.4g}Hz_amp{self._fixed_amp:.4g}V"

                # --- Settle ---
                if self._settle_s > 0:
                    self.log.emit(f"  Settling {self._settle_s:.1f} s …")
                    t0 = time.time()
                    while time.time() - t0 < self._settle_s:
                        if self._cancel:
                            break
                        time.sleep(0.05)

                if self._cancel:
                    break

                # --- Trigger DAQ recording ---
                n_samples = 2 ** self._n_bits
                dur_s     = n_samples / self._sample_rate if self._sample_rate else 0
                self.log.emit(
                    f"  Recording {self._files_per_step} × "
                    f"{n_samples:,} samples ({dur_s:.2f} s) → {basename}"
                )
                try:
                    kwargs: dict = dict(
                        n_files=self._files_per_step,
                        basename=basename,
                        sample_rate=self._sample_rate,
                        n_bits=self._n_bits,
                    )
                    if self._output_dir:
                        kwargs["output_dir"] = self._output_dir
                    resp = daq.send("start_recording", **kwargs)
                except Exception as exc:
                    self.log.emit(f"  ERROR starting DAQ: {exc}")
                    self.finished.emit(False)
                    return

                if resp.get("status") != "ok":
                    self.log.emit(
                        f"  DAQ refused: {resp.get('message', resp)}"
                    )
                    self.finished.emit(False)
                    return

                # --- Wait for recording to finish ---
                timeout = max(180.0, dur_s * self._files_per_step * 2.5)
                t0 = time.time()
                # Brief initial wait so the recorder thread is definitely alive
                time.sleep(0.5)
                while True:
                    if self._cancel:
                        try:
                            daq.send("stop_recording")
                        except Exception:
                            pass
                        break
                    if time.time() - t0 > timeout:
                        self.log.emit("  ERROR: DAQ timeout — stopping.")
                        try:
                            daq.send("stop_recording")
                        except Exception:
                            pass
                        self.finished.emit(False)
                        return
                    try:
                        st = daq.send("get_status")
                        st_data = st.get("data", {})
                        if not st_data.get("recording", True):
                            # Recording stopped — check whether files were written
                            files_written = st_data.get("file_index", 0)
                            elapsed = time.time() - t0
                            if files_written == 0:
                                self.log.emit(
                                    f"  WARNING: recording stopped after {elapsed:.1f}s "
                                    f"but 0 files written.\n"
                                    f"  Check DAQ server logs for hardware errors "
                                    f"(wrong device name, NI-DAQmx not connected, etc.)"
                                )
                            else:
                                self.log.emit(
                                    f"  {files_written}/{self._files_per_step} file(s) "
                                    f"written in {elapsed:.1f}s"
                                )
                            break
                    except Exception as poll_exc:
                        self.log.emit(f"  poll error: {poll_exc}")
                    time.sleep(0.3)

                if self._cancel:
                    break

                try:
                    lf = daq.send("last_file").get("data", {}).get("path", "?")
                    self.log.emit(f"  ✓ Last file: {lf}")
                except Exception:
                    self.log.emit("  Done.")

            self.finished.emit(not self._cancel)

        except Exception as exc:
            self.log.emit(f"Sweep error: {exc}")
            self.finished.emit(False)
        finally:
            try:
                self._afg.output_off(self._channel)
                self.log.emit("Output OFF")
            except Exception as exc:
                self.log.emit(f"WARNING: could not turn output off: {exc}")
            daq.close()


# ---------------------------------------------------------------------------
# Sweep tab
# ---------------------------------------------------------------------------

class SweepTab(QWidget):
    """
    Self-contained sweep tab: set up the waveform, configure DAQ, and run
    a parameter sweep — all without leaving this tab or re-connecting the AFG.

    Uses the electrode map's already-connected AFG for the selected axis.
    Controls the running usphere-DAQ server via ZMQ.
    """

    _WAVEFORMS     = ["Sine", "Square", "Ramp", "Noise", "DC Bias", "ARB (loaded)"]
    _NO_FREQ_TYPES = {"Noise", "DC Bias", "ARB (loaded)"}   # disable freq sweep for these

    def __init__(self, electrode_map: "ElectrodeMapWidget", parent=None):
        super().__init__(parent)
        self._map          = electrode_map
        self._worker: _SweepWorker | None = None
        self._sweep_list:   list[dict] = []
        self._pending_list: list[dict] = []
        self._running_list: bool       = False
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(8, 8, 8, 8)

        if not _ZMQ_AVAILABLE:
            lbl = QLabel(
                "ZMQ not available — install with:  pip install pyzmq\n"
                "The sweep tab requires a running usphere-DAQ server."
            )
            lbl.setStyleSheet("color: #b45309; font-size: 12px;")
            lbl.setWordWrap(True)
            root.addWidget(lbl)
            root.addStretch()
            return

        # ── DAQ Server ──────────────────────────────────────────────────
        daq_box = QGroupBox("DAQ Server (ZMQ)")
        dl = QHBoxLayout(daq_box)
        dl.addWidget(QLabel("Host:"))
        self._host_edit = QLineEdit("localhost")
        self._host_edit.setMaximumWidth(130)
        dl.addWidget(self._host_edit)
        dl.addWidget(QLabel("Port:"))
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1024, 65535)
        self._port_spin.setValue(5552)
        self._port_spin.setMaximumWidth(75)
        dl.addWidget(self._port_spin)
        self._ping_btn = QPushButton("Ping")
        self._ping_btn.setMaximumWidth(60)
        self._ping_btn.clicked.connect(self._ping_daq)
        dl.addWidget(self._ping_btn)
        self._fetch_btn = QPushButton("Fetch Config")
        self._fetch_btn.setMaximumWidth(90)
        self._fetch_btn.setToolTip("Load current DAQ output_dir, sample_rate and n_bits from the server")
        self._fetch_btn.clicked.connect(self._fetch_daq_config)
        dl.addWidget(self._fetch_btn)
        self._daq_status = QLabel("—")
        self._daq_status.setStyleSheet("color: gray;")
        dl.addWidget(self._daq_status)
        dl.addStretch()
        root.addWidget(daq_box)

        # ── Waveform Setup ───────────────────────────────────────────────
        wf_box = QGroupBox("Waveform Setup")
        wl = QVBoxLayout(wf_box)

        # Row 0: axis + waveform type
        wr0 = QHBoxLayout()
        wr0.addWidget(QLabel("Axis:"))
        self._axis_bg = QButtonGroup(self)
        for ax in ("X", "Y", "Z"):
            rb = QRadioButton(ax)
            self._axis_bg.addButton(rb)
            wr0.addWidget(rb)
            if ax == "X":
                rb.setChecked(True)
        wr0.addSpacing(16)
        wr0.addWidget(QLabel("Type:"))
        self._wf_combo = QComboBox()
        self._wf_combo.addItems(self._WAVEFORMS)
        self._wf_combo.setMaximumWidth(130)
        self._wf_combo.currentTextChanged.connect(self._on_wf_type_changed)
        wr0.addWidget(self._wf_combo)
        wr0.addStretch()
        wl.addLayout(wr0)

        # Row 1: freq + amplitude
        wr1 = QHBoxLayout()
        wr1.addWidget(QLabel("Frequency:"))
        self._wf_freq = QDoubleSpinBox()
        self._wf_freq.setRange(0.001, 25e6)
        self._wf_freq.setDecimals(3)
        self._wf_freq.setValue(100.0)
        self._wf_freq.setSuffix(" Hz")
        self._wf_freq.setMinimumWidth(130)
        wr1.addWidget(self._wf_freq)
        wr1.addWidget(QLabel("Amplitude:"))
        self._wf_amp = QDoubleSpinBox()
        self._wf_amp.setRange(0.001, 20.0)
        self._wf_amp.setDecimals(3)
        self._wf_amp.setValue(1.0)
        self._wf_amp.setSuffix(" Vpp")
        self._wf_amp.setMinimumWidth(110)
        wr1.addWidget(self._wf_amp)
        wr1.addStretch()
        wl.addLayout(wr1)

        # Row 2: offset + phase
        wr2 = QHBoxLayout()
        wr2.addWidget(QLabel("Offset:"))
        self._wf_offset = QDoubleSpinBox()
        self._wf_offset.setRange(-10.0, 10.0)
        self._wf_offset.setDecimals(3)
        self._wf_offset.setValue(0.0)
        self._wf_offset.setSuffix(" V")
        self._wf_offset.setMinimumWidth(100)
        wr2.addWidget(self._wf_offset)
        wr2.addWidget(QLabel("Phase:"))
        self._wf_phase = QDoubleSpinBox()
        self._wf_phase.setRange(-180.0, 180.0)
        self._wf_phase.setDecimals(2)
        self._wf_phase.setValue(0.0)
        self._wf_phase.setSuffix(" °")
        self._wf_phase.setMinimumWidth(100)
        wr2.addWidget(self._wf_phase)
        wr2.addStretch()
        wl.addLayout(wr2)

        # Row 3: Apply / ON / OFF / status
        wr3 = QHBoxLayout()
        self._apply_btn = QPushButton("Apply Waveform")
        self._apply_btn.clicked.connect(self._apply_waveform)
        wr3.addWidget(self._apply_btn)
        self._on_btn = QPushButton("Output ON")
        self._on_btn.setStyleSheet("QPushButton{background:#4CAF50;color:white;}")
        self._on_btn.clicked.connect(self._output_on)
        wr3.addWidget(self._on_btn)
        self._off_btn = QPushButton("Output OFF")
        self._off_btn.setStyleSheet("QPushButton{background:#F44336;color:white;}")
        self._off_btn.clicked.connect(self._output_off)
        wr3.addWidget(self._off_btn)
        self._wf_status = QLabel("—")
        self._wf_status.setStyleSheet("color:gray;")
        wr3.addWidget(self._wf_status)
        wr3.addStretch()
        wl.addLayout(wr3)
        self._wf_hw_lbl = QLabel("")
        self._wf_hw_lbl.setStyleSheet("color:gray;font-style:italic;")
        wl.addWidget(self._wf_hw_lbl)

        # Frequency comb sub-section (requires afg2225_arbitrarywf)
        self._wf_comb_worker: "_CombWorker | None" = None
        if _ARB_AVAILABLE:
            comb_box = QGroupBox("Frequency Comb (ARB)")
            ccl = QVBoxLayout(comb_box)

            cc1 = QHBoxLayout()
            cc1.addWidget(QLabel("Frequencies (Hz):"))
            self._wf_comb_freqs = QLineEdit()
            self._wf_comb_freqs.setPlaceholderText("e.g.  100, 200, 500")
            cc1.addWidget(self._wf_comb_freqs)
            ccl.addLayout(cc1)

            cc_range = QHBoxLayout()
            cc_range.addWidget(QLabel("Range:"))
            self._wf_comb_start = QDoubleSpinBox()
            self._wf_comb_start.setRange(0.001, 25e6)
            self._wf_comb_start.setDecimals(3)
            self._wf_comb_start.setValue(100.0)
            self._wf_comb_start.setMinimumWidth(90)
            self._wf_comb_start.setPrefix("start ")
            cc_range.addWidget(self._wf_comb_start)
            self._wf_comb_stop = QDoubleSpinBox()
            self._wf_comb_stop.setRange(0.001, 25e6)
            self._wf_comb_stop.setDecimals(3)
            self._wf_comb_stop.setValue(1000.0)
            self._wf_comb_stop.setMinimumWidth(90)
            self._wf_comb_stop.setPrefix("stop ")
            cc_range.addWidget(self._wf_comb_stop)
            self._wf_comb_step = QDoubleSpinBox()
            self._wf_comb_step.setRange(0.001, 25e6)
            self._wf_comb_step.setDecimals(3)
            self._wf_comb_step.setValue(100.0)
            self._wf_comb_step.setMinimumWidth(90)
            self._wf_comb_step.setPrefix("step ")
            cc_range.addWidget(self._wf_comb_step)
            wf_range_btn = QPushButton("→ List")
            wf_range_btn.setMaximumWidth(60)
            wf_range_btn.clicked.connect(self._wf_comb_range_to_list)
            cc_range.addWidget(wf_range_btn)
            cc_range.addStretch()
            ccl.addLayout(cc_range)

            cc2 = QHBoxLayout()
            cc2.addWidget(QLabel("Amplitude:"))
            self._wf_comb_amp = QDoubleSpinBox()
            self._wf_comb_amp.setRange(0.001, 20.0)
            self._wf_comb_amp.setDecimals(3)
            self._wf_comb_amp.setValue(1.0)
            self._wf_comb_amp.setSuffix(" Vpp")
            self._wf_comb_amp.setMinimumWidth(100)
            cc2.addWidget(self._wf_comb_amp)
            cc2.addSpacing(12)
            cc2.addWidget(QLabel("MC iter:"))
            self._wf_comb_mc = QSpinBox()
            self._wf_comb_mc.setRange(0, 20000)
            self._wf_comb_mc.setValue(500)
            self._wf_comb_mc.setMinimumWidth(75)
            cc2.addWidget(self._wf_comb_mc)
            cc2.addSpacing(12)
            self._wf_comb_btn = QPushButton("Apply Comb")
            self._wf_comb_btn.clicked.connect(self._apply_wf_comb)
            cc2.addWidget(self._wf_comb_btn)
            self._wf_comb_status = QLabel("—")
            self._wf_comb_status.setStyleSheet("color:gray;")
            cc2.addWidget(self._wf_comb_status, 1)
            ccl.addLayout(cc2)

            wl.addWidget(comb_box)

        root.addWidget(wf_box)

        # ── Recording Settings ───────────────────────────────────────────
        rec_box = QGroupBox("Recording Settings")
        rl = QVBoxLayout(rec_box)

        rr1 = QHBoxLayout()
        rr1.addWidget(QLabel("Output dir:"))
        self._dir_edit = QLineEdit()
        self._dir_edit.setPlaceholderText("required — browse or type a full path")
        rr1.addWidget(self._dir_edit)
        self._dir_btn = QPushButton("Browse…")
        self._dir_btn.setMaximumWidth(80)
        self._dir_btn.clicked.connect(self._browse_dir)
        rr1.addWidget(self._dir_btn)
        rl.addLayout(rr1)

        rr2 = QHBoxLayout()
        rr2.addWidget(QLabel("Prefix:"))
        self._prefix_edit = QLineEdit("ptrap")
        self._prefix_edit.setMaximumWidth(110)
        self._prefix_edit.setToolTip(
            "Saved as  {prefix}_amp_{val}_NNN.h5  or  {prefix}_freq_{val}_NNN.h5"
        )
        rr2.addWidget(self._prefix_edit)
        rr2.addWidget(QLabel("Files/step:"))
        self._files_spin = QSpinBox()
        self._files_spin.setRange(1, 200)
        self._files_spin.setValue(1)
        self._files_spin.setMaximumWidth(65)
        rr2.addWidget(self._files_spin)
        rr2.addWidget(QLabel("Settle (s):"))
        self._settle_spin = QDoubleSpinBox()
        self._settle_spin.setRange(0.0, 300.0)
        self._settle_spin.setDecimals(1)
        self._settle_spin.setValue(2.0)
        self._settle_spin.setMaximumWidth(75)
        rr2.addWidget(self._settle_spin)
        rr2.addStretch()
        rl.addLayout(rr2)

        rr3 = QHBoxLayout()
        rr3.addWidget(QLabel("Sample rate:"))
        self._rate_spin = QDoubleSpinBox()
        self._rate_spin.setRange(1.0, 2e6)
        self._rate_spin.setDecimals(0)
        self._rate_spin.setValue(10000.0)
        self._rate_spin.setSuffix(" Hz")
        self._rate_spin.setMinimumWidth(120)
        self._rate_spin.valueChanged.connect(self._update_duration_lbl)
        rr3.addWidget(self._rate_spin)
        rr3.addWidget(QLabel("n_bits (2ⁿ samples):"))
        self._nbits_spin = QSpinBox()
        self._nbits_spin.setRange(10, 25)
        self._nbits_spin.setValue(17)
        self._nbits_spin.setMaximumWidth(60)
        self._nbits_spin.valueChanged.connect(self._update_duration_lbl)
        rr3.addWidget(self._nbits_spin)
        self._dur_lbl = QLabel()
        self._dur_lbl.setStyleSheet("color: gray; font-size: 10px;")
        rr3.addWidget(self._dur_lbl)
        rr3.addStretch()
        rl.addLayout(rr3)
        self._update_duration_lbl()
        root.addWidget(rec_box)

        # ── Sweep ────────────────────────────────────────────────────────
        sw_box = QGroupBox("Sweep")
        sl = QVBoxLayout(sw_box)

        sm1 = QHBoxLayout()
        sm1.addWidget(QLabel("Mode:"))
        self._mode_bg = QButtonGroup(self)
        self._amp_rb  = QRadioButton("Amplitude (Vpp)")
        self._freq_rb = QRadioButton("Frequency (Hz)")
        self._amp_rb.setChecked(True)
        self._mode_bg.addButton(self._amp_rb)
        self._mode_bg.addButton(self._freq_rb)
        self._amp_rb.toggled.connect(self._on_mode_changed)
        sm1.addWidget(self._amp_rb)
        sm1.addWidget(self._freq_rb)
        sm1.addStretch()
        sl.addLayout(sm1)

        sm2 = QHBoxLayout()
        sm2.addWidget(QLabel("Start:"))
        self._start_spin = QDoubleSpinBox()
        self._start_spin.setDecimals(4)
        self._start_spin.setMinimumWidth(110)
        sm2.addWidget(self._start_spin)
        sm2.addWidget(QLabel("Stop:"))
        self._stop_spin = QDoubleSpinBox()
        self._stop_spin.setDecimals(4)
        self._stop_spin.setMinimumWidth(110)
        sm2.addWidget(self._stop_spin)
        sm2.addWidget(QLabel("Step:"))
        self._step_spin = QDoubleSpinBox()
        self._step_spin.setDecimals(4)
        self._step_spin.setMinimumWidth(100)
        sm2.addWidget(self._step_spin)
        self._range_unit_lbl = QLabel("Vpp")
        sm2.addWidget(self._range_unit_lbl)
        sm2.addStretch()
        sl.addLayout(sm2)
        root.addWidget(sw_box)

        self._on_mode_changed()

        # ── Sweep Chain / Recipe ─────────────────────────────────────────
        chain_box = QGroupBox("Sweep Chain / Recipe")
        chl = QVBoxLayout(chain_box)

        chain_btn_row = QHBoxLayout()
        self._add_btn = QPushButton("Add to List")
        self._add_btn.setToolTip("Append current sweep configuration to the list")
        self._add_btn.clicked.connect(self._add_to_list)
        chain_btn_row.addWidget(self._add_btn)

        self._remove_last_btn = QPushButton("Clear Last")
        self._remove_last_btn.setEnabled(False)
        self._remove_last_btn.clicked.connect(self._remove_last)
        chain_btn_row.addWidget(self._remove_last_btn)

        self._clear_list_btn = QPushButton("Clear List")
        self._clear_list_btn.setEnabled(False)
        self._clear_list_btn.clicked.connect(self._clear_list)
        chain_btn_row.addWidget(self._clear_list_btn)

        chain_btn_row.addSpacing(16)

        self._save_recipe_btn = QPushButton("Save Recipe…")
        self._save_recipe_btn.clicked.connect(self._save_recipe)
        chain_btn_row.addWidget(self._save_recipe_btn)

        self._load_recipe_btn = QPushButton("Load Recipe…")
        self._load_recipe_btn.clicked.connect(self._load_recipe)
        chain_btn_row.addWidget(self._load_recipe_btn)

        chain_btn_row.addStretch()
        chl.addLayout(chain_btn_row)

        self._list_display = QTextEdit()
        self._list_display.setReadOnly(True)
        self._list_display.setFixedHeight(100)
        self._list_display.setStyleSheet(
            "font-family:Consolas,monospace;font-size:10px;"
            "background:#f8f8f8;color:#333;border:1px solid #ccc;"
        )
        self._list_display.setPlaceholderText(
            "(empty — configure a sweep above then click Add to List)"
        )
        chl.addWidget(self._list_display)
        root.addWidget(chain_box)

        # ── Controls ────────────────────────────────────────────────────
        ctrl_row = QHBoxLayout()

        self._start_btn = QPushButton("▶  Start Sweep")
        self._start_btn.setMinimumWidth(130)
        self._start_btn.setStyleSheet(
            "QPushButton{background:#1d4ed8;color:white;font-weight:bold;"
            "padding:5px 18px;border-radius:4px;}"
            "QPushButton:hover{background:#2563eb;}"
            "QPushButton:disabled{background:#94a3b8;}"
        )
        self._start_btn.clicked.connect(self._start_sweep)
        ctrl_row.addWidget(self._start_btn)

        self._run_list_btn = QPushButton("▶  Run List")
        self._run_list_btn.setEnabled(False)
        self._run_list_btn.setMinimumWidth(110)
        self._run_list_btn.setStyleSheet(
            "QPushButton{background:#15803d;color:white;font-weight:bold;"
            "padding:5px 14px;border-radius:4px;}"
            "QPushButton:hover{background:#16a34a;}"
            "QPushButton:disabled{background:#94a3b8;}"
        )
        self._run_list_btn.clicked.connect(self._run_list)
        ctrl_row.addWidget(self._run_list_btn)

        self._cancel_btn = QPushButton("■  Cancel")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.setStyleSheet(
            "QPushButton{background:#dc2626;color:white;padding:5px 12px;"
            "border-radius:4px;}"
            "QPushButton:hover{background:#ef4444;}"
            "QPushButton:disabled{background:#94a3b8;}"
        )
        self._cancel_btn.clicked.connect(self._cancel_sweep)
        ctrl_row.addWidget(self._cancel_btn)

        self._progress_lbl = QLabel("")
        self._progress_lbl.setStyleSheet("color:gray;font-size:10px;")
        ctrl_row.addWidget(self._progress_lbl)
        ctrl_row.addStretch()
        root.addLayout(ctrl_row)

        # ── Log ─────────────────────────────────────────────────────────
        self._log_box = QTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setMinimumHeight(150)
        self._log_box.setStyleSheet(
            "font-family:Consolas,monospace;font-size:11px;"
            "background:#1e1e1e;color:#d4d4d4;"
        )
        root.addWidget(self._log_box)

        # Poll waveform setup HW state every 8 s (skip if sweep running)
        self._wf_poll_timer = QTimer(self)
        self._wf_poll_timer.setInterval(8000)
        self._wf_poll_timer.timeout.connect(self._poll_wf_hw_status)
        self._wf_poll_timer.start()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _log(self, msg: str):
        self._log_box.append(msg)
        self._log_box.verticalScrollBar().setValue(
            self._log_box.verticalScrollBar().maximum()
        )

    def _update_duration_lbl(self):
        if not hasattr(self, "_dur_lbl"):
            return
        n   = 2 ** self._nbits_spin.value()
        dur = n / self._rate_spin.value() if self._rate_spin.value() else 0
        self._dur_lbl.setText(
            f"→  {n:,} samples  =  {dur:.2f} s per file"
        )

    def _on_mode_changed(self):
        is_amp = self._amp_rb.isChecked()
        if is_amp:
            self._start_spin.setRange(0.001, 20.0)
            self._stop_spin.setRange(0.001, 20.0)
            self._step_spin.setRange(0.001, 10.0)
            self._start_spin.setValue(0.1)
            self._stop_spin.setValue(2.0)
            self._step_spin.setValue(0.1)
            self._range_unit_lbl.setText("Vpp")
        else:
            self._start_spin.setRange(0.001, 25e6)
            self._stop_spin.setRange(0.001, 25e6)
            self._step_spin.setRange(0.001, 1e6)
            self._start_spin.setValue(100.0)
            self._stop_spin.setValue(1000.0)
            self._step_spin.setValue(100.0)
            self._range_unit_lbl.setText("Hz")

    def _on_wf_type_changed(self, wf: str):
        no_freq = wf in self._NO_FREQ_TYPES
        self._freq_rb.setEnabled(not no_freq)
        self._wf_freq.setEnabled(wf != "ARB (loaded)")
        if no_freq and self._freq_rb.isChecked():
            self._amp_rb.setChecked(True)

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(
            self, "Select output directory",
            self._dir_edit.text() or ""
        )
        if d:
            self._dir_edit.setText(d)

    def _selected_axis(self) -> str:
        for btn in self._axis_bg.buttons():
            if btn.isChecked():
                return btn.text().lower()
        return "x"

    def _get_afg_or_err(self) -> tuple:
        """Return (afg, ch) or emit error log and return (None, None)."""
        axis = self._selected_axis()
        afg, ch = self._map.get_afg_ch(axis)
        if afg is None or not afg.is_connected:
            self._wf_status.setText(
                f"{axis.upper()} AFG not connected — connect in Electrode Map"
            )
            self._wf_status.setStyleSheet("color:red;")
            return None, None
        return afg, ch

    # ------------------------------------------------------------------
    # DAQ helpers
    # ------------------------------------------------------------------

    def _make_daq_client(self, timeout_ms: int = 3000) -> "_ModuleClient":
        return _ModuleClient(
            "daq",
            rep_port=self._port_spin.value(),
            pub_port=self._port_spin.value() + 1,
            host=self._host_edit.text().strip() or "localhost",
            timeout_ms=timeout_ms,
        )

    def _ping_daq(self):
        try:
            c = self._make_daq_client()
            ok = c.ping()
            c.close()
        except Exception as exc:
            self._daq_status.setText(f"Error: {exc}")
            self._daq_status.setStyleSheet("color:red;")
            return
        if ok:
            self._daq_status.setText("Connected")
            self._daq_status.setStyleSheet("color:green;font-weight:bold;")
        else:
            self._daq_status.setText("No response")
            self._daq_status.setStyleSheet("color:red;")

    def _fetch_daq_config(self):
        """Pull current DAQ config from the server and populate the UI fields."""
        try:
            c = self._make_daq_client()
            resp = c.send("get_config")
            c.close()
        except Exception as exc:
            self._daq_status.setText(f"Fetch error: {exc}")
            self._daq_status.setStyleSheet("color:red;")
            return
        if resp.get("status") != "ok":
            self._daq_status.setText(
                f"Fetch failed: {resp.get('message','')}"
            )
            self._daq_status.setStyleSheet("color:red;")
            return
        cfg = resp.get("data", {})
        if "output_dir" in cfg and not self._dir_edit.text().strip():
            self._dir_edit.setText(str(cfg["output_dir"]))
        if "sample_rate" in cfg:
            self._rate_spin.setValue(float(cfg["sample_rate"]))
        if "n_bits" in cfg:
            self._nbits_spin.setValue(int(cfg["n_bits"]))
        if "basename" in cfg and not self._prefix_edit.text().strip():
            self._prefix_edit.setText(str(cfg["basename"]))
        self._daq_status.setText(
            f"Config fetched  (dir: {cfg.get('output_dir','?')})"
        )
        self._daq_status.setStyleSheet("color:green;")

    # ------------------------------------------------------------------
    # Waveform controls
    # ------------------------------------------------------------------

    def _apply_waveform(self):
        afg, ch = self._get_afg_or_err()
        if afg is None:
            return
        wf     = self._wf_combo.currentText()
        freq   = self._wf_freq.value()
        amp    = self._wf_amp.value()
        offset = self._wf_offset.value()
        phase  = self._wf_phase.value()
        try:
            if wf == "Sine":
                afg.setup_sine(ch, frequency=freq, amplitude=amp, offset=offset)
                afg.set_phase(ch, phase)
            elif wf == "Square":
                afg.setup_square(ch, frequency=freq, amplitude=amp, offset=offset)
                afg.set_phase(ch, phase)
            elif wf == "Ramp":
                afg.setup_ramp(ch, frequency=freq, amplitude=amp, offset=offset)
                afg.set_phase(ch, phase)
            elif wf == "Noise":
                afg.setup_noise(ch, amplitude=amp, offset=offset)
            elif wf == "DC Bias":
                afg.setup_sine(ch, frequency=1e-6, amplitude=amp, offset=offset)
            elif wf == "ARB (loaded)":
                afg.set_amplitude(ch, amp)
                afg.set_offset(ch, offset)
            status_txt = (f"DC Bias — {amp:.3g} Vpp, offset {offset:+.3g} V"
                          if wf == "DC Bias"
                          else f"Applied — {wf}, {freq:.4g} Hz, {amp:.3g} Vpp")
            self._wf_status.setText(status_txt)
            self._wf_status.setStyleSheet("color:green;")
            self._update_wf_hw_lbl(afg, ch)
        except Exception as exc:
            self._wf_status.setText(f"Error: {exc}")
            self._wf_status.setStyleSheet("color:red;")

    def _output_on(self):
        afg, ch = self._get_afg_or_err()
        if afg is None:
            return
        try:
            afg.output_on(ch)
            self._wf_status.setText("Output ON")
            self._wf_status.setStyleSheet("color:green;")
            self._update_wf_hw_lbl(afg, ch)
        except Exception as exc:
            self._wf_status.setText(f"Error: {exc}")
            self._wf_status.setStyleSheet("color:red;")

    def _output_off(self):
        afg, ch = self._get_afg_or_err()
        if afg is None:
            return
        try:
            afg.output_off(ch)
            self._wf_status.setText("Output OFF")
            self._wf_status.setStyleSheet("color:gray;")
            self._update_wf_hw_lbl(afg, ch)
        except Exception as exc:
            self._wf_status.setText(f"Error: {exc}")
            self._wf_status.setStyleSheet("color:red;")

    # ------------------------------------------------------------------
    # Waveform comb helpers
    # ------------------------------------------------------------------

    def _wf_comb_range_to_list(self):
        import numpy as np
        start = self._wf_comb_start.value()
        stop  = self._wf_comb_stop.value()
        step  = self._wf_comb_step.value()
        if step <= 0 or start >= stop:
            self._wf_comb_status.setText("Invalid range (need start < stop, step > 0)")
            self._wf_comb_status.setStyleSheet("color:red;")
            return
        freqs = np.arange(start, stop, step)
        if len(freqs) == 0:
            self._wf_comb_status.setText("Range produced no frequencies")
            self._wf_comb_status.setStyleSheet("color:red;")
            return
        self._wf_comb_freqs.setText(", ".join(f"{f:.6g}" for f in freqs))
        self._wf_comb_status.setText(f"{len(freqs)} frequencies loaded")
        self._wf_comb_status.setStyleSheet("color:gray;")

    def _apply_wf_comb(self):
        if not _ARB_AVAILABLE or self._wf_comb_worker is not None:
            return
        afg, ch = self._get_afg_or_err()
        if afg is None:
            return
        raw = self._wf_comb_freqs.text().strip()
        if not raw:
            self._wf_comb_status.setText("Enter frequencies first")
            self._wf_comb_status.setStyleSheet("color:red;")
            return
        try:
            freqs = [float(x.strip()) for x in raw.split(",") if x.strip()]
        except ValueError:
            self._wf_comb_status.setText("Invalid frequency list")
            self._wf_comb_status.setStyleSheet("color:red;")
            return
        if not freqs:
            self._wf_comb_status.setText("No valid frequencies")
            self._wf_comb_status.setStyleSheet("color:red;")
            return

        self._wf_comb_btn.setEnabled(False)
        self._wf_comb_status.setText("Working…")
        self._wf_comb_status.setStyleSheet("color:gray;")

        self._wf_comb_worker = _CombWorker(
            afg, ch, freqs,
            amplitude=self._wf_comb_amp.value(),
            offset=0.0,
            n_mc=self._wf_comb_mc.value(),
            parent=self,
        )
        self._wf_comb_worker.log.connect(self._wf_comb_status.setText)
        self._wf_comb_worker.finished.connect(self._on_wf_comb_done)
        self._wf_comb_worker.start()

    def _on_wf_comb_done(self, ok: bool, msg: str):
        self._wf_comb_btn.setEnabled(True)
        self._wf_comb_worker = None
        if ok:
            self._wf_comb_status.setText(msg)
            self._wf_comb_status.setStyleSheet("color:green;")
            afg, ch = self._get_afg_or_err()
            if afg is not None:
                self._update_wf_hw_lbl(afg, ch)
        else:
            self._wf_comb_status.setText(f"Error: {msg}")
            self._wf_comb_status.setStyleSheet("color:red;")

    # ------------------------------------------------------------------

    def _update_wf_hw_lbl(self, afg, ch: int) -> None:
        _poll_hw_async(afg, ch, self._wf_hw_lbl.setText)

    def _poll_wf_hw_status(self) -> None:
        if self._worker is not None:
            return
        if getattr(self, '_wf_poll_running', False):
            return
        axis = self._selected_axis()
        try:
            afg, ch = self._map.get_afg_ch(axis)
            if afg is None or not afg.is_connected:
                return
        except Exception:
            return
        self._wf_poll_running = True

        def _done(txt):
            self._wf_hw_lbl.setText(txt)
            self._wf_poll_running = False

        _poll_hw_async(afg, ch, _done)

    # ------------------------------------------------------------------
    # Sweep control
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Sweep config capture & list management
    # ------------------------------------------------------------------

    def _capture_sweep_config(self) -> dict:
        """Snapshot the current UI state into a portable dict."""
        return {
            "axis":           self._selected_axis(),
            "wf_type":        self._wf_combo.currentText(),
            "freq_hz":        self._wf_freq.value(),
            "amp_vpp":        self._wf_amp.value(),
            "offset_v":       self._wf_offset.value(),
            "phase_deg":      self._wf_phase.value(),
            "mode":           "amplitude" if self._amp_rb.isChecked() else "frequency",
            "start":          self._start_spin.value(),
            "stop":           self._stop_spin.value(),
            "step":           self._step_spin.value(),
            "files_per_step": self._files_spin.value(),
            "settle_s":       self._settle_spin.value(),
            "sample_rate":    self._rate_spin.value(),
            "n_bits":         self._nbits_spin.value(),
            "output_dir":     self._dir_edit.text().strip(),
            "prefix":         self._prefix_edit.text().strip() or "ptrap",
            "daq_host":       self._host_edit.text().strip() or "localhost",
            "daq_port":       self._port_spin.value(),
        }

    def _format_sweep_entry(self, i: int, cfg: dict) -> str:
        axis  = cfg["axis"].upper()
        wf    = cfg["wf_type"]
        freq  = cfg["freq_hz"]
        amp   = cfg["amp_vpp"]
        mode  = cfg["mode"]
        start = cfg["start"]
        stop  = cfg["stop"]
        step  = cfg["step"]
        unit  = "Vpp" if mode == "amplitude" else "Hz"
        n_pts = max(1, round((stop - start) / step) + 1) if step > 0 else 1
        sr    = cfg["sample_rate"]
        nb    = cfg["n_bits"]
        dur   = (2 ** nb) / sr if sr else 0.0
        fps   = cfg["files_per_step"]
        return (
            f"[{i+1}] {axis} | {wf} {freq:.4g}Hz {amp:.4g}Vpp | "
            f"{mode}: {start:.4g}→{stop:.4g} {unit} step {step:.4g} ({n_pts} pts) | "
            f"{fps}×{dur:.1f}s settle {cfg['settle_s']:.1f}s | → {cfg['prefix']}"
        )

    def _refresh_list_display(self):
        has = bool(self._sweep_list)
        if has:
            self._list_display.setPlainText(
                "\n".join(
                    self._format_sweep_entry(i, cfg)
                    for i, cfg in enumerate(self._sweep_list)
                )
            )
        else:
            self._list_display.clear()
        self._run_list_btn.setEnabled(has and self._worker is None)
        self._remove_last_btn.setEnabled(has)
        self._clear_list_btn.setEnabled(has)

    def _add_to_list(self):
        cfg = self._capture_sweep_config()
        if not cfg["output_dir"]:
            self._log("Cannot add to list: output directory is empty.")
            return
        self._sweep_list.append(cfg)
        self._refresh_list_display()
        self._log(f"Added: {self._format_sweep_entry(len(self._sweep_list)-1, cfg)}")

    def _remove_last(self):
        if self._sweep_list:
            self._sweep_list.pop()
            self._refresh_list_display()
            self._log(f"Removed last entry ({len(self._sweep_list)} remaining).")

    def _clear_list(self):
        self._sweep_list.clear()
        self._refresh_list_display()
        self._log("Sweep list cleared.")

    def _save_recipe(self):
        import json as _json
        path, _ = QFileDialog.getSaveFileName(
            self, "Save sweep recipe", "",
            "JSON files (*.json);;All files (*)"
        )
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                _json.dump(self._sweep_list, f, indent=2)
            self._log(f"Recipe saved ({len(self._sweep_list)} sweep(s)): {path}")
        except Exception as exc:
            self._log(f"ERROR saving recipe: {exc}")

    def _load_recipe(self):
        import json as _json
        path, _ = QFileDialog.getOpenFileName(
            self, "Load sweep recipe", "",
            "JSON files (*.json);;All files (*)"
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            if not isinstance(data, list):
                self._log("ERROR: recipe file must be a JSON list.")
                return
            self._sweep_list = data
            self._refresh_list_display()
            self._log(f"Recipe loaded ({len(self._sweep_list)} sweep(s)): {path}")
        except Exception as exc:
            self._log(f"ERROR loading recipe: {exc}")

    # ------------------------------------------------------------------
    # Sweep execution (single and list)
    # ------------------------------------------------------------------

    def _start_sweep(self):
        """Start a single sweep from the current UI state (no wf auto-apply)."""
        cfg = self._capture_sweep_config()
        self._launch_worker(cfg, apply_wf=False)

    def _run_list(self):
        """Start running the full sweep list from the beginning."""
        if not self._sweep_list:
            self._log("Sweep list is empty — add sweeps first.")
            return
        self._pending_list = list(self._sweep_list)
        self._running_list = True
        total = len(self._pending_list)
        self._log(
            f"\n{'='*60}\n"
            f"Running recipe: {total} sweep(s)\n"
            f"{'='*60}"
        )
        self._start_btn.setEnabled(False)
        self._run_list_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._run_next_in_list()

    def _run_next_in_list(self):
        if not self._pending_list:
            self._on_list_done(True)
            return
        cfg   = self._pending_list.pop(0)
        done  = len(self._sweep_list) - len(self._pending_list)
        total = len(self._sweep_list)
        self._progress_lbl.setText(f"Recipe {done}/{total}")
        self._log(f"\n--- Recipe step {done}/{total} ---")
        self._launch_worker(cfg, apply_wf=True)

    def _on_list_done(self, ok: bool):
        self._running_list = False
        self._pending_list = []
        self._start_btn.setEnabled(True)
        self._run_list_btn.setEnabled(bool(self._sweep_list))
        self._cancel_btn.setEnabled(False)
        self._progress_lbl.setText("")
        self._log(
            "\n=== Recipe complete ===" if ok
            else "\n=== Recipe cancelled/stopped ==="
        )

    def _launch_worker(self, cfg: dict, apply_wf: bool):
        """Validate, optionally apply waveform, then start _SweepWorker."""
        axis = cfg["axis"]
        afg, ch = self._map.get_afg_ch(axis)

        if afg is None or not afg.is_connected:
            self._log(
                f"ERROR: {axis.upper()} electrode AFG not connected.\n"
                "Connect it in the Electrode Map tab."
            )
            if self._running_list:
                self._on_list_done(False)
            return

        out_dir = cfg.get("output_dir", "")
        if not out_dir:
            self._log(
                "ERROR: Output directory is empty.\n"
                "Browse to a folder or click 'Fetch Config'."
            )
            if self._running_list:
                self._on_list_done(False)
            return

        # Apply waveform before sweeping (recipe mode always; single mode skips)
        wf    = cfg["wf_type"]
        freq  = cfg["freq_hz"]
        amp   = cfg["amp_vpp"]
        offset = cfg["offset_v"]
        phase  = cfg["phase_deg"]
        if apply_wf:
            try:
                if wf == "Sine":
                    afg.setup_sine(ch, frequency=freq, amplitude=amp, offset=offset)
                    afg.set_phase(ch, phase)
                elif wf == "Square":
                    afg.setup_square(ch, frequency=freq, amplitude=amp, offset=offset)
                    afg.set_phase(ch, phase)
                elif wf == "Ramp":
                    afg.setup_ramp(ch, frequency=freq, amplitude=amp, offset=offset)
                    afg.set_phase(ch, phase)
                elif wf == "Noise":
                    afg.setup_noise(ch, amplitude=amp, offset=offset)
                elif wf == "DC Bias":
                    afg.setup_sine(ch, frequency=1e-6, amplitude=amp, offset=offset)
                elif wf == "ARB (loaded)":
                    afg.set_amplitude(ch, amp)
                    afg.set_offset(ch, offset)
                afg.output_on(ch)
                label = (f"DC Bias {amp:.4g}Vpp offset {offset:+.3g}V"
                         if wf == "DC Bias" else f"{wf} {freq:.4g}Hz {amp:.4g}Vpp")
                self._log(f"  Waveform: {label} → output ON")
            except Exception as exc:
                self._log(f"  WARNING: waveform apply failed: {exc}")

        # Build sweep value list
        start = cfg["start"]
        stop  = cfg["stop"]
        step  = cfg["step"]
        vals, v = [], start
        while v <= stop + 1e-9:
            vals.append(round(v, 8))
            v += step
        if not vals:
            self._log("ERROR: No sweep steps in range.")
            if self._running_list:
                self._on_list_done(False)
            return

        mode   = cfg["mode"]
        prefix = cfg.get("prefix", "ptrap")
        sr     = cfg["sample_rate"]
        nb     = cfg["n_bits"]
        fps    = cfg["files_per_step"]
        settle = cfg["settle_s"]

        self._log(
            f"Sweep: {axis.upper()} electrode  ({self._map.assignment_str(axis)})\n"
            f"  Mode: {mode}  |  "
            f"{len(vals)} steps: {vals[0]:.4g} → {vals[-1]:.4g} "
            f"({'Vpp' if mode == 'amplitude' else 'Hz'})\n"
            f"  {fps} file(s)/step  | settle {settle:.1f} s\n"
            f"  Output: {out_dir}  |  prefix: {prefix}\n"
            f"  {2**nb:,} samples @ {sr:.0f} Hz = {2**nb / sr:.2f} s/file"
        )

        self._worker = _SweepWorker(
            afg=afg, channel=ch,
            mode=mode, values=vals,
            settle_s=settle,
            files_per_step=fps,
            prefix=prefix, output_dir=out_dir,
            sample_rate=sr,
            n_bits=nb,
            daq_host=cfg.get("daq_host", "localhost"),
            daq_rep_port=cfg.get("daq_port", 5552),
            fixed_freq=freq, fixed_amp=amp,
        )
        self._worker.log.connect(self._log)
        self._worker.progress.connect(
            lambda s, t: self._progress_lbl.setText(f"Step {s}/{t}")
        )
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

        self._start_btn.setEnabled(False)
        self._run_list_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)

    def _cancel_sweep(self):
        self._pending_list.clear()   # prevent next step in list from starting
        if self._worker:
            self._worker.cancel()

    def _on_finished(self, ok: bool):
        self._worker = None
        self._progress_lbl.setText("")

        if self._running_list:
            if ok and self._pending_list:
                self._run_next_in_list()
            else:
                self._on_list_done(ok)
        else:
            self._start_btn.setEnabled(True)
            self._run_list_btn.setEnabled(bool(self._sweep_list))
            self._cancel_btn.setEnabled(False)
            self._log("Sweep complete." if ok else "Sweep stopped.")


# ---------------------------------------------------------------------------
# Combined waveform control tab
# ---------------------------------------------------------------------------

def _make_scroll(widget: QWidget) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.NoFrame)
    scroll.setWidget(widget)
    return scroll


class WaveformControlTab(QWidget):
    """
    Paul trap electrode and actuator waveform control.

    Sub-tabs:
        Electrode Map  — WG/CH assignments for X/Y/Z axes
        X Electrode    — full waveform control (Sine/Square/Pulse/Ramp/Noise)
        Y Electrode    — same
        Z Electrode    — same
        Filament       — pulse to SSR
        Flash Lamp     — trigger pulse + DC control
        Sweep          — automated amplitude/frequency sweep with DAQ recording

    Parameters
    ----------
    get_afg : callable
        ``get_afg(wg_index: int)`` → ``AFG2225Controller | None``,
        wg_index is 1, 2, or 3.
    get_nge : callable
        ``get_nge()`` → ``NGESupplyController | None`` — the DC power supply
        used for the flash-lamp control and filament power lines.
    """

    def __init__(self, get_afg, get_nge=None, parent=None):
        super().__init__(parent)
        self._get_afg = get_afg
        self._get_nge = get_nge if get_nge is not None else (lambda: None)
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)

        tabs = QTabWidget()

        # --- Channel Map (electrodes + lock-in reference + power supply) ---
        map_w = QWidget()
        map_v = QVBoxLayout(map_w)
        map_v.setContentsMargins(8, 8, 8, 8)
        self.electrode_map = ElectrodeMapWidget(self._get_afg)
        self.lockin_ref = LockInReferenceGroup(self._get_afg, self.electrode_map)
        self.nge_map = NGEChannelMap(self._get_nge)
        map_v.addWidget(self.electrode_map)
        map_v.addWidget(self.lockin_ref)
        map_v.addWidget(self.nge_map)
        map_v.addStretch()
        tabs.addTab(_make_scroll(map_w), "Channel Map")

        # --- X / Y / Z electrode tabs ---
        for axis in ("x", "y", "z"):
            inner = QWidget()
            vbox  = QVBoxLayout(inner)
            ctrl  = ChannelControlWidget(axis, self.electrode_map)
            # Auto-mirror the lock-in reference when this drive is (re)applied
            # or its output is toggled.
            ctrl._on_applied = self._on_drive_applied
            ctrl._on_output_changed = self._on_drive_output_changed
            setattr(self, f"{axis}_drive", ctrl)
            vbox.addWidget(ctrl)
            tabs.addTab(_make_scroll(inner), f"{axis.upper()} Electrode")

        # --- Filament: trigger (WG3-CH2 pulse to SSR) + power (NGE) ---
        fil_w = QWidget()
        fil_v = QVBoxLayout(fil_w)
        self.filament = PulseGroup(
            "Filament — Trigger (pulse to SSR)", self._get_afg, "WG3", "CH2"
        )
        self.filament_power = NGEControlGroup(
            "Filament — Power (NGE)", "filament_power", self.nge_map,
            default_current_a=3.0, default_voltage_v=5.0,
        )
        self.filament_ramp = FilamentRampWidget(lambda: self.filament)
        fil_v.addWidget(self.filament)
        fil_v.addWidget(self.filament_power)
        fil_v.addWidget(self.filament_ramp)
        fil_v.addStretch()
        tabs.addTab(_make_scroll(fil_w), "Filament")

        # --- Flash Lamp: trigger (WG3-CH1 pulse) + control (NGE) ---
        flash_w = QWidget()
        flash_v = QVBoxLayout(flash_w)
        self.flash_trigger = PulseGroup(
            "Flash Lamp — Trigger (pulse)", self._get_afg, "WG3", "CH1"
        )
        # Flash-lamp defaults: 200 Hz rate, 4 ms pulse.
        self.flash_trigger._freq.setValue(200.0)
        self.flash_trigger._width_ms.setValue(4.0)
        self.flash_control = NGEControlGroup(
            "Flash Lamp — Control (NGE)", "flash_control", self.nge_map,
            default_current_a=0.1, default_voltage_v=4.0,
        )
        flash_v.addWidget(self.flash_trigger)
        flash_v.addWidget(self.flash_control)
        flash_v.addStretch()
        tabs.addTab(_make_scroll(flash_w), "Flash Lamp")

        self.flashlamp = FlashLampAdapter(self.flash_trigger, self.flash_control)

        # --- Sweep ---
        self.sweep = SweepTab(self.electrode_map)
        tabs.addTab(_make_scroll(self.sweep), "Sweep")

        outer.addWidget(tabs)

    def _on_drive_applied(self, axis: str):
        """A drive channel was applied — re-mirror the lock-in reference if it
        mirrors this channel (keeps a square drive -> square reference, etc.)."""
        wg_n = self.electrode_map.get_wg_n(axis)
        ch = self.electrode_map.get_ch(axis)
        self.lockin_ref.notify_drive_applied(wg_n, ch)

    def _on_drive_output_changed(self, axis: str, is_on: bool):
        """A drive channel's output was toggled — mirror the on/off state onto
        the lock-in reference if it mirrors this channel."""
        wg_n = self.electrode_map.get_wg_n(axis)
        ch = self.electrode_map.get_ch(axis)
        self.lockin_ref.notify_drive_output(wg_n, ch, is_on)
