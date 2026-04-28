"""
sr530_gui.py

Standalone PyQt5 GUI for the Stanford Research Systems SR530 Lock-In Amplifier.

Run with:
    python sr530_gui.py

Requires:
    pip install PyQt5 pyserial
    pip install pyqtgraph   # optional - enables live X-output plot in Monitor tab
"""

from __future__ import annotations

import sys
import time

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from sr530_controller import (
    SR530Controller,
    SENSITIVITY_TABLE,
    PRE_TIME_CONSTANT_TABLE,
    POST_TIME_CONSTANT_TABLE,
    HARMONIC_MODE_TABLE,
    ENBW_TABLE,
    TRIGGER_MODE_TABLE,
    DISPLAY_SELECT_TABLE,
    REMOTE_MODE_TABLE,
    KEY_TABLE,
)

_RESERVE_LABELS = ["Low Noise", "Normal", "High Reserve"]


# ---------------------------------------------------------------------------
# Background polling thread
# ---------------------------------------------------------------------------

class _PollThread(QThread):
    """Calls snapshot() on a background thread at a configurable interval."""

    snapshot_ready = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, controller: SR530Controller, interval_ms: int = 500):
        super().__init__()
        self._ctrl = controller
        self._interval_ms = max(100, interval_ms)
        self._running = False

    def set_interval(self, ms: int) -> None:
        self._interval_ms = max(100, ms)

    def run(self) -> None:
        self._running = True
        while self._running:
            t0 = time.time()
            try:
                snap = self._ctrl.snapshot()
                self.snapshot_ready.emit(snap)
            except Exception as exc:
                self.error.emit(str(exc))
                self._running = False
                break
            elapsed = time.time() - t0
            sleep_ms = self._interval_ms - int(elapsed * 1000)
            if sleep_ms > 0:
                self.msleep(sleep_ms)

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
# Connection tab
# ---------------------------------------------------------------------------

class ConnectionTab(QWidget):
    """Serial port connection panel."""

    connected = pyqtSignal(object)   # SR530Controller
    disconnected = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ctrl: SR530Controller | None = None
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(10)
        outer.setContentsMargins(10, 10, 10, 10)

        grp = QGroupBox("Serial settings")
        form = QFormLayout(grp)

        self._port_edit = QLineEdit("COM11")
        form.addRow("Port:", self._port_edit)

        self._baud_edit = QLineEdit("19200")
        form.addRow("Baud rate:", self._baud_edit)

        self._timeout_spin = QDoubleSpinBox()
        self._timeout_spin.setRange(0.5, 30.0)
        self._timeout_spin.setValue(2.0)
        self._timeout_spin.setSuffix(" s")
        self._timeout_spin.setMaximumWidth(110)
        form.addRow("Timeout:", self._timeout_spin)

        self._echo_cb = QCheckBox("Echo mode (SW2-6 DOWN)")
        self._echo_cb.setToolTip(
            "Tick if SW2 switch 6 is DOWN (terminal/echo mode).\n"
            "Leave unticked for normal computer control (SW2-6 UP)."
        )
        form.addRow("", self._echo_cb)

        outer.addWidget(grp)

        btn_row = QHBoxLayout()

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setMinimumWidth(100)
        self._connect_btn.clicked.connect(self._on_connect)
        btn_row.addWidget(self._connect_btn)

        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setMinimumWidth(100)
        self._disconnect_btn.setEnabled(False)
        self._disconnect_btn.clicked.connect(self._on_disconnect)
        btn_row.addWidget(self._disconnect_btn)

        self._test_btn = QPushButton("Test (read frequency)")
        self._test_btn.setMinimumWidth(170)
        self._test_btn.setEnabled(False)
        self._test_btn.clicked.connect(self._on_test)
        btn_row.addWidget(self._test_btn)

        btn_row.addStretch()
        outer.addLayout(btn_row)

        self._status_lbl = QLabel("Not connected")
        self._status_lbl.setStyleSheet("color: gray; font-size: 13px;")
        outer.addWidget(self._status_lbl)

        log_grp = QGroupBox("Connection log")
        log_layout = QVBoxLayout(log_grp)
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(220)
        log_layout.addWidget(self._log)
        outer.addWidget(log_grp)

        outer.addStretch()

    def _log_msg(self, msg: str, color: str = "black") -> None:
        self._log.append(
            f'<span style="color:{color};">[{time.strftime("%H:%M:%S")}] {msg}</span>'
        )

    def _on_connect(self) -> None:
        port = self._port_edit.text().strip()
        try:
            baud = int(self._baud_edit.text())
        except ValueError:
            baud = 9600
        timeout = self._timeout_spin.value()

        self._status_lbl.setText("Connecting...")
        self._status_lbl.setStyleSheet("color: #FF9800; font-size: 13px;")
        QApplication.processEvents()

        try:
            ctrl = SR530Controller(port, baudrate=baud, timeout=timeout,
                                   echo=self._echo_cb.isChecked())
            if not ctrl.connect():
                raise RuntimeError("connect() returned False")
            self._ctrl = ctrl
            self._log_msg(f"Connected to {port} at {baud} baud", "green")
            self._status_lbl.setText(f"Connected - {port}")
            self._status_lbl.setStyleSheet("color: green; font-size: 13px;")
            self._connect_btn.setEnabled(False)
            self._disconnect_btn.setEnabled(True)
            self._test_btn.setEnabled(True)
            self.connected.emit(ctrl)
        except Exception as exc:
            self._log_msg(f"Failed: {exc}", "red")
            self._status_lbl.setText(f"Error: {exc}")
            self._status_lbl.setStyleSheet("color: red; font-size: 13px;")

    def _on_disconnect(self) -> None:
        if self._ctrl:
            try:
                self._ctrl.disconnect()
            except Exception:
                pass
            self._ctrl = None
        self._log_msg("Disconnected", "gray")
        self._status_lbl.setText("Disconnected")
        self._status_lbl.setStyleSheet("color: gray; font-size: 13px;")
        self._connect_btn.setEnabled(True)
        self._disconnect_btn.setEnabled(False)
        self._test_btn.setEnabled(False)
        self.disconnected.emit()

    def _on_test(self) -> None:
        if not self._ctrl:
            return
        try:
            freq = self._ctrl.get_frequency()
            self._log_msg(f"Reference frequency: {freq:.4f} Hz", "#1565C0")
        except Exception as exc:
            self._log_msg(f"Test failed: {exc}", "red")

    @property
    def controller(self) -> SR530Controller | None:
        return self._ctrl


