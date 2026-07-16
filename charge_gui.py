"""
charge_gui.py

Main GUI for usphere charge control.
Run with:
    python charge_gui.py

Dependencies:
    pip install PyQt5 pyvisa pyserial
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

from PyQt5.QtCore import QThread, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from charge_analysis import AnalysisTab
from charge_control import ChargeController
from charge_gui_tabs import ControlTab, CalibrationTab, ExperimentTab
from photon_order_experiment import PhotonOrderExperiment
from charge_sequence import ChargeSequencer
from sequencer_tab import SequencerTab
from wg_control_tab import (
    WaveformControlTab, DriveSetbackAdapter, FilamentAdapter,
    ChargeSequencerActuators,
)

# Rolling session log — sits alongside this script
LOG_FILE = Path(__file__).parent / "charge_session_log.jsonl"

# AFG driver path
_AFG_PATH = Path(__file__).parent / "resources" / "GWINSTEKAFG2225_controller"
if str(_AFG_PATH) not in sys.path:
    sys.path.insert(0, str(_AFG_PATH))

try:
    from afg2225_controller import AFG2225Controller
    _AFG_AVAILABLE = True
except ImportError:
    _AFG_AVAILABLE = False

# NGE100 DC power supply (flash-lamp control + filament power lines)
from nge_supply import NGESupplyController, NGE_AVAILABLE as _NGE_AVAILABLE

# SR530 submodule path
_SR530_PATH = Path(__file__).parent / "resources" / "SR530_controller"
if _SR530_PATH.exists() and str(_SR530_PATH) not in sys.path:
    sys.path.insert(0, str(_SR530_PATH))

try:
    from sr530_gui import (
        ConnectionTab  as SR530ConnectionTab,
        ParametersTab  as SR530ParametersTab,
        MonitorTab     as SR530MonitorTab,
    )
    _SR530_GUI_AVAILABLE = True
except ImportError:
    _SR530_GUI_AVAILABLE = False


# ---------------------------------------------------------------------------
# Session log helpers
# ---------------------------------------------------------------------------

def _load_last_configs() -> dict:
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
            if isinstance(result, tuple):
                self.done.emit(*result)
            else:
                self.done.emit(bool(result), "")
        except Exception as e:
            self.done.emit(False, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# SR530 embedded tab
# ---------------------------------------------------------------------------

class SR530Tab(QWidget):
    """
    Embeds the SR530 GUI (Connection / Parameters / Monitor sub-tabs)
    as a single top-level tab in the main charge window.

    Falls back to a plain "not available" message if the SR530 submodule is
    not installed.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        if not _SR530_GUI_AVAILABLE:
            lbl = QLabel(
                "SR530 submodule not found.\n"
                "Run:  git submodule update --init  "
                "and ensure resources/SR530_controller is present."
            )
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color: gray; font-size: 13px;")
            outer.addWidget(lbl)
            self._tabs = None
            return

        self._tabs = QTabWidget()

        self._conn_tab     = SR530ConnectionTab()
        self._params_tab   = SR530ParametersTab()
        self._monitor_tab  = SR530MonitorTab()

        self._conn_tab.connected.connect(self._on_connected)
        self._conn_tab.disconnected.connect(self._on_disconnected)

        self._tabs.addTab(self._conn_tab,     "Connection")
        self._tabs.addTab(self._params_tab,   "Parameters")
        self._tabs.addTab(self._monitor_tab,  "Monitor")

        self._tabs.setTabEnabled(1, False)
        self._tabs.setTabEnabled(2, False)

        outer.addWidget(self._tabs)

    def _on_connected(self, ctrl) -> None:
        self._params_tab.set_controller(ctrl)
        self._monitor_tab.set_controller(ctrl)
        self._tabs.setTabEnabled(1, True)
        self._tabs.setTabEnabled(2, True)

    def _on_disconnected(self) -> None:
        self._monitor_tab.stop()
        self._params_tab.set_controller(None)
        self._monitor_tab.set_controller(None)
        self._tabs.setTabEnabled(1, False)
        self._tabs.setTabEnabled(2, False)

    def stop(self) -> None:
        if self._tabs and self._monitor_tab:
            self._monitor_tab.stop()


