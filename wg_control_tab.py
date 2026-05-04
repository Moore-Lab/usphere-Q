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

Public attributes on WaveformControlTab (for control-loop wiring):
    electrode_map  : ElectrodeMapWidget
    x_drive        : ChannelControlWidget
    y_drive        : ChannelControlWidget
    z_drive        : ChannelControlWidget
    filament       : PulseGroup
    flash_trigger  : PulseGroup
    flash_control  : DCGroup
    flashlamp      : FlashLampAdapter
"""

from __future__ import annotations

import math

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QTabWidget,
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

    def __init__(self, title: str, get_afg,
                 wg_default: str, ch_default: str,
                 parent=None):
        super().__init__(title, parent)
        self._get_afg = get_afg
        self._wg_default = wg_default
        self._ch_default = ch_default
        self._build()

    def _build(self):
        g = QGridLayout(self)
        g.setColumnStretch(7, 1)
        row = 0

        self._wg, self._ch, self._imp = _add_selector_row(
            g, row, self._wg_default, self._ch_default, "High Z"
        )
        row += 1

        g.addWidget(QLabel("High level (V):"), row, 0, Qt.AlignRight)
        self._amp = QDoubleSpinBox()
        self._amp.setRange(0.001, 20.0)
        self._amp.setDecimals(3)
        self._amp.setValue(5.0)
        self._amp.setSuffix(" V")
        self._amp.setMinimumWidth(130)
        g.addWidget(self._amp, row, 1, 1, 3)
        row += 1

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

        self._freq.valueChanged.connect(self._update_hints)
        self._width_ms.valueChanged.connect(self._update_hints)
        self._update_hints()

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

    def _apply(self):
        afg, ch = self._afg_ch()
        if afg is None:
            return
        freq_hz = self._freq.value()
        width_s = self._width_ms.value() * 1e-3
        v_high  = self._amp.value()
        offset  = v_high / 2.0

        if width_s >= 1.0 / freq_hz:
            self._status_err("Pulse width ≥ period — not applied")
            return
        try:
            self._set_impedance(afg, ch)
            afg.setup_pulse(ch, frequency=freq_hz,
                            amplitude=v_high, offset=offset,
                            width=width_s)
            self._status_ok(
                f"Applied — {freq_hz:.3f} Hz, {width_s*1e3:.3f} ms, "
                f"0 V – {v_high:.3f} V"
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
                        amplitude=v_high, offset=v_high / 2.0,
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
        g = QGridLayout(self)
        g.setColumnStretch(7, 1)
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

        outer.addWidget(tabs)
