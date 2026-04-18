"""
sr530_gui.py

Standalone PyQt5 GUI for the Stanford Research Systems SR530 Lock-In Amplifier.

Run with:
    python sr530_gui.py

Requires:
    pip install PyQt5 pyserial
    pip install pyqtgraph   # optional — enables live X-output plot in Monitor tab
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
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from sr530_controller import (
    SR530Controller,
    SENSITIVITY_TABLE,
    TIME_CONSTANT_TABLE,
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

    # -- UI --

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(10)
        outer.setContentsMargins(10, 10, 10, 10)

        # Serial settings
        grp = QGroupBox("Serial settings")
        form = QFormLayout(grp)

        self._port_edit = QLineEdit("COM5")
        form.addRow("Port:", self._port_edit)

        self._baud_edit = QLineEdit("9600")
        form.addRow("Baud rate:", self._baud_edit)

        self._timeout_spin = QDoubleSpinBox()
        self._timeout_spin.setRange(0.5, 30.0)
        self._timeout_spin.setValue(2.0)
        self._timeout_spin.setSuffix(" s")
        self._timeout_spin.setMaximumWidth(110)
        form.addRow("Timeout:", self._timeout_spin)

        outer.addWidget(grp)

        # Buttons
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

        # Log
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

    # -- Slots --

    def _on_connect(self) -> None:
        port = self._port_edit.text().strip()
        try:
            baud = int(self._baud_edit.text())
        except ValueError:
            baud = 9600
        timeout = self._timeout_spin.value()

        self._status_lbl.setText("Connecting…")
        self._status_lbl.setStyleSheet("color: #FF9800; font-size: 13px;")
        QApplication.processEvents()

        try:
            ctrl = SR530Controller(port, baudrate=baud, timeout=timeout)
            if not ctrl.connect():
                raise RuntimeError("connect() returned False")
            self._ctrl = ctrl
            self._log_msg(f"Connected to {port} at {baud} baud", "green")
            self._status_lbl.setText(f"Connected — {port}")
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
    """Set and read all SR530 control parameters."""

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

    # -- UI --

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
        self._phase_spin.setSuffix(" °")
        self._phase_spin.setMaximumWidth(130)
        self._phase_spin.setEnabled(False)
        ref_form.addRow("Phase:", self._phase_spin)
        self._controls.append(self._phase_spin)

        outer.addWidget(ref_grp)

        # Amplifier
        amp_grp = QGroupBox("Amplifier")
        amp_form = QFormLayout(amp_grp)

        self._sens_combo = QComboBox()
        for idx in sorted(SENSITIVITY_TABLE):
            self._sens_combo.addItem(f"{SENSITIVITY_TABLE[idx]}", idx)
        self._sens_combo.setMaximumWidth(160)
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
        for idx in sorted(TIME_CONSTANT_TABLE):
            self._pre_tc_combo.addItem(f"{TIME_CONSTANT_TABLE[idx]}", idx)
        self._pre_tc_combo.setMaximumWidth(160)
        self._pre_tc_combo.setEnabled(False)
        filt_form.addRow("Pre time constant:", self._pre_tc_combo)
        self._controls.append(self._pre_tc_combo)

        self._post_tc_combo = QComboBox()
        for idx in sorted(TIME_CONSTANT_TABLE):
            self._post_tc_combo.addItem(f"{TIME_CONSTANT_TABLE[idx]}", idx)
        self._post_tc_combo.setMaximumWidth(160)
        self._post_tc_combo.setEnabled(False)
        filt_form.addRow("Post time constant:", self._post_tc_combo)
        self._controls.append(self._post_tc_combo)

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

        self._status_lbl = QLabel("—")
        self._status_lbl.setStyleSheet("color: gray;")
        outer.addWidget(self._status_lbl)

        outer.addStretch()

    # -- Helpers --

    def _set_combo_by_data(self, combo: QComboBox, value: int) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.setCurrentIndex(i)
                return

    # -- Slots --

    def _apply_all(self) -> None:
        if not self._ctrl:
            return
        errors = []
        try:
            self._ctrl.set_phase(self._phase_spin.value())
        except Exception as e:
            errors.append(f"phase: {e}")
        try:
            self._ctrl.set_sensitivity(self._sens_combo.currentData())
        except Exception as e:
            errors.append(f"sensitivity: {e}")
        try:
            self._ctrl.set_dynamic_reserve(self._reserve_combo.currentIndex())
        except Exception as e:
            errors.append(f"reserve: {e}")
        try:
            self._ctrl.set_pre_time_constant(self._pre_tc_combo.currentData())
        except Exception as e:
            errors.append(f"pre TC: {e}")
        try:
            self._ctrl.set_post_time_constant(self._post_tc_combo.currentData())
        except Exception as e:
            errors.append(f"post TC: {e}")

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
            phase    = self._ctrl.get_phase()
            sens     = self._ctrl.get_sensitivity()
            reserve  = self._ctrl.get_dynamic_reserve()
            pre_tc   = self._ctrl.get_pre_time_constant()
            post_tc  = self._ctrl.get_post_time_constant()

            self._phase_spin.setValue(phase)
            self._set_combo_by_data(self._sens_combo, sens)
            self._reserve_combo.setCurrentIndex(reserve)
            self._set_combo_by_data(self._pre_tc_combo, pre_tc)
            self._set_combo_by_data(self._post_tc_combo, post_tc)

            self._status_lbl.setText(f"Read at {time.strftime('%H:%M:%S')}")
            self._status_lbl.setStyleSheet("color: green;")
        except Exception as exc:
            self._status_lbl.setText(f"Read error: {exc}")
            self._status_lbl.setStyleSheet("color: red;")


# ---------------------------------------------------------------------------
# Monitor tab
# ---------------------------------------------------------------------------

class MonitorTab(QWidget):
    """Live readout of X/Y/R/θ with optional auto-refresh and live plot."""

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

    # -- UI --

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(10)
        outer.setContentsMargins(10, 10, 10, 10)

        # Auto-refresh controls
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

        # Outputs group
        out_grp = QGroupBox("Outputs")
        g_out = self._build_outputs_grid()
        out_grp.setLayout(g_out)
        outer.addWidget(out_grp)

        # Status indicators
        stat_grp = QGroupBox("Status")
        sh = QHBoxLayout(stat_grp)

        self._overload_lbl = QLabel("Overload: —")
        self._overload_lbl.setAlignment(Qt.AlignCenter)
        self._overload_lbl.setMinimumWidth(120)
        self._overload_lbl.setStyleSheet(
            "padding: 4px 12px; border-radius: 4px;"
            "background: #BDBDBD; color: white; font-weight: bold;"
        )
        sh.addWidget(self._overload_lbl)

        self._unlock_lbl = QLabel("Reference: —")
        self._unlock_lbl.setAlignment(Qt.AlignCenter)
        self._unlock_lbl.setMinimumWidth(160)
        self._unlock_lbl.setStyleSheet(
            "padding: 4px 12px; border-radius: 4px;"
            "background: #BDBDBD; color: white; font-weight: bold;"
        )
        sh.addWidget(self._unlock_lbl)

        sh.addStretch()
        outer.addWidget(stat_grp)

        # Optional live plot
        self._init_plot(outer)

    def _build_outputs_grid(self):
        from PyQt5.QtWidgets import QGridLayout

        g = QGridLayout()
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)

        def _big():
            l = QLabel("—")
            l.setStyleSheet("font-size: 18px; font-weight: bold;")
            l.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            return l

        def _small():
            l = QLabel("—")
            l.setStyleSheet("color: #555;")
            return l

        g.addWidget(QLabel("X (in-phase):"),  0, 0)
        self._x_frac_lbl  = _big();   g.addWidget(self._x_frac_lbl,  0, 1)
        self._x_v_lbl     = _small(); g.addWidget(self._x_v_lbl,     0, 2)

        g.addWidget(QLabel("Y (quadrature):"), 1, 0)
        self._y_frac_lbl  = _big();   g.addWidget(self._y_frac_lbl,  1, 1)
        self._y_v_lbl     = _small(); g.addWidget(self._y_v_lbl,     1, 2)

        g.addWidget(QLabel("R (magnitude):"), 2, 0)
        self._r_frac_lbl  = _big();   g.addWidget(self._r_frac_lbl,  2, 1)
        self._r_v_lbl     = _small(); g.addWidget(self._r_v_lbl,     2, 2)

        g.addWidget(QLabel("θ (phase):"),     3, 0)
        self._theta_lbl   = _big();   g.addWidget(self._theta_lbl,   3, 1)
        g.addWidget(QLabel("°"),              3, 2)

        g.addWidget(QLabel("Frequency:"),     4, 0)
        self._freq_lbl    = _big();   g.addWidget(self._freq_lbl,    4, 1)
        g.addWidget(QLabel("Hz"),             4, 2)

        g.addWidget(QLabel("Sensitivity:"),   5, 0)
        self._sens_lbl    = QLabel("—"); g.addWidget(self._sens_lbl, 5, 1)

        return g

    def _init_plot(self, layout):
        try:
            import pyqtgraph as pg
            pw = pg.PlotWidget(title="X output — live")
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

    # -- Polling --

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
            self._poll_thread.wait(3000)
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

    # -- Snapshot handler --

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

        overloaded = snap["overloaded"]
        unlocked   = snap["unlocked"]

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
        self._poll_thread = None
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
        self.resize(660, 640)
        self._build_ui()

    def _build_ui(self):
        self._tabs = QTabWidget()

        self._conn_tab    = ConnectionTab()
        self._params_tab  = ParametersTab()
        self._monitor_tab = MonitorTab()

        self._conn_tab.connected.connect(self._on_connected)
        self._conn_tab.disconnected.connect(self._on_disconnected)

        self._tabs.addTab(self._conn_tab,    "Connection")
        self._tabs.addTab(self._params_tab,  "Parameters")
        self._tabs.addTab(self._monitor_tab, "Monitor")

        self._tabs.setTabEnabled(1, False)
        self._tabs.setTabEnabled(2, False)

        self.setCentralWidget(self._tabs)
        self.statusBar().showMessage("Not connected")

    def _on_connected(self, ctrl: SR530Controller) -> None:
        self._params_tab.set_controller(ctrl)
        self._monitor_tab.set_controller(ctrl)
        self._tabs.setTabEnabled(1, True)
        self._tabs.setTabEnabled(2, True)
        self.statusBar().showMessage(f"Connected — {ctrl._port}")

    def _on_disconnected(self) -> None:
        self._monitor_tab.stop()
        self._params_tab.set_controller(None)
        self._monitor_tab.set_controller(None)
        self._tabs.setTabEnabled(1, False)
        self._tabs.setTabEnabled(2, False)
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