# ---------------------------------------------------------------------------
# Per-WG connection panel (COM port only)
# ---------------------------------------------------------------------------

class _WGPanel(QGroupBox):
    """
    Connection panel for one waveform generator (WG1 / WG2 / WG3).

    Holds an AFG2225Controller; exposes it via .afg (None when disconnected).
    """

    def __init__(self, title: str, saved_port: str = "", parent=None):
        super().__init__(title, parent)
        self._afg: AFG2225Controller | None = None
        self._workers: list[_Worker] = []
        self._build(saved_port)

    def _build(self, saved_port: str):
        h = QHBoxLayout(self)

        h.addWidget(QLabel("COM port:"))
        self._port_edit = QLineEdit(saved_port or "")
        self._port_edit.setPlaceholderText("e.g. COM5")
        self._port_edit.setMaximumWidth(120)
        h.addWidget(self._port_edit)

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setMinimumWidth(80)
        self._connect_btn.clicked.connect(self._on_connect)
        h.addWidget(self._connect_btn)

        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setMinimumWidth(80)
        self._disconnect_btn.setEnabled(False)
        self._disconnect_btn.clicked.connect(self._on_disconnect)
        h.addWidget(self._disconnect_btn)

        self._test_btn = QPushButton("Test")
        self._test_btn.setMinimumWidth(50)
        self._test_btn.setEnabled(False)
        self._test_btn.clicked.connect(self._on_test)
        h.addWidget(self._test_btn)

        self._status = QLabel("—")
        self._status.setStyleSheet("color: gray;")
        self._status.setMinimumWidth(220)
        h.addWidget(self._status)

        h.addStretch()

    def _run_worker(self, fn, *args, on_done=None):
        w = _Worker(fn, *args)
        if on_done:
            w.done.connect(on_done)
        w.done.connect(lambda _ok, _msg, ww=w: self._workers.remove(ww)
                       if ww in self._workers else None)
        w.finished.connect(w.deleteLater)
        self._workers.append(w)
        w.start()

    def _set_status(self, ok: bool | None, msg: str):
        self._status.setText(msg)
        if ok is True:
            self._status.setStyleSheet("color: green;")
        elif ok is False:
            self._status.setStyleSheet("color: red;")
        else:
            self._status.setStyleSheet("color: gray;")

    def _on_connect(self):
        if not _AFG_AVAILABLE:
            self._set_status(False, "AFG driver not found")
            return
        port = self._port_edit.text().strip()
        if not port:
            self._set_status(False, "Enter a COM port")
            return
        self._set_status(None, "Connecting…")
        self._afg = AFG2225Controller()

        def _do():
            ok = self._afg.connect(port)
            return ok, f"Connected — {self._afg.idn or 'unknown'}" if ok else "connect() returned False"

        def _after(ok, msg):
            if not ok:
                self._afg = None
            self._set_status(ok, msg)
            self._connect_btn.setEnabled(not ok)
            self._disconnect_btn.setEnabled(ok)
            self._test_btn.setEnabled(ok)

        self._run_worker(_do, on_done=_after)

    def _on_disconnect(self):
        if self._afg:
            try:
                self._afg.disconnect()
            except Exception:
                pass
            self._afg = None
        self._set_status(None, "Disconnected")
        self._connect_btn.setEnabled(True)
        self._disconnect_btn.setEnabled(False)
        self._test_btn.setEnabled(False)

    def _on_test(self):
        if not self._afg or not self._afg.is_connected:
            self._set_status(False, "Not connected")
            return

        def _do():
            idn = self._afg.idn or "unknown"
            return True, f"OK — {idn}"

        self._run_worker(_do, on_done=lambda ok, msg: self._set_status(ok, msg))

    def get_config(self) -> dict:
        return {"com_port": self._port_edit.text().strip()}

    @property
    def afg(self) -> AFG2225Controller | None:
        return self._afg if (self._afg and self._afg.is_connected) else None


