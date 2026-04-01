"""
charge_gui.py

Main GUI for usphere charge control.
Run with:
    python charge_gui.py

Dependencies:
    pip install PyQt5 pyvisa
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

from PyQt5.QtCore import QObject, QThread, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont, QIcon
from PyQt5.QtWidgets import (
    QApplication,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import wg_flashlamp
import wg_filament
import wg_drive
from wg_flashlamp import FlashLampController
from wg_filament import FilamentController
from wg_drive import DriveController
from charge_analysis import AnalysisTab
from charge_control import ChargeController
from charge_gui_tabs import WaveformGenTab, ControlTab, CalibrationTab, ExperimentTab
from photon_order_experiment import PhotonOrderExperiment

# Rolling session log — sits alongside this script
LOG_FILE = Path(__file__).parent / "charge_session_log.jsonl"

# Modules in display order
_WG_MODULES = [wg_flashlamp, wg_filament, wg_drive]
_WG_CONTROLLERS = {
    wg_flashlamp.MODULE_NAME: FlashLampController,
    wg_filament.MODULE_NAME:  FilamentController,
    wg_drive.MODULE_NAME:     DriveController,
}


# ---------------------------------------------------------------------------
# Session log helpers
# ---------------------------------------------------------------------------

def _load_last_configs() -> dict:
    """Return {MODULE_NAME: config_dict} from the most recent log entry."""
    if not LOG_FILE.exists():
        return {}
    try:
        lines = [l for l in LOG_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
        if not lines:
            return {}
        return json.loads(lines[-1]).get("configs", {})
    except Exception:
        return {}


def _append_log(configs: dict) -> None:
    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "configs": configs,
    }
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Worker thread for blocking hardware operations
# ---------------------------------------------------------------------------

class _Worker(QThread):
    done = pyqtSignal(bool, str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self):
        try:
            result = self._fn(*self._args, **self._kwargs)
            # fn may return (bool, str) or just bool
            if isinstance(result, tuple):
                self.done.emit(*result)
            else:
                self.done.emit(bool(result), "")
        except Exception as e:
            self.done.emit(False, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Per-module connection panel
# ---------------------------------------------------------------------------

class WGConnectionPanel(QGroupBox):
    """
    One collapsible group box per WG module.
    Auto-builds form fields from MODULE.CONFIG_FIELDS.
    Exposes the live controller instance via .controller (None when disconnected).
    """

    def __init__(self, module, controller_class, saved_config: dict, parent=None):
        super().__init__(module.DEVICE_NAME, parent)
        self._module = module
        self._controller_class = controller_class
        self._controller = None
        self._workers: list[_Worker] = []  # keep refs so threads aren't GC'd

        self._fields: dict[str, QLineEdit] = {}
        self._status_lbl: QLabel | None = None
        self._connect_btn: QPushButton | None = None
        self._disconnect_btn: QPushButton | None = None
        self._enable_btn: QPushButton | None = None
        self._disable_btn: QPushButton | None = None

        self._build_ui()
        self._restore_config(saved_config)
        self._update_button_states()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        g = QGridLayout(self)
        g.setColumnStretch(1, 1)
        row = 0

        # --- Config fields from MODULE.CONFIG_FIELDS ---
        for field_def in self._module.CONFIG_FIELDS:
            key     = field_def["key"]
            label   = field_def["label"]
            default = str(field_def.get("default", ""))
            g.addWidget(QLabel(f"{label}:"), row, 0)
            edit = QLineEdit(default)
            self._fields[key] = edit
            g.addWidget(edit, row, 1, 1, 3)
            row += 1

        # --- Connect / Disconnect row ---
        conn_row = QHBoxLayout()
        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setMinimumWidth(90)
        self._connect_btn.clicked.connect(self._on_connect)
        conn_row.addWidget(self._connect_btn)

        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setMinimumWidth(90)
        self._disconnect_btn.clicked.connect(self._on_disconnect)
        conn_row.addWidget(self._disconnect_btn)

        test_btn = QPushButton("Test")
        test_btn.setMinimumWidth(60)
        test_btn.clicked.connect(self._on_test)
        conn_row.addWidget(test_btn)

        conn_row.addStretch()
        g.addLayout(conn_row, row, 0, 1, 4)
        row += 1

        # --- Status label ---
        self._status_lbl = QLabel("—")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet("color: gray;")
        g.addWidget(self._status_lbl, row, 0, 1, 4)
        row += 1

        # --- Enable / Disable row (for manual testing) ---
        act_row = QHBoxLayout()
        act_row.addWidget(QLabel("Output:"))

        self._enable_btn = QPushButton("Enable")
        self._enable_btn.setMinimumWidth(70)
        self._enable_btn.clicked.connect(self._on_enable)
        act_row.addWidget(self._enable_btn)

        self._disable_btn = QPushButton("Disable")
        self._disable_btn.setMinimumWidth(70)
        self._disable_btn.clicked.connect(self._on_disable)
        act_row.addWidget(self._disable_btn)

        act_row.addStretch()
        g.addLayout(act_row, row, 0, 1, 4)

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _restore_config(self, saved: dict):
        for key, edit in self._fields.items():
            if key in saved:
                edit.setText(str(saved[key]))

    def get_config(self) -> dict:
        return {key: edit.text() for key, edit in self._fields.items()}

    # ------------------------------------------------------------------
    # Button state
    # ------------------------------------------------------------------

    def _update_button_states(self):
        connected = self._controller is not None and self._controller.is_connected
        self._connect_btn.setEnabled(not connected)
        self._disconnect_btn.setEnabled(connected)
        self._enable_btn.setEnabled(connected)
        self._disable_btn.setEnabled(connected)

    def _set_status(self, ok: bool | None, msg: str):
        self._status_lbl.setText(msg)
        if ok is True:
            self._status_lbl.setStyleSheet("color: green;")
        elif ok is False:
            self._status_lbl.setStyleSheet("color: red;")
        else:
            self._status_lbl.setStyleSheet("color: gray;")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _run_worker(self, fn, *args, on_done=None, **kwargs):
        worker = _Worker(fn, *args, **kwargs)
        if on_done:
            worker.done.connect(on_done)
        worker.done.connect(lambda _ok, _msg, w=worker: self._workers.remove(w)
                            if w in self._workers else None)
        worker.finished.connect(worker.deleteLater)
        self._workers.append(worker)
        worker.start()

    def _on_test(self):
        self._set_status(None, "Testing…")
        config = self.get_config()
        self._run_worker(
            self._module.test, config,
            on_done=lambda ok, msg: self._set_status(ok, msg),
        )

    def _on_connect(self):
        self._set_status(None, "Connecting…")
        config = self.get_config()
        self._controller = self._controller_class(config)

        def _do_connect():
            ok = self._controller.connect()
            return ok, (self._controller.is_connected
                        and f"Connected — {self._module.DEVICE_NAME}"
                        or "connect() returned False")

        def _after_connect(ok, msg):
            if not ok:
                self._controller = None
            self._set_status(ok, msg)
            self._update_button_states()

        self._run_worker(_do_connect, on_done=_after_connect)

    def _on_disconnect(self):
        if self._controller:
            try:
                self._controller.disconnect()
            except Exception as e:
                self._set_status(False, f"Disconnect error: {e}")
            else:
                self._set_status(None, "Disconnected")
            finally:
                self._controller = None
                self._update_button_states()

    def _on_enable(self):
        if not self._controller:
            return
        config = self.get_config()
        self._controller.configure(config)

        def _do_enable():
            ok = self._controller.enable()
            return ok, ("Output enabled" if ok else "enable() returned False")

        self._run_worker(_do_enable,
                         on_done=lambda ok, msg: self._set_status(ok, msg))

    def _on_disable(self):
        if not self._controller:
            return

        def _do_disable():
            ok = self._controller.disable()
            return ok, ("Output disabled" if ok else "disable() returned False")

        self._run_worker(_do_disable,
                         on_done=lambda ok, msg: self._set_status(ok, msg))

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    @property
    def controller(self):
        """Live controller instance, or None if not connected."""
        return self._controller


# ---------------------------------------------------------------------------
# Connections tab
# ---------------------------------------------------------------------------

class ConnectionsTab(QWidget):
    """
    One WGConnectionPanel per WG module, in a scrollable column.
    Also exposes a status log and a "Save config" button.
    """

    def __init__(self, saved_configs: dict, parent=None):
        super().__init__(parent)
        self._panels: dict[str, WGConnectionPanel] = {}
        self._build_ui(saved_configs)

    def _build_ui(self, saved_configs: dict):
        outer = QVBoxLayout(self)
        outer.setSpacing(8)
        outer.setContentsMargins(6, 6, 6, 6)

        # Scrollable area for the panels
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        inner = QWidget()
        vbox = QVBoxLayout(inner)
        vbox.setSpacing(10)
        vbox.setContentsMargins(4, 4, 4, 4)

        for mod in _WG_MODULES:
            ctrl_cls = _WG_CONTROLLERS[mod.MODULE_NAME]
            saved = saved_configs.get(mod.MODULE_NAME, {})
            panel = WGConnectionPanel(mod, ctrl_cls, saved)
            self._panels[mod.MODULE_NAME] = panel
            vbox.addWidget(panel)

        vbox.addStretch()
        scroll.setWidget(inner)
        outer.addWidget(scroll, stretch=1)

        # Save config button
        save_row = QHBoxLayout()
        save_btn = QPushButton("Save current config")
        save_btn.setMaximumWidth(160)
        save_btn.clicked.connect(self._on_save)
        save_row.addStretch()
        save_row.addWidget(save_btn)
        outer.addLayout(save_row)

    def _on_save(self):
        _append_log(self.get_all_configs())

    def get_all_configs(self) -> dict:
        return {name: panel.get_config() for name, panel in self._panels.items()}

    def get_controller(self, module_name: str):
        """Return the live controller for a module, or None."""
        panel = self._panels.get(module_name)
        return panel.controller if panel else None


# ---------------------------------------------------------------------------
# Placeholder tab
# ---------------------------------------------------------------------------

class _PlaceholderTab(QWidget):
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        lbl = QLabel(text)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet("color: gray; font-size: 13px;")
        QVBoxLayout(self).addWidget(lbl)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("usphere Charge Control")
        self.resize(800, 680)

        saved = _load_last_configs()
        self._connections_tab = ConnectionsTab(saved)
        self._analysis_tab = AnalysisTab()
        if "Analysis" in saved:
            self._analysis_tab.restore_config(saved["Analysis"])

        # Charge controller (GUI-independent logic)
        self._charge_ctrl = ChargeController()

        # Control tab
        self._control_tab = ControlTab(self._charge_ctrl)
        if "Control" in saved:
            self._control_tab.restore_config(saved["Control"])

        # Calibration tab
        self._calibration_tab = CalibrationTab()
        if "Calibration" in saved:
            self._calibration_tab.restore_config(saved["Calibration"])

        # Experiment tab
        self._experiment_tab = ExperimentTab()
        if "Experiment" in saved:
            self._experiment_tab.restore_config(saved["Experiment"])
        # Create the experiment engine
        self._photon_exp = PhotonOrderExperiment()
        self._experiment_tab.set_experiment(self._photon_exp)

        # Waveform generator tabs — get controllers from Connections tab
        self._flashlamp_tab = WaveformGenTab(
            "Flash Lamp",
            lambda: self._connections_tab.get_controller("FlashLamp"),
            channels=2,
        )
        self._filament_tab = WaveformGenTab(
            "Filament",
            lambda: self._connections_tab.get_controller("Filament"),
            channels=1,
        )
        self._drive_tab = WaveformGenTab(
            "Drive",
            lambda: self._connections_tab.get_controller("Drive"),
            channels=1,
        )

        # Wire analysis → control loop
        self._analysis_tab.charge_updated.connect(
            self._charge_ctrl.on_charge_update
        )
        # Wire analysis → experiment engine
        self._analysis_tab.charge_updated.connect(
            self._photon_exp.on_charge_update
        )

        # Wire actuators into controller when they become available
        self._actuator_timer = QTimer(self)
        self._actuator_timer.timeout.connect(self._sync_actuators)
        self._actuator_timer.start(2000)  # check every 2 s

        tabs = QTabWidget()
        tabs.addTab(self._connections_tab, "Connections")
        tabs.addTab(self._flashlamp_tab,   "Flash Lamp")
        tabs.addTab(self._filament_tab,    "Filament")
        tabs.addTab(self._drive_tab,       "Drive")
        tabs.addTab(self._analysis_tab,    "Analysis")
        tabs.addTab(self._control_tab,     "Control")
        tabs.addTab(self._calibration_tab, "Calibration")
        tabs.addTab(self._experiment_tab,  "Experiment")

        self.setCentralWidget(tabs)

    def _sync_actuators(self):
        """Keep the controller's actuator references in sync with connections."""
        fl = self._connections_tab.get_controller("FlashLamp")
        fi = self._connections_tab.get_controller("Filament")
        self._charge_ctrl.set_actuators(flashlamp=fl, filament=fi)
        self._photon_exp.set_actuators(flashlamp=fl, filament=fi)

    def closeEvent(self, event):
        # Stop analysis if running
        if self._analysis_tab.source is not None and self._analysis_tab.source.is_running:
            self._analysis_tab._on_stop()
        # Stop control loop if running
        if self._charge_ctrl.is_running:
            self._charge_ctrl.stop()
        # Stop experiment if running
        if self._photon_exp.is_running:
            self._photon_exp.abort()
        # Persist config on exit
        configs = self._connections_tab.get_all_configs()
        configs["Analysis"] = self._analysis_tab.get_config()
        configs["Control"] = self._control_tab.get_config()
        configs["Calibration"] = self._calibration_tab.get_config()
        configs["Experiment"] = self._experiment_tab.get_config()
        _append_log(configs)
        event.accept()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "yale.usphere.charge"
        )
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