# ---------------------------------------------------------------------------
# Parameters tab
# ---------------------------------------------------------------------------

class ParametersTab(QWidget):
    """Set and read all SR530 primary control parameters."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ctrl: SR530Controller | None = None
        self._controls: list[QWidget] = []
        self._build_ui()

    def set_controller(self, ctrl: SR530Controller | None) -> None:
        self._ctrl = ctrl
        enabled = ctrl is not None
        for w in self._controls:
            w.setEnabled(enabled)
        if enabled:
            self._read_all()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(10)
        outer.setContentsMargins(10, 10, 10, 10)

        # Reference
        ref_grp = QGroupBox("Reference")
        ref_form = QFormLayout(ref_grp)

        self._phase_spin = QDoubleSpinBox()
        self._phase_spin.setRange(-180.0, 180.0)
        self._phase_spin.setDecimals(2)
        self._phase_spin.setSingleStep(0.1)
        self._phase_spin.setSuffix(" deg")
        self._phase_spin.setMaximumWidth(130)
        self._phase_spin.setEnabled(False)
        ref_form.addRow("Phase:", self._phase_spin)
        self._controls.append(self._phase_spin)

        self._auto_phase_btn = QPushButton("Auto-phase")
        self._auto_phase_btn.setMaximumWidth(120)
        self._auto_phase_btn.setEnabled(False)
        self._auto_phase_btn.clicked.connect(self._on_auto_phase)
        ref_form.addRow("", self._auto_phase_btn)
        self._controls.append(self._auto_phase_btn)

        outer.addWidget(ref_grp)

        # Amplifier
        amp_grp = QGroupBox("Amplifier")
        amp_form = QFormLayout(amp_grp)

        self._sens_combo = QComboBox()
        for idx in sorted(SENSITIVITY_TABLE):
            self._sens_combo.addItem(SENSITIVITY_TABLE[idx], idx)
        self._sens_combo.setMaximumWidth(200)
        self._sens_combo.setEnabled(False)
        amp_form.addRow("Sensitivity:", self._sens_combo)
        self._controls.append(self._sens_combo)

        self._reserve_combo = QComboBox()
        for label in _RESERVE_LABELS:
            self._reserve_combo.addItem(label)
        self._reserve_combo.setMaximumWidth(160)
        self._reserve_combo.setEnabled(False)
        amp_form.addRow("Dynamic reserve:", self._reserve_combo)
        self._controls.append(self._reserve_combo)

        outer.addWidget(amp_grp)

        # Filters
        filt_grp = QGroupBox("Filters")
        filt_form = QFormLayout(filt_grp)

        self._pre_tc_combo = QComboBox()
        for idx in sorted(PRE_TIME_CONSTANT_TABLE):
            self._pre_tc_combo.addItem(PRE_TIME_CONSTANT_TABLE[idx], idx)
        self._pre_tc_combo.setMaximumWidth(160)
        self._pre_tc_combo.setEnabled(False)
        filt_form.addRow("Pre time constant:", self._pre_tc_combo)
        self._controls.append(self._pre_tc_combo)

        self._post_tc_combo = QComboBox()
        for idx in sorted(POST_TIME_CONSTANT_TABLE):
            self._post_tc_combo.addItem(POST_TIME_CONSTANT_TABLE[idx], idx)
        self._post_tc_combo.setMaximumWidth(160)
        self._post_tc_combo.setEnabled(False)
        filt_form.addRow("Post time constant:", self._post_tc_combo)
        self._controls.append(self._post_tc_combo)

        self._bandpass_cb = QCheckBox("Bandpass filter (signal channel)")
        self._bandpass_cb.setEnabled(False)
        filt_form.addRow("", self._bandpass_cb)
        self._controls.append(self._bandpass_cb)

        self._notch_cb = QCheckBox("Line notch filter (60 Hz)")
        self._notch_cb.setEnabled(False)
        filt_form.addRow("", self._notch_cb)
        self._controls.append(self._notch_cb)

        self._notch2x_cb = QCheckBox("2x line notch filter (120 Hz)")
        self._notch2x_cb.setEnabled(False)
        filt_form.addRow("", self._notch2x_cb)
        self._controls.append(self._notch2x_cb)

        outer.addWidget(filt_grp)

        # Buttons
        btn_row = QHBoxLayout()

        self._apply_btn = QPushButton("Apply all")
        self._apply_btn.setMinimumWidth(120)
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self._apply_all)
        btn_row.addWidget(self._apply_btn)
        self._controls.append(self._apply_btn)

        self._read_btn = QPushButton("Read from instrument")
        self._read_btn.setMinimumWidth(170)
        self._read_btn.setEnabled(False)
        self._read_btn.clicked.connect(self._read_all)
        btn_row.addWidget(self._read_btn)
        self._controls.append(self._read_btn)

        btn_row.addStretch()
        outer.addLayout(btn_row)

        self._status_lbl = QLabel("-")
        self._status_lbl.setStyleSheet("color: gray;")
        outer.addWidget(self._status_lbl)

        outer.addStretch()

    def _set_combo_by_data(self, combo: QComboBox, value: int) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.setCurrentIndex(i)
                return

    def _on_auto_phase(self) -> None:
        if not self._ctrl:
            return
        try:
            self._ctrl.auto_phase()
            self._status_lbl.setText(f"Auto-phase triggered at {time.strftime('%H:%M:%S')}")
            self._status_lbl.setStyleSheet("color: green;")
        except Exception as exc:
            self._status_lbl.setText(f"Auto-phase error: {exc}")
            self._status_lbl.setStyleSheet("color: red;")

    def _apply_all(self) -> None:
        if not self._ctrl:
            return
        errors = []
        for label, fn in [
            ("phase",    lambda: self._ctrl.set_phase(self._phase_spin.value())),
            ("sens",     lambda: self._ctrl.set_sensitivity(self._sens_combo.currentData())),
            ("reserve",  lambda: self._ctrl.set_dynamic_reserve(self._reserve_combo.currentIndex())),
            ("pre TC",   lambda: self._ctrl.set_pre_time_constant(self._pre_tc_combo.currentData())),
            ("post TC",  lambda: self._ctrl.set_post_time_constant(self._post_tc_combo.currentData())),
            ("bandpass", lambda: self._ctrl.set_bandpass_filter(self._bandpass_cb.isChecked())),
            ("notch",    lambda: self._ctrl.set_line_notch(self._notch_cb.isChecked())),
            ("2x notch", lambda: self._ctrl.set_2x_line_notch(self._notch2x_cb.isChecked())),
        ]:
            try:
                fn()
            except Exception as e:
                errors.append(f"{label}: {e}")

        if errors:
            self._status_lbl.setText("Errors: " + "; ".join(errors))
            self._status_lbl.setStyleSheet("color: red;")
        else:
            self._status_lbl.setText(f"Applied at {time.strftime('%H:%M:%S')}")
            self._status_lbl.setStyleSheet("color: green;")

    def _read_all(self) -> None:
        if not self._ctrl:
            return
        try:
            self._phase_spin.setValue(self._ctrl.get_phase())
            self._set_combo_by_data(self._sens_combo, self._ctrl.get_sensitivity())
            self._reserve_combo.setCurrentIndex(self._ctrl.get_dynamic_reserve())
            self._set_combo_by_data(self._pre_tc_combo, self._ctrl.get_pre_time_constant())
            self._set_combo_by_data(self._post_tc_combo, self._ctrl.get_post_time_constant())
            self._bandpass_cb.setChecked(self._ctrl.get_bandpass_filter())
            self._notch_cb.setChecked(self._ctrl.get_line_notch())
            self._notch2x_cb.setChecked(self._ctrl.get_2x_line_notch())
            self._status_lbl.setText(f"Read at {time.strftime('%H:%M:%S')}")
            self._status_lbl.setStyleSheet("color: green;")
        except Exception as exc:
            self._status_lbl.setText(f"Read error: {exc}")
            self._status_lbl.setStyleSheet("color: red;")


# ---------------------------------------------------------------------------
# Advanced tab
# ---------------------------------------------------------------------------

class AdvancedTab(QWidget):
    """Advanced instrument settings: harmonic, trigger, expand, ENBW,
    manual offsets, analog I/O, remote/local, key simulation."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ctrl: SR530Controller | None = None
        self._controls: list[QWidget] = []
        self._build_ui()

    def set_controller(self, ctrl: SR530Controller | None) -> None:
        self._ctrl = ctrl
        enabled = ctrl is not None
        for w in self._controls:
            w.setEnabled(enabled)
        if enabled:
            self._read_all()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        main = QVBoxLayout(content)
        main.setSpacing(8)
        main.setContentsMargins(10, 10, 10, 10)

        # ---- Top row: two columns ----
        top_row = QHBoxLayout()

        # Left column
        left_col = QVBoxLayout()

        # Reference Input group
        ref_grp = QGroupBox("Reference Input")
        ref_form = QFormLayout(ref_grp)

        self._harmonic_combo = QComboBox()
        for k, v in HARMONIC_MODE_TABLE.items():
            self._harmonic_combo.addItem(v, k)
        self._harmonic_combo.setEnabled(False)
        ref_form.addRow("Harmonic mode:", self._harmonic_combo)
        self._controls.append(self._harmonic_combo)

        self._trigger_combo = QComboBox()
        for k, v in TRIGGER_MODE_TABLE.items():
            self._trigger_combo.addItem(v, k)
        self._trigger_combo.setEnabled(False)
        ref_form.addRow("Trigger mode:", self._trigger_combo)
        self._controls.append(self._trigger_combo)

        left_col.addWidget(ref_grp)

        # Output Configuration group
        out_cfg_grp = QGroupBox("Output Configuration")
        out_cfg_form = QFormLayout(out_cfg_grp)

        self._display_combo = QComboBox()
        for k, v in DISPLAY_SELECT_TABLE.items():
            self._display_combo.addItem(v, k)
        self._display_combo.setEnabled(False)
        out_cfg_form.addRow("Display (Q1/Q2):", self._display_combo)
        self._controls.append(self._display_combo)

        self._expand1_cb = QCheckBox("Channel 1 x10")
        self._expand1_cb.setEnabled(False)
        self._expand1_cb.setToolTip("Multiplies Ch1 meter and output by x10 (does not affect QX BNC)")
        out_cfg_form.addRow("Expand:", self._expand1_cb)
        self._controls.append(self._expand1_cb)

        self._expand2_cb = QCheckBox("Channel 2 x10")
        self._expand2_cb.setEnabled(False)
        out_cfg_form.addRow("", self._expand2_cb)
        self._controls.append(self._expand2_cb)

        self._enbw_combo = QComboBox()
        for k, v in ENBW_TABLE.items():
            self._enbw_combo.addItem(v, k)
        self._enbw_combo.setEnabled(False)
        self._enbw_combo.setToolTip("Equivalent Noise Bandwidth (for noise measurements)")
        out_cfg_form.addRow("ENBW:", self._enbw_combo)
        self._controls.append(self._enbw_combo)

        left_col.addWidget(out_cfg_grp)
        top_row.addLayout(left_col)

        # Right column
        right_col = QVBoxLayout()

        # Instrument Control group
        inst_grp = QGroupBox("Instrument Control")
        inst_form = QFormLayout(inst_grp)

        self._remote_combo = QComboBox()
        for k, v in REMOTE_MODE_TABLE.items():
            self._remote_combo.addItem(v, k)
        self._remote_combo.setEnabled(False)
        self._remote_combo.setToolTip(
            "Local: front panel active\n"
            "Remote: front panel locked (LOCAL key restores)\n"
            "Lockout: only I,0 command restores"
        )
        inst_form.addRow("Remote mode:", self._remote_combo)
        self._controls.append(self._remote_combo)

        self._preamp_lbl = QLabel("-")
        inst_form.addRow("Pre-amplifier:", self._preamp_lbl)

        key_row_layout = QHBoxLayout()
        self._key_spin = QSpinBox()
        self._key_spin.setRange(1, 32)
        self._key_spin.setValue(1)
        self._key_spin.setMaximumWidth(55)
        self._key_spin.setEnabled(False)
        key_row_layout.addWidget(self._key_spin)
        self._controls.append(self._key_spin)

        self._key_btn = QPushButton("Send key")
        self._key_btn.setEnabled(False)
        self._key_btn.clicked.connect(self._on_send_key)
        key_row_layout.addWidget(self._key_btn)
        key_row_layout.addStretch()
        inst_form.addRow("Key (1-32):", key_row_layout)
        self._controls.append(self._key_btn)

        self._key_name_lbl = QLabel("-")
        self._key_name_lbl.setStyleSheet("color: gray; font-style: italic;")
        self._key_spin.valueChanged.connect(self._on_key_spin_changed)
        inst_form.addRow("", self._key_name_lbl)

        # Auto-offset buttons
        ao_row = QHBoxLayout()
        for label, slot in [("Auto-offset X", self._on_ao_x),
                             ("Auto-offset Y", self._on_ao_y),
                             ("Auto-offset R", self._on_ao_r)]:
            btn = QPushButton(label)
            btn.setEnabled(False)
            btn.clicked.connect(slot)
            ao_row.addWidget(btn)
            self._controls.append(btn)
        ao_row.addStretch()
        inst_form.addRow("Auto-offset:", ao_row)

        right_col.addWidget(inst_grp)
        right_col.addStretch()
        top_row.addLayout(right_col)

        main.addLayout(top_row)

        # ---- Manual Offsets (full width) ----
        off_grp = QGroupBox("Manual Offsets  (fraction of full-scale, -1.0 to +1.0)")
        off_layout = QHBoxLayout(off_grp)

        for axis, en_attr, val_attr in [
            ("X", "_ox_en_cb", "_ox_val_spin"),
            ("Y", "_oy_en_cb", "_oy_val_spin"),
            ("R", "_or_en_cb", "_or_val_spin"),
        ]:
            col_grp = QGroupBox(f"{axis} Offset")
            col_form = QFormLayout(col_grp)

            en_cb = QCheckBox("Enable")
            en_cb.setEnabled(False)
            col_form.addRow("", en_cb)
            setattr(self, en_attr, en_cb)
            self._controls.append(en_cb)

            val_spin = QDoubleSpinBox()
            val_spin.setRange(-1.0, 1.0)
            val_spin.setDecimals(4)
            val_spin.setSingleStep(0.01)
            val_spin.setSuffix(" FS")
            val_spin.setToolTip(
                "Offset in units of full-scale (1.0 = current sensitivity FS).\n"
                "Applied as: offset_volts = value x sensitivity_volts"
            )
            val_spin.setEnabled(False)
            col_form.addRow("Value:", val_spin)
            setattr(self, val_attr, val_spin)
            self._controls.append(val_spin)

            off_layout.addWidget(col_grp)

        main.addWidget(off_grp)

        # ---- Analog I/O (full width) ----
        aio_grp = QGroupBox("Analog I/O  (Rear Panel BNC)")
        aio_layout = QHBoxLayout(aio_grp)

        # A/D Inputs X1-X4
        in_grp = QGroupBox("A/D Inputs (read-only, +-10.24 V)")
        in_grid = QGridLayout(in_grp)
        self._adc_labels: dict[int, QLabel] = {}
        for row_i, n in enumerate([1, 2, 3, 4]):
            in_grid.addWidget(QLabel(f"X{n}:"), row_i, 0)
            val_lbl = QLabel("-")
            val_lbl.setStyleSheet("font-family: monospace; min-width: 100px;")
            in_grid.addWidget(val_lbl, row_i, 1)
            self._adc_labels[n] = val_lbl

        self._read_in_btn = QPushButton("Read all inputs")
        self._read_in_btn.setEnabled(False)
        self._read_in_btn.clicked.connect(self._on_read_inputs)
        in_grid.addWidget(self._read_in_btn, 4, 0, 1, 2)
        self._controls.append(self._read_in_btn)
        aio_layout.addWidget(in_grp)

        # D/A Outputs X5, X6
        da_grp = QGroupBox("D/A Outputs (+-10.238 V)")
        da_form = QFormLayout(da_grp)
        self._da_spins: dict[int, QDoubleSpinBox] = {}

        for n in (5, 6):
            row_w = QHBoxLayout()
            spin = QDoubleSpinBox()
            spin.setRange(-10.238, 10.238)
            spin.setDecimals(3)
            spin.setSingleStep(0.1)
            spin.setSuffix(" V")
            spin.setMaximumWidth(120)
            spin.setEnabled(False)
            row_w.addWidget(spin)
            self._da_spins[n] = spin
            self._controls.append(spin)

            set_btn = QPushButton("Set")
            set_btn.setMaximumWidth(45)
            set_btn.setEnabled(False)
            _n = n
            set_btn.clicked.connect(lambda _, _nn=_n: self._on_set_da(_nn))
            row_w.addWidget(set_btn)
            self._controls.append(set_btn)

            read_btn = QPushButton("Read")
            read_btn.setMaximumWidth(50)
            read_btn.setEnabled(False)
            read_btn.clicked.connect(lambda _, _nn=_n: self._on_read_da(_nn))
            row_w.addWidget(read_btn)
            row_w.addStretch()
            self._controls.append(read_btn)

            da_form.addRow(f"X{n}:", row_w)

        aio_layout.addWidget(da_grp)
        main.addWidget(aio_grp)

        # ---- Buttons row ----
        btn_row = QHBoxLayout()

        self._read_btn = QPushButton("Read all from instrument")
        self._read_btn.setMinimumWidth(200)
        self._read_btn.setEnabled(False)
        self._read_btn.clicked.connect(self._read_all)
        btn_row.addWidget(self._read_btn)
        self._controls.append(self._read_btn)

        self._apply_btn = QPushButton("Apply all")
        self._apply_btn.setMinimumWidth(120)
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self._apply_all)
        btn_row.addWidget(self._apply_btn)
        self._controls.append(self._apply_btn)

        btn_row.addStretch()
        main.addLayout(btn_row)

        self._status_lbl = QLabel("-")
        self._status_lbl.setStyleSheet("color: gray;")
        main.addWidget(self._status_lbl)

        main.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll)

        # Initialise key name label
        self._on_key_spin_changed(1)

    # --- helpers ---

    def _set_combo_by_data(self, combo: QComboBox, value: int) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.setCurrentIndex(i)
                return

    def _on_key_spin_changed(self, n: int) -> None:
        self._key_name_lbl.setText(KEY_TABLE.get(n, "?"))

    # --- slots ---

    def _on_send_key(self) -> None:
        if not self._ctrl:
            return
        n = self._key_spin.value()
        try:
            self._ctrl.send_key(n)
            self._status_lbl.setText(
                f"Key {n} ({KEY_TABLE.get(n,'?')}) sent at {time.strftime('%H:%M:%S')}"
            )
            self._status_lbl.setStyleSheet("color: green;")
        except Exception as exc:
            self._status_lbl.setText(f"Key error: {exc}")
            self._status_lbl.setStyleSheet("color: red;")

    def _on_ao_x(self) -> None:
        if self._ctrl:
            try:
                self._ctrl.auto_offset_x()
                self._status_lbl.setText("Auto-offset X sent")
                self._status_lbl.setStyleSheet("color: green;")
            except Exception as exc:
                self._status_lbl.setText(f"AX error: {exc}")
                self._status_lbl.setStyleSheet("color: red;")

    def _on_ao_y(self) -> None:
        if self._ctrl:
            try:
                self._ctrl.auto_offset_y()
                self._status_lbl.setText("Auto-offset Y sent")
                self._status_lbl.setStyleSheet("color: green;")
            except Exception as exc:
                self._status_lbl.setText(f"AY error: {exc}")
                self._status_lbl.setStyleSheet("color: red;")

    def _on_ao_r(self) -> None:
        if self._ctrl:
            try:
                self._ctrl.auto_offset_r()
                self._status_lbl.setText("Auto-offset R sent")
                self._status_lbl.setStyleSheet("color: green;")
            except Exception as exc:
                self._status_lbl.setText(f"AR error: {exc}")
                self._status_lbl.setStyleSheet("color: red;")

    def _on_read_inputs(self) -> None:
        if not self._ctrl:
            return
        for n in (1, 2, 3, 4):
            try:
                v = self._ctrl.read_analog_input(n)
                self._adc_labels[n].setText(f"{v:+.6f} V")
            except Exception as exc:
                self._adc_labels[n].setText(f"ERR: {exc}")

    def _on_set_da(self, n: int) -> None:
        if not self._ctrl:
            return
        v = self._da_spins[n].value()
        try:
            self._ctrl.set_da_output(n, v)
            self._status_lbl.setText(
                f"X{n} set to {v:.3f} V at {time.strftime('%H:%M:%S')}"
            )
            self._status_lbl.setStyleSheet("color: green;")
        except Exception as exc:
            self._status_lbl.setText(f"D/A error: {exc}")
            self._status_lbl.setStyleSheet("color: red;")

    def _on_read_da(self, n: int) -> None:
        if not self._ctrl:
            return
        try:
            v = self._ctrl.get_da_output(n)
            self._da_spins[n].setValue(v)
        except Exception as exc:
            self._status_lbl.setText(f"D/A read error: {exc}")
            self._status_lbl.setStyleSheet("color: red;")

    def _read_all(self) -> None:
        if not self._ctrl:
            return
        errors = []

        def _try(label, fn, apply):
            try:
                apply(fn())
            except Exception as e:
                errors.append(f"{label}: {e}")

        _try("harmonic", self._ctrl.get_harmonic_mode,
             lambda v: self._set_combo_by_data(self._harmonic_combo, v))
        _try("trigger", self._ctrl.get_trigger_mode,
             lambda v: self._set_combo_by_data(self._trigger_combo, v))
        _try("display", self._ctrl.get_display_select,
             lambda v: self._set_combo_by_data(self._display_combo, v))
        _try("expand1", lambda: self._ctrl.get_expand(1),
             self._expand1_cb.setChecked)
        _try("expand2", lambda: self._ctrl.get_expand(2),
             self._expand2_cb.setChecked)
        _try("enbw", self._ctrl.get_enbw,
             lambda v: self._set_combo_by_data(self._enbw_combo, v))
        _try("remote", self._ctrl.get_remote_mode,
             lambda v: self._set_combo_by_data(self._remote_combo, v))
        _try("preamp", self._ctrl.get_preamp_status,
             lambda v: self._preamp_lbl.setText("Connected" if v else "Not connected"))
        _try("offset X", self._ctrl.get_offset_x_enabled,
             self._ox_en_cb.setChecked)
        _try("offset Y", self._ctrl.get_offset_y_enabled,
             self._oy_en_cb.setChecked)
        _try("offset R", self._ctrl.get_offset_r_enabled,
             self._or_en_cb.setChecked)

        if errors:
            self._status_lbl.setText("Read errors: " + "; ".join(errors))
            self._status_lbl.setStyleSheet("color: red;")
        else:
            self._status_lbl.setText(f"Read at {time.strftime('%H:%M:%S')}")
            self._status_lbl.setStyleSheet("color: green;")

    def _apply_all(self) -> None:
        if not self._ctrl:
            return
        errors = []

        def _try(label, fn):
            try:
                fn()
            except Exception as e:
                errors.append(f"{label}: {e}")

        _try("harmonic", lambda: self._ctrl.set_harmonic_mode(self._harmonic_combo.currentData()))
        _try("trigger",  lambda: self._ctrl.set_trigger_mode(self._trigger_combo.currentData()))
        _try("display",  lambda: self._ctrl.set_display_select(self._display_combo.currentData()))
        _try("expand1",  lambda: self._ctrl.set_expand(1, self._expand1_cb.isChecked()))
        _try("expand2",  lambda: self._ctrl.set_expand(2, self._expand2_cb.isChecked()))
        _try("enbw",     lambda: self._ctrl.set_enbw(self._enbw_combo.currentData()))
        _try("remote",   lambda: self._ctrl.set_remote_mode(self._remote_combo.currentData()))

        # Offsets: convert FS fraction to volts
        try:
            sens_v = self._ctrl.get_sensitivity_volts()
            _try("offset X", lambda: self._ctrl.set_offset_x(
                self._ox_en_cb.isChecked(), self._ox_val_spin.value() * sens_v))
            _try("offset Y", lambda: self._ctrl.set_offset_y(
                self._oy_en_cb.isChecked(), self._oy_val_spin.value() * sens_v))
            _try("offset R", lambda: self._ctrl.set_offset_r(
                self._or_en_cb.isChecked(), self._or_val_spin.value() * sens_v))
        except Exception as e:
            errors.append(f"sensitivity read: {e}")

        if errors:
            self._status_lbl.setText("Errors: " + "; ".join(errors))
            self._status_lbl.setStyleSheet("color: red;")
        else:
            self._status_lbl.setText(f"Applied at {time.strftime('%H:%M:%S')}")
            self._status_lbl.setStyleSheet("color: green;")


