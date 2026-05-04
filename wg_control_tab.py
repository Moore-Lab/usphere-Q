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
    x_drive        : ChannelControlWidget
    y_drive        : ChannelControlWidget
    z_drive        : ChannelControlWidget
    filament       : PulseGroup
    flash_trigger  : PulseGroup
    flash_control  : DCGroup
    flashlamp      : FlashLampAdapter
    sweep          : SweepTab
"""

from __future__ import annotations

import math

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QButtonGroup,
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

    Default mapping:
        X → WG1-CH1
        Y → WG1-CH2
        Z → WG2-CH1

    The electrode drive tabs read from this map at call time, so changes
    take effect immediately without restarting.
    """

    _DEFAULTS: dict[str, tuple[str, str]] = {
        "x": ("WG1", "CH1"),
        "y": ("WG1", "CH2"),
        "z": ("WG2", "CH1"),
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
            "Default: X → WG1-CH1,  Y → WG1-CH2,  Z → WG2-CH1"
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
        self._amp.setValue(1.0)
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
        except Exception as e:
            self._set_status_err(f"Error: {e}")

    def _output_on(self):
        afg, ch = self._afg_ch()
        if afg is None:
            return
        try:
            afg.output_on(ch)
            self._set_status_ok("Output ON")
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
        except Exception as e:
            self._set_status_err(f"Error: {e}")

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
# FlashLampAdapter
# Wraps flash_trigger (PulseGroup) + flash_control (DCGroup) into a single
# object matching the FlashLampController interface expected by
# ChargeController and PhotonOrderExperiment.
# ---------------------------------------------------------------------------

class FlashLampAdapter:
    """Combines PulseGroup and DCGroup into a FlashLampController-compatible object."""

    def __init__(self, trigger: PulseGroup, control: DCGroup):
        self._trigger = trigger
        self._control = control

    def enable(self) -> bool:
        return self._trigger.enable()

    def disable(self) -> bool:
        return self._trigger.disable()

    @property
    def is_connected(self) -> bool:
        return self._trigger.is_connected

    def set_flash_rate(self, rate_hz: float):
        self._trigger._freq.setValue(rate_hz)
        self._trigger._apply()

    def set_electrode_voltage(self, voltage_v: float):
        self._control.set_voltage(voltage_v)

    def get_flash_rate(self) -> float:
        return self._trigger._freq.value()

    def get_electrode_voltage(self) -> float:
        return self._control._voltage.value()


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

    _WAVEFORMS     = ["Sine", "Square", "Ramp", "Noise", "ARB (loaded)"]
    _NO_FREQ_TYPES = {"Noise", "ARB (loaded)"}   # disable freq sweep for these

    def __init__(self, electrode_map: "ElectrodeMapWidget", parent=None):
        super().__init__(parent)
        self._map    = electrode_map
        self._worker: _SweepWorker | None = None
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
            elif wf == "ARB (loaded)":
                afg.set_amplitude(ch, amp)
                afg.set_offset(ch, offset)
            self._wf_status.setText(
                f"Applied — {wf}, {freq:.4g} Hz, {amp:.3g} Vpp"
            )
            self._wf_status.setStyleSheet("color:green;")
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
        except Exception as exc:
            self._wf_status.setText(f"Error: {exc}")
            self._wf_status.setStyleSheet("color:red;")

    # ------------------------------------------------------------------
    # Sweep control
    # ------------------------------------------------------------------

    def _build_values(self) -> list:
        start = self._start_spin.value()
        stop  = self._stop_spin.value()
        step  = self._step_spin.value()
        vals, v = [], start
        while v <= stop + 1e-9:
            vals.append(round(v, 8))
            v += step
        return vals

    def _start_sweep(self):
        axis = self._selected_axis()
        afg, ch = self._map.get_afg_ch(axis)

        if afg is None or not afg.is_connected:
            self._log(
                f"ERROR: {axis.upper()} electrode AFG not connected.\n"
                "Connect it in the Electrode Map tab, then return here."
            )
            return

        out_dir = self._dir_edit.text().strip()
        if not out_dir:
            self._log(
                "ERROR: Output directory is empty.\n"
                "Browse to a folder or click 'Fetch Config' to load the DAQ default."
            )
            return

        vals = self._build_values()
        if not vals:
            self._log("ERROR: No sweep steps in range.")
            return

        mode   = "amplitude" if self._amp_rb.isChecked() else "frequency"
        prefix = self._prefix_edit.text().strip() or "ptrap"

        self._log(
            f"Sweep: {axis.upper()} electrode  ({self._map.assignment_str(axis)})\n"
            f"  Mode: {mode}  |  "
            f"{len(vals)} steps: {vals[0]:.4g} → {vals[-1]:.4g} "
            f"({'Vpp' if mode == 'amplitude' else 'Hz'})\n"
            f"  {self._files_spin.value()} file(s)/step  "
            f"| settle {self._settle_spin.value():.1f} s\n"
            f"  Output: {out_dir}  |  prefix: {prefix}\n"
            f"  {2**self._nbits_spin.value():,} samples @ "
            f"{self._rate_spin.value():.0f} Hz = "
            f"{2**self._nbits_spin.value() / self._rate_spin.value():.2f} s/file"
        )

        self._worker = _SweepWorker(
            afg=afg, channel=ch,
            mode=mode, values=vals,
            settle_s=self._settle_spin.value(),
            files_per_step=self._files_spin.value(),
            prefix=prefix, output_dir=out_dir,
            sample_rate=self._rate_spin.value(),
            n_bits=self._nbits_spin.value(),
            daq_host=self._host_edit.text().strip() or "localhost",
            daq_rep_port=self._port_spin.value(),
            fixed_freq=self._wf_freq.value(),
            fixed_amp=self._wf_amp.value(),
        )
        self._worker.log.connect(self._log)
        self._worker.progress.connect(
            lambda s, t: self._progress_lbl.setText(f"Step {s}/{t}")
        )
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

        self._start_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)

    def _cancel_sweep(self):
        if self._worker:
            self._worker.cancel()

    def _on_finished(self, ok: bool):
        self._start_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._progress_lbl.setText("")
        self._log("Sweep complete." if ok else "Sweep stopped.")
        self._worker = None


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
    """

    def __init__(self, get_afg, parent=None):
        super().__init__(parent)
        self._get_afg = get_afg
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)

        tabs = QTabWidget()

        # --- Electrode Map ---
        map_w = QWidget()
        map_v = QVBoxLayout(map_w)
        map_v.setContentsMargins(8, 8, 8, 8)
        self.electrode_map = ElectrodeMapWidget(self._get_afg)
        map_v.addWidget(self.electrode_map)
        map_v.addStretch()
        tabs.addTab(map_w, "Electrode Map")

        # --- X / Y / Z electrode tabs ---
        for axis in ("x", "y", "z"):
            inner = QWidget()
            vbox  = QVBoxLayout(inner)
            ctrl  = ChannelControlWidget(axis, self.electrode_map)
            setattr(self, f"{axis}_drive", ctrl)
            vbox.addWidget(ctrl)
            tabs.addTab(_make_scroll(inner), f"{axis.upper()} Electrode")

        # --- Filament ---
        fil_w = QWidget()
        fil_v = QVBoxLayout(fil_w)
        self.filament = PulseGroup(
            "Filament (pulse to SSR)", self._get_afg, "WG2", "CH2"
        )
        fil_v.addWidget(self.filament)
        fil_v.addStretch()
        tabs.addTab(_make_scroll(fil_w), "Filament")

        # --- Flash Lamp ---
        flash_w = QWidget()
        flash_v = QVBoxLayout(flash_w)
        self.flash_trigger = PulseGroup(
            "Flash Lamp — Trigger (pulse)", self._get_afg, "WG3", "CH1"
        )
        self.flash_control = DCGroup(
            "Flash Lamp — Control (DC)", self._get_afg, "WG3", "CH2"
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