# ---------------------------------------------------------------------------
# NGE100 power-supply connection panel
# ---------------------------------------------------------------------------

class _NGEPanel(QGroupBox):
    """
    Connection panel for the R&S NGE100 DC power supply.

    Holds an NGESupplyController; exposes it via .nge (None when disconnected).
    """

    def __init__(self, saved_port: str = "", parent=None):
        super().__init__("Power Supply — R&S NGE100", parent)
        self._psu: NGESupplyController | None = None
        self._workers: list[_Worker] = []
        self._build(saved_port)

    def _build(self, saved_port: str):
        h = QHBoxLayout(self)
        h.addWidget(QLabel("COM port:"))
        # Default to COM3 (current NGE port) — auto-discover is timing-flaky,
        # connect-by-port is reliable.  User-editable; last value is saved.
        self._port_edit = QLineEdit(saved_port or "COM3")
        self._port_edit.setPlaceholderText("e.g. COM3 (blank = auto-discover)")
        self._port_edit.setMaximumWidth(160)
        h.addWidget(self._port_edit)

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setMinimumWidth(80)
        self._connect_btn.clicked.connect(self._on_connect)
        h.addWidget(self._connect_btn)

        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setMinimumWidth(80)
        self._disconnect_btn.setEnabled(False)
        self._disconnect_btn.clicked.connect(self._on_disconnect)
        h.addWidget(self._disconnect_btn)

        self._status = QLabel("—")
        self._status.setStyleSheet("color: gray;")
        self._status.setMinimumWidth(260)
        h.addWidget(self._status)
        h.addStretch()

    def _set_status(self, ok, msg):
        self._status.setText(msg)
        self._status.setStyleSheet(
            "color: green;" if ok is True else
            "color: red;" if ok is False else "color: gray;")

    def _run_worker(self, fn, *args, on_done=None):
        w = _Worker(fn, *args)
        if on_done:
            w.done.connect(on_done)
        w.done.connect(lambda _ok, _msg, ww=w: self._workers.remove(ww)
                       if ww in self._workers else None)
        w.finished.connect(w.deleteLater)
        self._workers.append(w)
        w.start()

    def _on_connect(self):
        if not _NGE_AVAILABLE:
            self._set_status(False, "NGE100 driver not found")
            return
        port = self._port_edit.text().strip()
        self._set_status(None, "Connecting…")
        self._psu = NGESupplyController({"com_port": port})

        def _do():
            ok = self._psu.connect()
            return ok, (f"Connected — {self._psu.idn or 'unknown'}"
                        if ok else "connect() returned False")

        def _after(ok, msg):
            if not ok:
                self._psu = None
            self._set_status(ok, msg)
            self._connect_btn.setEnabled(not ok)
            self._disconnect_btn.setEnabled(ok)

        self._run_worker(_do, on_done=_after)

    def _on_disconnect(self):
        if self._psu:
            try:
                self._psu.disconnect()   # turns all outputs off, releases to local
            except Exception:
                pass
            self._psu = None
        self._set_status(None, "Disconnected")
        self._connect_btn.setEnabled(True)
        self._disconnect_btn.setEnabled(False)

    def get_config(self) -> dict:
        return {"com_port": self._port_edit.text().strip()}

    @property
    def nge(self) -> NGESupplyController | None:
        return self._psu if (self._psu and self._psu.is_connected) else None


# ---------------------------------------------------------------------------
# Connections tab
# ---------------------------------------------------------------------------

