"""
wg_control_tab.py

Combined waveform generator control tab for charge_gui.py.

Replaces the three separate Drive / Filament / Flash Lamp tabs.

Sections:
    Drive           — sine wave, selectable WG / CH, freq / amp / phase
    Filament        — pulse to SSR (0 V low, V_high high), freq + pulse width
    Flash Trigger   — pulse (same as filament)
    Flash Control   — DC voltage
"""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

_WG_OPTIONS  = ["WG1", "WG2", "WG3"]
_CH_OPTIONS  = ["CH1", "CH2"]
_IMP_OPTIONS = ["50 Ω", "High Z"]

_GREEN  = "background-color: #4CAF50; color: white;"
_RED    = "background-color: #F44336; color: white;"
_HINT   = "color: #9E9E9E; font-size: 11px;"
_WARN   = "color: #F44336; font-weight: bold; font-size: 11px;"


def _hint_label() -> QLabel:
    lbl = QLabel("")
    lbl.setStyleSheet(_HINT)
    return lbl


# ---------------------------------------------------------------------------
# Shared header: WG / CH / Load selectors
# ---------------------------------------------------------------------------

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
# Drive — sine wave
# ---------------------------------------------------------------------------

class DriveGroup(QGroupBox):

    def __init__(self, get_afg, parent=None):
        super().__init__("Drive (sine wave)", parent)
        self._get_afg = get_afg
        self._build()

    def _build(self):
        g = QGridLayout(self)
        g.setColumnStretch(7, 1)
        row = 0

        self._wg, self._ch, self._imp = _add_selector_row(
            g, row, "WG1", "CH2", "High Z"
        )
        row += 1

        g.addWidget(QLabel("Frequency:"), row, 0, Qt.AlignRight)
        self._freq = QDoubleSpinBox()
        self._freq.setRange(0.001, 25e6)
        self._freq.setDecimals(3)
        self._freq.setValue(100.0)
        self._freq.setSuffix(" Hz")
        self._freq.setMinimumWidth(130)
        g.addWidget(self._freq, row, 1, 1, 3)
        row += 1

        g.addWidget(QLabel("Amplitude:"), row, 0, Qt.AlignRight)
        self._amp = QDoubleSpinBox()
        self._amp.setRange(0.001, 20.0)
        self._amp.setDecimals(3)
        self._amp.setValue(1.0)
        self._amp.setSuffix(" Vpp")
        self._amp.setMinimumWidth(130)
        g.addWidget(self._amp, row, 1, 1, 3)
        row += 1

        g.addWidget(QLabel("Phase:"), row, 0, Qt.AlignRight)
        self._phase = QDoubleSpinBox()
        self._phase.setRange(-180.0, 180.0)
        self._phase.setDecimals(2)
        self._phase.setValue(0.0)
        self._phase.setSuffix(" °")
        self._phase.setMinimumWidth(130)
        g.addWidget(self._phase, row, 1, 1, 3)
        row += 1

        _add_buttons(g, row, self._apply, self._output_on, self._output_off)
        row += 1

        self._status = QLabel("—")
        self._status.setStyleSheet("color: gray;")
        g.addWidget(self._status, row, 0, 1, 8)

    # -- helpers --

    def _afg_ch(self):
        wg_n = self._wg.currentIndex() + 1
        ch   = self._ch.currentIndex() + 1
        afg  = self._get_afg(wg_n)
        if afg is None or not afg.is_connected:
            self._status.setText(f"WG{wg_n} not connected")
            self._status.setStyleSheet("color: red;")
            return None, None
        return afg, ch

    def _set_impedance(self, afg, ch: int):
        if self._imp.currentIndex() == 1:
            afg.set_load_high_z(ch)
        else:
            afg.set_load_50_ohm(ch)

    def _status_ok(self, msg: str):
        self._status.setText(msg)
        self._status.setStyleSheet("color: green;")

    def _status_err(self, msg: str):
        self._status.setText(msg)
        self._status.setStyleSheet("color: red;")

    # -- slots --

    def _apply(self):
        afg, ch = self._afg_ch()
        if afg is None:
            return
        try:
            self._set_impedance(afg, ch)
            afg.setup_sine(ch, frequency=self._freq.value(),
                           amplitude=self._amp.value(), offset=0.0)
            afg.set_phase(ch, self._phase.value())
            self._status_ok(
                f"Applied — {self._freq.value():.3f} Hz, "
                f"{self._amp.value():.3f} Vpp, {self._phase.value():.1f}°"
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

    # -- public getters for control-loop wiring --

    def get_afg_ch(self):
        """Return (AFG2225Controller | None, ch: int) for current selection."""
        wg_n = self._wg.currentIndex() + 1
        return self._get_afg(wg_n), self._wg.currentIndex() + 1

    def get_freq(self) -> float:
        return self._freq.value()


# ---------------------------------------------------------------------------
# Pulse group — shared by Filament and Flash Trigger
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

        # Amplitude (= V_high)
        g.addWidget(QLabel("High level (V):"), row, 0, Qt.AlignRight)
        self._amp = QDoubleSpinBox()
        self._amp.setRange(0.001, 20.0)
        self._amp.setDecimals(3)
        self._amp.setValue(5.0)
        self._amp.setSuffix(" V")
        self._amp.setMinimumWidth(130)
        g.addWidget(self._amp, row, 1, 1, 3)
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

        # Warning
        self._warn = QLabel("")
        self._warn.setStyleSheet(_WARN)
        g.addWidget(self._warn, row, 0, 1, 8)
        row += 1

        _add_buttons(g, row, self._apply, self._output_on, self._output_off)
        row += 1

        self._status = QLabel("—")
        self._status.setStyleSheet("color: gray;")
        g.addWidget(self._status, row, 0, 1, 8)

        # Cross-update hints
        self._freq.valueChanged.connect(self._update_hints)
        self._width_ms.valueChanged.connect(self._update_hints)
        self._update_hints()

    # -- hint logic --

    def _update_hints(self):
        freq  = self._freq.value()
        width = self._width_ms.value() * 1e-3  # seconds
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

    # -- helpers --

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

    # -- slots --

    def _apply(self):
        afg, ch = self._afg_ch()
        if afg is None:
            return
        freq_hz  = self._freq.value()
        width_s  = self._width_ms.value() * 1e-3
        v_high   = self._amp.value()
        offset   = v_high / 2.0   # keeps 0 V low, v_high V high

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

    # -- public for control-loop --

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
            # DC via sine with ~0 amplitude and the offset set to desired V
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
        """Called externally to change the DC level."""
        self._voltage.setValue(v)
        self._apply()


# ---------------------------------------------------------------------------
# Adapter: FlashLampAdapter
# Wraps flash_trigger (PulseGroup) + flash_control (DCGroup) into a single
# object that satisfies the FlashLampController interface expected by
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
# Combined tab
# ---------------------------------------------------------------------------

class WaveformControlTab(QWidget):
    """
    Single tab containing Drive / Filament / Flash Trigger / Flash Control.

    Parameters
    ----------
    get_afg : callable
        ``get_afg(wg_index: int)`` → ``AFG2225Controller | None``
        wg_index is 1, 2, or 3.
    """

    def __init__(self, get_afg, parent=None):
        super().__init__(parent)
        self._get_afg = get_afg
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)

        inner = QWidget()
        vbox = QVBoxLayout(inner)
        vbox.setSpacing(10)

        self.drive   = DriveGroup(self._get_afg)
        self.filament = PulseGroup(
            "Filament (pulse to SSR)",
            self._get_afg, "WG2", "CH2",
        )
        self.flash_trigger = PulseGroup(
            "Flash Lamp — Trigger (pulse)",
            self._get_afg, "WG3", "CH1",
        )
        self.flash_control = DCGroup(
            "Flash Lamp — Control (DC)",
            self._get_afg, "WG3", "CH2",
        )
        # Combined adapter for ChargeController / PhotonOrderExperiment
        self.flashlamp = FlashLampAdapter(self.flash_trigger, self.flash_control)

        vbox.addWidget(self.drive)
        vbox.addWidget(self.filament)
        vbox.addWidget(self.flash_trigger)
        vbox.addWidget(self.flash_control)
        vbox.addStretch()

        scroll.setWidget(inner)
        outer.addWidget(scroll)