# ---------------------------------------------------------------------------
# Monitor tab
# ---------------------------------------------------------------------------

class MonitorTab(QWidget):
    """Live readout of X/Y/R/theta with optional auto-refresh and live plot."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ctrl: SR530Controller | None = None
        self._poll_thread: _PollThread | None = None
        self._history_x: list[float] = []
        self._build_ui()

    def set_controller(self, ctrl: SR530Controller | None) -> None:
        self._ctrl = ctrl
        enabled = ctrl is not None
        self._auto_cb.setEnabled(enabled)
        self._read_now_btn.setEnabled(enabled)
        if not enabled:
            self._stop_polling()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(10)
        outer.setContentsMargins(10, 10, 10, 10)

        refresh_row = QHBoxLayout()
        self._auto_cb = QCheckBox("Auto-refresh every")
        self._auto_cb.setEnabled(False)
        self._auto_cb.toggled.connect(self._on_auto_toggle)
        refresh_row.addWidget(self._auto_cb)

        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(100, 10000)
        self._interval_spin.setValue(500)
        self._interval_spin.setSuffix(" ms")
        self._interval_spin.setMaximumWidth(110)
        self._interval_spin.valueChanged.connect(self._on_interval_changed)
        refresh_row.addWidget(self._interval_spin)

        self._read_now_btn = QPushButton("Read now")
        self._read_now_btn.setMinimumWidth(90)
        self._read_now_btn.setEnabled(False)
        self._read_now_btn.clicked.connect(self._read_once)
        refresh_row.addWidget(self._read_now_btn)

        refresh_row.addStretch()
        outer.addLayout(refresh_row)

        out_grp = QGroupBox("Outputs")
        g_out = self._build_outputs_grid()
        out_grp.setLayout(g_out)
        outer.addWidget(out_grp)

        stat_grp = QGroupBox("Status")
        sh = QHBoxLayout(stat_grp)

        self._overload_lbl = QLabel("Overload: -")
        self._overload_lbl.setAlignment(Qt.AlignCenter)
        self._overload_lbl.setMinimumWidth(120)
        self._overload_lbl.setStyleSheet(
            "padding: 4px 12px; border-radius: 4px;"
            "background: #BDBDBD; color: white; font-weight: bold;"
        )
        sh.addWidget(self._overload_lbl)

        self._unlock_lbl = QLabel("Reference: -")
        self._unlock_lbl.setAlignment(Qt.AlignCenter)
        self._unlock_lbl.setMinimumWidth(160)
        self._unlock_lbl.setStyleSheet(
            "padding: 4px 12px; border-radius: 4px;"
            "background: #BDBDBD; color: white; font-weight: bold;"
        )
        sh.addWidget(self._unlock_lbl)

        self._noref_lbl = QLabel("No Ref: -")
        self._noref_lbl.setAlignment(Qt.AlignCenter)
        self._noref_lbl.setMinimumWidth(120)
        self._noref_lbl.setStyleSheet(
            "padding: 4px 12px; border-radius: 4px;"
            "background: #BDBDBD; color: white; font-weight: bold;"
        )
        sh.addWidget(self._noref_lbl)

        sh.addStretch()
        outer.addWidget(stat_grp)

        self._init_plot(outer)

    def _build_outputs_grid(self):
        g = QGridLayout()
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)

        def _big():
            l = QLabel("-")
            l.setStyleSheet("font-size: 18px; font-weight: bold;")
            l.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            return l

        def _small():
            l = QLabel("-")
            l.setStyleSheet("color: #555;")
            return l

        g.addWidget(QLabel("X (in-phase):"),   0, 0)
        self._x_frac_lbl = _big();   g.addWidget(self._x_frac_lbl,  0, 1)
        self._x_v_lbl    = _small(); g.addWidget(self._x_v_lbl,     0, 2)

        g.addWidget(QLabel("Y (quadrature):"), 1, 0)
        self._y_frac_lbl = _big();   g.addWidget(self._y_frac_lbl,  1, 1)
        self._y_v_lbl    = _small(); g.addWidget(self._y_v_lbl,     1, 2)

        g.addWidget(QLabel("R (magnitude):"),  2, 0)
        self._r_frac_lbl = _big();   g.addWidget(self._r_frac_lbl,  2, 1)
        self._r_v_lbl    = _small(); g.addWidget(self._r_v_lbl,     2, 2)

        g.addWidget(QLabel("theta (phase):"),  3, 0)
        self._theta_lbl  = _big();   g.addWidget(self._theta_lbl,   3, 1)
        g.addWidget(QLabel("deg"),             3, 2)

        g.addWidget(QLabel("Frequency:"),      4, 0)
        self._freq_lbl   = _big();   g.addWidget(self._freq_lbl,    4, 1)
        g.addWidget(QLabel("Hz"),              4, 2)

        g.addWidget(QLabel("Sensitivity:"),    5, 0)
        self._sens_lbl   = QLabel("-"); g.addWidget(self._sens_lbl, 5, 1)

        return g

    def _init_plot(self, layout):
        try:
            import pyqtgraph as pg
            pw = pg.PlotWidget(title="X output - live")
            pw.setLabel("left", "X (fraction FS)")
            pw.setLabel("bottom", "Sample #")
            pw.showGrid(x=True, y=True, alpha=0.3)
            pw.setMinimumHeight(180)
            self._plot_line = pw.plot([], [], pen=pg.mkPen("b", width=1.5))
            self._plot_widget = pw
            layout.addWidget(pw, stretch=1)
        except ImportError:
            self._plot_widget = None
            self._plot_line = None
            note = QLabel("Install pyqtgraph for live plot:  pip install pyqtgraph")
            note.setAlignment(Qt.AlignCenter)
            note.setStyleSheet("color: gray;")
            layout.addWidget(note)

    def _on_auto_toggle(self, checked: bool) -> None:
        if checked:
            self._start_polling()
        else:
            self._stop_polling()

    def _on_interval_changed(self, ms: int) -> None:
        if self._poll_thread is not None:
            self._poll_thread.set_interval(ms)

    def _start_polling(self) -> None:
        if self._ctrl is None or self._poll_thread is not None:
            return
        self._poll_thread = _PollThread(self._ctrl, self._interval_spin.value())
        self._poll_thread.snapshot_ready.connect(self._on_snapshot)
        self._poll_thread.error.connect(self._on_poll_error)
        self._poll_thread.start()

    def _stop_polling(self) -> None:
        if self._poll_thread is not None:
            self._poll_thread.stop()
            # Worst case: 11 serial queries x 2 s timeout each = 22 s
            self._poll_thread.wait(25000)
            self._poll_thread = None

    def _read_once(self) -> None:
        if not self._ctrl:
            return
        try:
            self._on_snapshot(self._ctrl.snapshot())
        except Exception as exc:
            self._overload_lbl.setText(f"Error: {exc}")
            self._overload_lbl.setStyleSheet(
                "padding: 4px 12px; border-radius: 4px;"
                "background: #F44336; color: white; font-weight: bold;"
            )

    def _on_snapshot(self, snap: dict) -> None:
        self._x_frac_lbl.setText(f"{snap['x']:+.4f} FS")
        self._y_frac_lbl.setText(f"{snap['y']:+.4f} FS")
        self._r_frac_lbl.setText(f"{snap['r']:.4f} FS")
        self._x_v_lbl.setText(f"{snap['x_v']:+.6f} V")
        self._y_v_lbl.setText(f"{snap['y_v']:+.6f} V")
        self._r_v_lbl.setText(f"{snap['r_v']:.6f} V")
        self._theta_lbl.setText(f"{snap['theta']:+.2f}")
        self._freq_lbl.setText(f"{snap['frequency']:.4f}")
        self._sens_lbl.setText(snap["sensitivity"])

        overloaded   = snap["overloaded"]
        unlocked     = snap["unlocked"]
        no_reference = snap.get("no_reference", False)

        self._overload_lbl.setText("Overload: YES" if overloaded else "Overload: OK")
        self._overload_lbl.setStyleSheet(
            f"padding: 4px 12px; border-radius: 4px; font-weight: bold; color: white;"
            f"background: {'#F44336' if overloaded else '#4CAF50'};"
        )
        self._unlock_lbl.setText(
            "Reference: UNLOCKED" if unlocked else "Reference: Locked"
        )
        self._unlock_lbl.setStyleSheet(
            f"padding: 4px 12px; border-radius: 4px; font-weight: bold; color: white;"
            f"background: {'#FF9800' if unlocked else '#4CAF50'};"
        )
        self._noref_lbl.setText("No Ref: YES" if no_reference else "No Ref: OK")
        self._noref_lbl.setStyleSheet(
            f"padding: 4px 12px; border-radius: 4px; font-weight: bold; color: white;"
            f"background: {'#9C27B0' if no_reference else '#4CAF50'};"
        )

        if self._plot_line is not None:
            self._history_x.append(snap["x"])
            if len(self._history_x) > 500:
                self._history_x = self._history_x[-500:]
            self._plot_line.setData(
                list(range(len(self._history_x))), self._history_x
            )

    def _on_poll_error(self, msg: str) -> None:
        self._overload_lbl.setText(f"Poll error: {msg}")
        self._overload_lbl.setStyleSheet(
            "padding: 4px 12px; border-radius: 4px;"
            "background: #F44336; color: white; font-weight: bold;"
        )
        # Let _stop_polling (triggered by setChecked) do the thread cleanup.
        # Do NOT set _poll_thread = None here; that drops the reference before
        # wait() is called and leaves a dangling thread.
        self._auto_cb.setChecked(False)

    def stop(self) -> None:
        self._stop_polling()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class SR530Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SR530 Lock-In Amplifier Controller")
        self.resize(760, 760)
        self._build_ui()

    def _build_ui(self):
        self._tabs = QTabWidget()

        self._conn_tab     = ConnectionTab()
        self._params_tab   = ParametersTab()
        self._advanced_tab = AdvancedTab()
        self._monitor_tab  = MonitorTab()

        self._conn_tab.connected.connect(self._on_connected)
        self._conn_tab.disconnected.connect(self._on_disconnected)

        self._tabs.addTab(self._conn_tab,     "Connection")   # 0
        self._tabs.addTab(self._params_tab,   "Parameters")   # 1
        self._tabs.addTab(self._advanced_tab, "Advanced")     # 2
        self._tabs.addTab(self._monitor_tab,  "Monitor")      # 3

        self._tabs.setTabEnabled(1, False)
        self._tabs.setTabEnabled(2, False)
        self._tabs.setTabEnabled(3, False)

        self.setCentralWidget(self._tabs)
        self.statusBar().showMessage("Not connected")

    def _on_connected(self, ctrl: SR530Controller) -> None:
        self._params_tab.set_controller(ctrl)
        self._advanced_tab.set_controller(ctrl)
        self._monitor_tab.set_controller(ctrl)
        self._tabs.setTabEnabled(1, True)
        self._tabs.setTabEnabled(2, True)
        self._tabs.setTabEnabled(3, True)
        self.statusBar().showMessage(f"Connected - {ctrl.port}")

    def _on_disconnected(self) -> None:
        self._monitor_tab.stop()
        self._params_tab.set_controller(None)
        self._advanced_tab.set_controller(None)
        self._monitor_tab.set_controller(None)
        self._tabs.setTabEnabled(1, False)
        self._tabs.setTabEnabled(2, False)
        self._tabs.setTabEnabled(3, False)
        self.statusBar().showMessage("Disconnected")

    def closeEvent(self, event):
        self._monitor_tab.stop()
        ctrl = self._conn_tab.controller
        if ctrl:
            try:
                ctrl.disconnect()
            except Exception:
                pass
        event.accept()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "yale.usphere.sr530"
        )
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = SR530Window()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