class ConnectionsTab(QWidget):
    """
    Three WG connection panels (WG1 / WG2 / WG3) plus the NGE100 power supply,
    and a "Launch Charge Control" button that switches to the
    WaveformControlTab.
    """

    launch_clicked = pyqtSignal()

    def __init__(self, saved_configs: dict, parent=None):
        super().__init__(parent)
        self._panels: dict[str, _WGPanel] = {}
        self._nge_panel: _NGEPanel | None = None
        self._build(saved_configs)

    def _build(self, saved: dict):
        outer = QVBoxLayout(self)
        outer.setSpacing(8)
        outer.setContentsMargins(6, 6, 6, 6)

        for name in ("WG1", "WG2", "WG3"):
            port = saved.get(name, {}).get("com_port", "")
            panel = _WGPanel(name, saved_port=port)
            self._panels[name] = panel
            outer.addWidget(panel)

        nge_port = saved.get("NGE", {}).get("com_port", "")
        self._nge_panel = _NGEPanel(saved_port=nge_port)
        outer.addWidget(self._nge_panel)

        outer.addSpacing(12)

        launch_row = QHBoxLayout()
        launch_btn = QPushButton("Launch Electrode Control ▶")
        launch_btn.setMinimumWidth(200)
        launch_btn.setStyleSheet(
            "font-size: 14px; font-weight: bold;"
            "background-color: #1565C0; color: white;"
            "padding: 8px 16px; border-radius: 4px;"
        )
        launch_btn.clicked.connect(self.launch_clicked)
        launch_row.addStretch()
        launch_row.addWidget(launch_btn)
        launch_row.addStretch()
        outer.addLayout(launch_row)

        save_row = QHBoxLayout()
        save_btn = QPushButton("Save current config")
        save_btn.setMaximumWidth(160)
        save_btn.clicked.connect(self._on_save)
        save_row.addStretch()
        save_row.addWidget(save_btn)
        outer.addLayout(save_row)

        outer.addStretch()

    def _on_save(self):
        _append_log(self.get_all_configs())

    def get_all_configs(self) -> dict:
        cfg = {name: panel.get_config() for name, panel in self._panels.items()}
        if self._nge_panel is not None:
            cfg["NGE"] = self._nge_panel.get_config()
        return cfg

    def get_afg(self, wg_index: int) -> AFG2225Controller | None:
        """Return the live AFG2225Controller for WG{wg_index}, or None."""
        name = f"WG{wg_index}"
        panel = self._panels.get(name)
        return panel.afg if panel else None

    def get_nge(self) -> NGESupplyController | None:
        """Return the live NGESupplyController, or None."""
        return self._nge_panel.nge if self._nge_panel else None


# ---------------------------------------------------------------------------
# Main widget
# ---------------------------------------------------------------------------

class ChargeWidget(QWidget):
    def __init__(self):
        super().__init__()

        saved = _load_last_configs()

        # --- Connections tab ---
        self._connections_tab = ConnectionsTab(saved)

        # --- WaveformControl tab ---
        self._wg_tab = WaveformControlTab(
            lambda wg_n: self._connections_tab.get_afg(wg_n),
            get_nge=self._connections_tab.get_nge,
        )
        if "ElectrodeMap" in saved:
            self._wg_tab.electrode_map.restore_config(saved["ElectrodeMap"])
        if "LockInRef" in saved:
            self._wg_tab.lockin_ref.restore_config(saved["LockInRef"])
        if "NGEMap" in saved:
            self._wg_tab.nge_map.restore_config(saved["NGEMap"])
        if "FlashControl" in saved:
            self._wg_tab.flash_control.restore_config(saved["FlashControl"])
        if "FilamentPower" in saved:
            self._wg_tab.filament_power.restore_config(saved["FilamentPower"])

        # --- Analysis tab ---
        self._analysis_tab = AnalysisTab()
        if "Analysis" in saved:
            self._analysis_tab.restore_config(saved["Analysis"])

        # --- Charge controller ---
        self._charge_ctrl = ChargeController()
        self._control_tab = ControlTab(self._charge_ctrl)
        if "Control" in saved:
            self._control_tab.restore_config(saved["Control"])

        # --- Calibration tab ---
        self._calibration_tab = CalibrationTab()
        if "Calibration" in saved:
            self._calibration_tab.restore_config(saved["Calibration"])

        # --- Experiment tab (Photon Order) ---
        self._experiment_tab = ExperimentTab()
        if "Experiment" in saved:
            self._experiment_tab.restore_config(saved["Experiment"])
        self._photon_exp = PhotonOrderExperiment()
        self._experiment_tab.set_experiment(self._photon_exp)

        # --- Command sequencer (charge/discharge cycles) ---
        self._seq_actuators = ChargeSequencerActuators(self._wg_tab)
        self._sequencer = ChargeSequencer(self._seq_actuators)
        self._sequencer_tab = SequencerTab()
        self._sequencer_tab.set_sequencer(self._sequencer)
        if "Sequencer" in saved:
            self._sequencer_tab.restore_config(saved["Sequencer"])
        # The sequencer's "set electrode" step requests a drive-amplitude change;
        # apply it on the GUI thread (spinbox = source of truth) so the field,
        # reference mirror, and charge normalization all stay consistent.
        self._sequencer.set_electrode_requested.connect(self._on_seq_set_electrode)

        # --- Filament actuator: trigger (WG3-CH2) + NGE power, so the control
        # loop can arm the filament-power DC output for the session.
        self._filament_actuator = FilamentAdapter(
            self._wg_tab.filament, self._wg_tab.filament_power)

        # --- Drive setback: park the drive tone low while the filament is on.
        # Wraps the filament actuator; settings live in the Control tab. The
        # reduced/restored absolute drive amplitude feeds AnalysisTab so the
        # lock-in charge readout stays normalized (charge ∝ X / drive amp).
        # arm()/disarm() pass through to the filament power.
        self._drive_setback = DriveSetbackAdapter(
            actuator=self._filament_actuator,
            get_drive_widget=lambda: getattr(
                self._wg_tab, f"{self._analysis_tab.monitor_axis()}_drive"),
            get_params=self._control_tab.get_setback_params,
            on_drive_amp=self._analysis_tab.set_current_drive_amp,
        )
        # Calibrations record the drive amplitude in use (the effective drive
        # amplitude of the monitored axis).
        self._calibration_tab._drive_amp_provider = (
            self._drive_setback.effective_amplitude)

        # --- Wire analysis → control loop, experiment, and sequencer ---
        self._analysis_tab.charge_updated.connect(self._charge_ctrl.on_charge_update)
        self._analysis_tab.charge_updated.connect(self._photon_exp.on_charge_update)
        self._analysis_tab.charge_updated.connect(self._sequencer.on_charge_update)

        # --- Wire analysis → auto lock-in calibration (and result back) ---
        self._analysis_tab.charge_updated.connect(
            self._calibration_tab.on_charge_update)
        self._calibration_tab.lockin_cal_saved.connect(self._on_lockin_cal_saved)

        # --- Actuator sync (wires WG tab groups to ChargeController) ---
        self._actuator_timer = QTimer(self)
        self._actuator_timer.timeout.connect(self._sync_actuators)
        self._actuator_timer.start(2000)

        # --- SR530 tab ---
        self._sr530_tab = SR530Tab()

        # --- Build tab widget ---
        self._tabs = QTabWidget()
        self._tabs.addTab(self._connections_tab, "Connections")
        self._tabs.addTab(self._wg_tab,          "Electrodes")
        self._tabs.addTab(self._sr530_tab,       "Lock-In (SR530)")
        self._tabs.addTab(self._analysis_tab,    "Analysis")
        self._tabs.addTab(self._control_tab,     "Control")
        self._tabs.addTab(self._calibration_tab, "Calibration")

        # Experiment tab holds the photon-order scan and the command sequencer.
        self._experiment_container = QTabWidget()
        self._experiment_container.addTab(self._experiment_tab, "Photon Order")
        self._experiment_container.addTab(self._sequencer_tab, "Command Sequence")
        self._tabs.addTab(self._experiment_container, "Experiment")

        # "Launch Charge Control" → switch to Waveform Control tab
        self._connections_tab.launch_clicked.connect(
            lambda: self._tabs.setCurrentWidget(self._wg_tab)
        )

        QVBoxLayout(self).addWidget(self._tabs)

    def _on_seq_set_electrode(self, amp: float):
        """Sequencer 'set electrode' step: set the monitored-axis drive
        amplitude (Vpp), re-apply it (programs the AFG + re-mirrors the lock-in
        reference), and update the charge normalization immediately.  Runs on
        the GUI thread (queued from the sequencer worker)."""
        axis = self._analysis_tab.monitor_axis()
        drive = getattr(self._wg_tab, f"{axis}_drive", None)
        if drive is None:
            return
        drive._amp.setValue(amp)
        drive._apply()
        self._analysis_tab.set_current_drive_amp(amp)

    def _on_lockin_cal_saved(self, vpe: float, drive_amp: float, kind: str):
        """A lock-in calibration was saved — push V/e and the calibration drive
        amplitude into the Analysis tab so the live readout normalizes by it."""
        self._analysis_tab.set_volts_per_electron(vpe, kind)
        self._analysis_tab.set_cal_drive_amp(drive_amp, kind)

    def _sync_actuators(self):
        """Wire WaveformControlTab actuators into ChargeController and
        PhotonOrderExperiment, and keep the Analysis tab's drive-amplitude
        normalization current (catches manual Electrodes-tab amplitude changes;
        the setback pushes immediately during charging)."""
        self._charge_ctrl.set_actuators(
            flashlamp=self._wg_tab.flashlamp,
            filament=self._drive_setback,   # filament wrapped: drive parks low while heating
        )
        self._photon_exp.set_actuators(
            flashlamp=self._wg_tab.flashlamp,
            filament=self._drive_setback,
        )
        self._analysis_tab.set_current_drive_amp(
            self._drive_setback.effective_amplitude())

    def closeEvent(self, event):
        self._sr530_tab.stop()
        if self._analysis_tab.source is not None and self._analysis_tab.source.is_running:
            self._analysis_tab._on_stop()
        if self._charge_ctrl.is_running:
            self._charge_ctrl.stop()
        if self._photon_exp.is_running:
            self._photon_exp.abort()
        if self._sequencer.is_running:
            self._sequencer.stop()
        configs = self._connections_tab.get_all_configs()
        configs["Analysis"]      = self._analysis_tab.get_config()
        configs["Control"]       = self._control_tab.get_config()
        configs["Calibration"]   = self._calibration_tab.get_config()
        configs["Experiment"]    = self._experiment_tab.get_config()
        configs["Sequencer"]     = self._sequencer_tab.get_config()
        configs["ElectrodeMap"]  = self._wg_tab.electrode_map.get_config()
        configs["LockInRef"]     = self._wg_tab.lockin_ref.get_config()
        configs["NGEMap"]        = self._wg_tab.nge_map.get_config()
        configs["FlashControl"]  = self._wg_tab.flash_control.get_config()
        configs["FilamentPower"] = self._wg_tab.filament_power.get_config()
        _append_log(configs)
        # Release the power supply (all outputs off, local control).
        try:
            nge = self._connections_tab.get_nge()
            if nge is not None:
                nge.disconnect()
        except Exception:
            pass
        event.accept()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

class ChargeWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("usphere Paul Trap Control")
        self.resize(860, 720)
        self._widget = ChargeWidget()
        self.setCentralWidget(self._widget)


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
    win = ChargeWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
