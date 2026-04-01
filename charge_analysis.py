"""
charge_analysis.py

Charge state analysis module for usphere-charge.

Provides:
    ChargeStateSource  — abstract base for charge state data providers
    FileBasedSource    — watches a DAQ output directory for new H5 files,
                         runs checkQ correlation analysis on each, emits results
    LockInSource       — stub for future lock-in amplifier integration

    AnalysisTab        — PyQt5 widget for the GUI "Analysis" tab
"""

from __future__ import annotations

import abc
import os
import sys
import time
from pathlib import Path
from typing import Callable

from PyQt5.QtCore import QObject, QThread, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

import numpy as np

# ---------------------------------------------------------------------------
# Import checkQ and openh5 from resources
# ---------------------------------------------------------------------------

_SCRIPTS_PATH = Path(__file__).parent / "resources" / "Microsphere-Utility-Scripts"
if str(_SCRIPTS_PATH) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_PATH))

import checkQ as cq
import openh5 as h5


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------

class ChargeStateSource(abc.ABC):
    """
    Interface for anything that provides a stream of charge-state measurements.
    The control loop and GUI depend only on this interface.
    """

    @abc.abstractmethod
    def get_latest(self) -> dict | None:
        """
        Return the most recent measurement dict, or None if no data yet.
        Expected keys (when available):
            charge_e, polarity, timestamp, raw (full measure_charge dict)
        """

    @abc.abstractmethod
    def start(self) -> None:
        """Begin producing measurements."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Stop producing measurements."""

    @property
    @abc.abstractmethod
    def is_running(self) -> bool:
        ...


# ---------------------------------------------------------------------------
# FileBasedSource
# ---------------------------------------------------------------------------

class FileBasedSource(ChargeStateSource):
    """
    Watches a directory for new .h5 files produced by usphere-DAQ.
    Each new file is read, correlation analysis is run via checkQ, and the
    result is stored / emitted via callbacks.
    """

    def __init__(
        self,
        watch_dir: str,
        calibration_file: str,
        sphere_diameter_um: float,
        position_channel: int = 0,
        drive_channel: int = 10,
        on_result: Callable[[dict], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ):
        self._watch_dir = watch_dir
        self._calibration_file = calibration_file
        self._sphere_diameter_um = sphere_diameter_um
        self._position_channel = position_channel
        self._drive_channel = drive_channel
        self._on_result = on_result
        self._on_error = on_error

        self._running = False
        self._thread: _FileWatchThread | None = None
        self._latest: dict | None = None
        self._calibration: dict | None = None

    # -- ChargeStateSource interface --

    def get_latest(self) -> dict | None:
        return self._latest

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = _FileWatchThread(self)
        self._thread.result_ready.connect(self._handle_result)
        self._thread.error.connect(self._handle_error)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.wait(3000)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._running

    # -- internal --

    def _handle_result(self, result: dict):
        self._latest = result
        if self._on_result:
            self._on_result(result)

    def _handle_error(self, msg: str):
        if self._on_error:
            self._on_error(msg)


class _FileWatchThread(QThread):
    """Background thread that polls the watch directory for new H5 files."""

    result_ready = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, source: FileBasedSource):
        super().__init__()
        self._source = source

    def run(self):
        src = self._source
        processed: set[str] = set()
        calibration: dict | None = None

        while src._running:
            try:
                h5_files = sorted(
                    [f.stem for f in Path(src._watch_dir).glob("*.h5")],
                    key=lambda x: int(x.rsplit("_", 1)[-1]),
                )
            except Exception as e:
                self.error.emit(f"Error listing files: {e}")
                time.sleep(2)
                continue

            new_files = [f for f in h5_files if f not in processed]

            if not new_files:
                time.sleep(1)
                continue

            for filename in new_files:
                if not src._running:
                    break

                # If only one new file, wait a moment so DAQ can finish writing
                if len(new_files) == 1:
                    time.sleep(1)

                fpath = os.path.join(src._watch_dir, filename + ".h5")
                try:
                    result = self._process_file(fpath, calibration)
                    # Lazy-load calibration from first file's drive frequency
                    if calibration is None and result is not None:
                        calibration = result.get("_calibration")
                    if result is not None:
                        self.result_ready.emit(result)
                except Exception as e:
                    self.error.emit(f"{filename}: {e}")

                processed.add(filename)

            time.sleep(1)

    def _process_file(self, fpath: str, calibration: dict | None) -> dict | None:
        src = self._source
        cols = [src._position_channel, src._drive_channel]

        cdat, attr, fhandle = h5.get_data(fpath)
        try:
            if len(cdat) == 0:
                return None
            sample_rate = float(attr["Fsamp"])
            col_data = h5.get_cols(cdat, attr, cols)
            t = col_data[0]
            x = col_data[1]   # position
            d = col_data[2]   # drive
        finally:
            if fhandle is not None:
                fhandle.close()

        # Determine drive frequency
        f0, drive_hp, drive_bp = cq.get_drive_frequency(d, sample_rate)

        # Load calibration on first file if not already loaded
        if calibration is None:
            calibration = cq.load_calibration(
                src._calibration_file, src._sphere_diameter_um, f0
            )

        timestamp = float(attr.get("Time", time.time()))
        duration = float(t[-1] - t[0]) if len(t) > 1 else 1.0

        if calibration is not None:
            meas = cq.measure_charge(d, x, sample_rate, calibration)
            q_corr = meas["polarity_corr"] * meas["n_charges_corr"]
            q_pos = meas["polarity_phase"] * meas["n_charges_pos"]
            return {
                "charge_e": q_corr,
                "polarity": meas["polarity_corr"],
                "charge_pos": q_pos,
                "polarity_phase": meas["polarity_phase"],
                "phase": meas["phase"],
                "drive_scale": meas["drive_scale"],
                "f0": meas["f0"],
                "timestamp": timestamp,
                "duration": duration,
                "file": os.path.basename(fpath),
                "calibrated": True,
                "raw": meas,
                "_calibration": calibration,
            }
        else:
            # Uncalibrated fallback
            lags, corr, corr_sm = cq.correlate_drive_position(
                drive_hp, x, sample_rate, f0, smoothed=True
            )
            peak_idx = np.argmax(np.abs(corr_sm))
            pos_resp, phase = cq.get_tone_response(x, drive_hp, sample_rate, f0)
            return {
                "charge_e": float(np.abs(corr_sm[peak_idx])),
                "polarity": float(np.sign(corr_sm[peak_idx])),
                "charge_pos": float(pos_resp),
                "polarity_phase": float(np.sign(phase)),
                "phase": float(phase),
                "drive_scale": 1.0,
                "f0": float(f0),
                "timestamp": timestamp,
                "duration": duration,
                "file": os.path.basename(fpath),
                "calibrated": False,
                "raw": None,
                "_calibration": None,
            }


# ---------------------------------------------------------------------------
# LockInSource — reads SR530 X output via ESP32/ADS1115 over USB serial
# ---------------------------------------------------------------------------

class _LockInSerialThread(QThread):
    """Background thread: reads 'V:±x.xxxxxx' lines from the ESP32."""

    voltage_ready = pyqtSignal(float)   # raw voltage from lock-in X output
    error = pyqtSignal(str)

    def __init__(self, port: str, baudrate: int = 115200):
        super().__init__()
        self._port = port
        self._baudrate = baudrate
        self._running = False

    def run(self):
        import serial
        self._running = True
        try:
            ser = serial.Serial(
                self._port, self._baudrate, timeout=1
            )
        except Exception as e:
            self.error.emit(f"Cannot open {self._port}: {e}")
            self._running = False
            return

        ser.reset_input_buffer()
        try:
            while self._running:
                raw = ser.readline()
                if not raw:
                    continue
                line = raw.decode("ascii", errors="replace").strip()
                if line.startswith("V:"):
                    try:
                        volts = float(line[2:])
                        self.voltage_ready.emit(volts)
                    except ValueError:
                        pass
                # Ignore non-voltage lines (comments, OK responses, etc.)
        finally:
            ser.close()
            self._running = False

    def stop(self):
        self._running = False


class LockInSource(ChargeStateSource):
    """
    Reads the SR530 X output via the ESP32 lockin_charge_reader over USB serial.

    The ESP32 streams lines like ``V:+0.00234`` at ~200 Hz.
    This source converts voltage → charge using a ``volts_per_electron``
    calibration factor:  n_charges = V_x / volts_per_electron.

    The sign of V_x directly gives charge polarity (after reference phase
    is properly set on the SR530).
    """

    def __init__(
        self,
        serial_port: str,
        volts_per_electron: float = 1.0,
        baudrate: int = 115200,
        on_result: Callable[[dict], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ):
        self._serial_port = serial_port
        self._volts_per_electron = volts_per_electron
        self._baudrate = baudrate
        self._on_result = on_result
        self._on_error = on_error

        self._thread: _LockInSerialThread | None = None
        self._latest: dict | None = None
        self._running = False
        self._sample_count = 0

    # -- ChargeStateSource interface --

    def get_latest(self) -> dict | None:
        return self._latest

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._sample_count = 0
        self._thread = _LockInSerialThread(self._serial_port, self._baudrate)
        self._thread.voltage_ready.connect(self._handle_voltage)
        self._thread.error.connect(self._handle_error)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.stop()
            self._thread.wait(3000)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._running

    # -- internal --

    def _handle_voltage(self, volts: float):
        self._sample_count += 1
        polarity = 1.0 if volts >= 0 else -1.0

        if self._volts_per_electron > 0:
            n_charges = volts / self._volts_per_electron
        else:
            n_charges = volts  # uncalibrated — just show raw voltage

        result = {
            "charge_e": float(n_charges),
            "polarity": polarity,
            "charge_pos": float(n_charges),  # same value (no second method)
            "polarity_phase": polarity,
            "phase": 0.0,
            "drive_scale": 1.0,
            "f0": 0.0,
            "timestamp": time.time(),
            "duration": 0.0,
            "file": f"sample #{self._sample_count}",
            "calibrated": self._volts_per_electron > 0
                          and self._volts_per_electron != 1.0,
            "raw_voltage": float(volts),
            "raw": None,
            "_calibration": None,
        }
        self._latest = result
        if self._on_result:
            self._on_result(result)

    def _handle_error(self, msg: str):
        self._running = False
        if self._on_error:
            self._on_error(msg)


# ---------------------------------------------------------------------------
# SR530SerialSource — polls SR530 directly over RS232 (no ESP32 needed)
# ---------------------------------------------------------------------------

class _SR530PollThread(QThread):
    """Background thread: polls SR530 X output over RS232 at a target rate."""

    voltage_ready = pyqtSignal(float)   # X output in real volts
    error = pyqtSignal(str)

    def __init__(self, port: str, poll_hz: float = 30.0):
        super().__init__()
        self._port = port
        self._poll_interval = 1.0 / max(poll_hz, 1.0)
        self._running = False

    def run(self):
        from sr530_controller import SR530Controller
        self._running = True
        try:
            lia = SR530Controller(self._port)
            lia.connect()
        except Exception as e:
            self.error.emit(f"Cannot connect to SR530 on {self._port}: {e}")
            self._running = False
            return

        try:
            while self._running:
                t0 = time.time()
                try:
                    volts = lia.get_x_volts()
                    self.voltage_ready.emit(volts)
                except Exception as e:
                    self.error.emit(f"SR530 read error: {e}")
                    break
                elapsed = time.time() - t0
                sleep_time = self._poll_interval - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)
        finally:
            try:
                lia.disconnect()
            except Exception:
                pass
            self._running = False

    def stop(self):
        self._running = False


class SR530SerialSource(ChargeStateSource):
    """
    Reads the SR530 X output directly over RS232 — no ESP32 needed.

    Polls the SR530 at ~30 Hz (limited by 9600 baud command/response).
    Converts X voltage → charge using a ``volts_per_electron`` calibration.
    Polarity is determined by the sign of the X output (assumes reference
    phase has been correctly set on the SR530).
    """

    def __init__(
        self,
        serial_port: str,
        volts_per_electron: float = 1.0,
        poll_hz: float = 30.0,
        on_result: Callable[[dict], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ):
        self._serial_port = serial_port
        self._volts_per_electron = volts_per_electron
        self._poll_hz = poll_hz
        self._on_result = on_result
        self._on_error = on_error

        self._thread: _SR530PollThread | None = None
        self._latest: dict | None = None
        self._running = False
        self._sample_count = 0

    # -- ChargeStateSource interface --

    def get_latest(self) -> dict | None:
        return self._latest

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._sample_count = 0
        self._thread = _SR530PollThread(self._serial_port, self._poll_hz)
        self._thread.voltage_ready.connect(self._handle_voltage)
        self._thread.error.connect(self._handle_error)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.stop()
            self._thread.wait(3000)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._running

    # -- internal --

    def _handle_voltage(self, volts: float):
        self._sample_count += 1
        polarity = 1.0 if volts >= 0 else -1.0

        if self._volts_per_electron > 0 and self._volts_per_electron != 1.0:
            n_charges = volts / self._volts_per_electron
        else:
            n_charges = volts  # uncalibrated — show raw voltage

        result = {
            "charge_e": float(n_charges),
            "polarity": polarity,
            "charge_pos": float(n_charges),
            "polarity_phase": polarity,
            "phase": 0.0,
            "drive_scale": 1.0,
            "f0": 0.0,
            "timestamp": time.time(),
            "duration": 0.0,
            "file": f"sample #{self._sample_count}",
            "calibrated": self._volts_per_electron > 0
                          and self._volts_per_electron != 1.0,
            "raw_voltage": float(volts),
            "raw": None,
            "_calibration": None,
        }
        self._latest = result
        if self._on_result:
            self._on_result(result)

    def _handle_error(self, msg: str):
        self._running = False
        if self._on_error:
            self._on_error(msg)


# ---------------------------------------------------------------------------
# Analysis tab (PyQt5 widget)
# ---------------------------------------------------------------------------

class AnalysisTab(QWidget):
    """
    GUI tab for live charge state monitoring.

    Supports three source types:
        File-based    — watches a DAQ directory for new H5 files (checkQ analysis)
        Lock-in ESP32 — reads SR530 X output via ESP32/ADS1115 USB serial
        Lock-in SR530 — polls SR530 X output directly over RS232 (no ESP32)
    """

    # Emitted whenever a new charge measurement arrives — control loop can connect
    charge_updated = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._source: ChargeStateSource | None = None
        self._history: list[dict] = []
        self._plot_widget = None
        self._plot_line_corr = None
        self._plot_line_pos = None
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        from PyQt5.QtWidgets import QComboBox, QStackedWidget

        outer = QVBoxLayout(self)
        outer.setSpacing(8)
        outer.setContentsMargins(6, 6, 6, 6)

        # --- Source selector ---
        src_row = QHBoxLayout()
        src_row.addWidget(QLabel("Source type:"))
        self._source_combo = QComboBox()
        self._source_combo.addItems([
            "File-based (checkQ)",
            "Lock-in (ESP32 analog)",
            "Lock-in (SR530 direct)",
        ])
        self._source_combo.currentIndexChanged.connect(self._on_source_type_changed)
        src_row.addWidget(self._source_combo)
        src_row.addStretch()
        outer.addLayout(src_row)

        # --- Stacked config panels ---
        self._config_stack = QStackedWidget()

        # Page 0: File-based config
        file_cfg = QGroupBox("File-based configuration")
        g = QGridLayout(file_cfg)
        g.setColumnStretch(1, 1)
        row = 0

        g.addWidget(QLabel("Watch directory:"), row, 0)
        self._dir_edit = QLineEdit()
        g.addWidget(self._dir_edit, row, 1)
        browse_dir = QPushButton("Browse…")
        browse_dir.setMaximumWidth(80)
        browse_dir.clicked.connect(self._browse_dir)
        g.addWidget(browse_dir, row, 2)
        row += 1

        g.addWidget(QLabel("Calibration file:"), row, 0)
        self._cal_edit = QLineEdit(
            str(Path(__file__).parent / "resources"
                / "Microsphere-Utility-Scripts" / "checkQ_calibration.json")
        )
        g.addWidget(self._cal_edit, row, 1)
        browse_cal = QPushButton("Browse…")
        browse_cal.setMaximumWidth(80)
        browse_cal.clicked.connect(self._browse_cal)
        g.addWidget(browse_cal, row, 2)
        row += 1

        g.addWidget(QLabel("Sphere diameter (µm):"), row, 0)
        self._diam_edit = QLineEdit("10.0")
        self._diam_edit.setMaximumWidth(100)
        g.addWidget(self._diam_edit, row, 1)
        row += 1

        g.addWidget(QLabel("Position channel (ai):"), row, 0)
        self._pos_ch_spin = QSpinBox()
        self._pos_ch_spin.setRange(0, 31)
        self._pos_ch_spin.setValue(0)
        self._pos_ch_spin.setMaximumWidth(80)
        g.addWidget(self._pos_ch_spin, row, 1)
        row += 1

        g.addWidget(QLabel("Drive channel (ai):"), row, 0)
        self._drive_ch_spin = QSpinBox()
        self._drive_ch_spin.setRange(0, 31)
        self._drive_ch_spin.setValue(10)
        self._drive_ch_spin.setMaximumWidth(80)
        g.addWidget(self._drive_ch_spin, row, 1)
        row += 1

        self._config_stack.addWidget(file_cfg)

        # Page 1: Lock-in config
        li_cfg = QGroupBox("Lock-in configuration")
        lg = QGridLayout(li_cfg)
        lg.setColumnStretch(1, 1)
        row = 0

        lg.addWidget(QLabel("ESP32 serial port:"), row, 0)
        self._li_port_edit = QLineEdit("COM3")
        self._li_port_edit.setMaximumWidth(120)
        lg.addWidget(self._li_port_edit, row, 1)
        row += 1

        lg.addWidget(QLabel("Baud rate:"), row, 0)
        self._li_baud_edit = QLineEdit("115200")
        self._li_baud_edit.setMaximumWidth(100)
        lg.addWidget(self._li_baud_edit, row, 1)
        row += 1

        lg.addWidget(QLabel("Volts per electron:"), row, 0)
        self._li_vpe_edit = QLineEdit("1.0")
        self._li_vpe_edit.setToolTip(
            "Calibration: lock-in X output voltage per unit charge.\n"
            "Set to 1.0 for uncalibrated (shows raw voltage).\n"
            "Determine from known charge states."
        )
        self._li_vpe_edit.setMaximumWidth(120)
        lg.addWidget(self._li_vpe_edit, row, 1)
        row += 1

        lg.addWidget(QLabel("SR530 serial port (optional):"), row, 0)
        self._sr530_port_edit = QLineEdit("")
        self._sr530_port_edit.setPlaceholderText("e.g. COM5 — leave blank to skip")
        self._sr530_port_edit.setMaximumWidth(200)
        lg.addWidget(self._sr530_port_edit, row, 1)
        row += 1

        self._config_stack.addWidget(li_cfg)

        # Page 2: SR530 direct RS232 config
        sr_cfg = QGroupBox("SR530 direct RS232 configuration")
        sg = QGridLayout(sr_cfg)
        sg.setColumnStretch(1, 1)
        row = 0

        sg.addWidget(QLabel("SR530 serial port:"), row, 0)
        self._sr_port_edit = QLineEdit("COM5")
        self._sr_port_edit.setToolTip(
            "COM port for the SR530 RS232 connection.\n"
            "If using a Brainbox serial-to-ethernet, enter\n"
            "the virtual COM port assigned by the driver."
        )
        self._sr_port_edit.setMaximumWidth(120)
        sg.addWidget(self._sr_port_edit, row, 1)
        row += 1

        sg.addWidget(QLabel("Poll rate (Hz):"), row, 0)
        self._sr_poll_edit = QLineEdit("30")
        self._sr_poll_edit.setToolTip(
            "How many times per second to query the SR530.\n"
            "Max ~50 Hz at 9600 baud.  30 Hz is a safe default."
        )
        self._sr_poll_edit.setMaximumWidth(80)
        sg.addWidget(self._sr_poll_edit, row, 1)
        row += 1

        sg.addWidget(QLabel("Volts per electron:"), row, 0)
        self._sr_vpe_edit = QLineEdit("1.0")
        self._sr_vpe_edit.setToolTip(
            "Calibration: SR530 X output (volts) per unit charge.\n"
            "Set to 1.0 for uncalibrated (shows raw voltage).\n"
            "Determine from known charge states."
        )
        self._sr_vpe_edit.setMaximumWidth(120)
        sg.addWidget(self._sr_vpe_edit, row, 1)
        row += 1

        self._config_stack.addWidget(sr_cfg)

        outer.addWidget(self._config_stack)

        # --- Start / Stop ---
        btn_row = QHBoxLayout()
        self._start_btn = QPushButton("Start monitoring")
        self._start_btn.setMinimumWidth(140)
        self._start_btn.clicked.connect(self._on_start)
        btn_row.addWidget(self._start_btn)

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setMinimumWidth(80)
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop)
        btn_row.addWidget(self._stop_btn)

        btn_row.addStretch()

        self._status_lbl = QLabel("Idle")
        self._status_lbl.setStyleSheet("color: gray;")
        btn_row.addWidget(self._status_lbl)

        outer.addLayout(btn_row)

        # --- Live charge display ---
        readout = QGroupBox("Live charge readout")
        rg = QGridLayout(readout)

        self._charge_lbl = QLabel("— e")
        self._charge_lbl.setStyleSheet("font-size: 28px; font-weight: bold;")
        self._charge_lbl.setAlignment(Qt.AlignCenter)
        rg.addWidget(self._charge_lbl, 0, 0, 1, 2)

        rg.addWidget(QLabel("Corr:"), 1, 0)
        self._corr_detail = QLabel("—")
        rg.addWidget(self._corr_detail, 1, 1)

        rg.addWidget(QLabel("FFT:"), 2, 0)
        self._fft_detail = QLabel("—")
        rg.addWidget(self._fft_detail, 2, 1)

        rg.addWidget(QLabel("Drive f₀:"), 3, 0)
        self._f0_lbl = QLabel("—")
        rg.addWidget(self._f0_lbl, 3, 1)

        rg.addWidget(QLabel("File:"), 4, 0)
        self._file_lbl = QLabel("—")
        rg.addWidget(self._file_lbl, 4, 1)

        outer.addWidget(readout)

        # --- Plot ---
        self._init_plot(outer)

    def _init_plot(self, layout):
        """Try to set up a pyqtgraph plot; fall back to a placeholder."""
        try:
            import pyqtgraph as pg

            pw = pg.PlotWidget(title="Charge vs file index")
            pw.setLabel("left", "Charge (e)")
            pw.setLabel("bottom", "File #")
            pw.addLegend()
            pw.showGrid(x=True, y=True, alpha=0.3)
            self._plot_line_corr = pw.plot([], [], pen="b", name="Correlation")
            self._plot_line_pos = pw.plot([], [], pen="r", name="FFT")
            self._plot_widget = pw
            layout.addWidget(pw, stretch=1)
        except ImportError:
            lbl = QLabel("Install pyqtgraph for live plotting:  pip install pyqtgraph")
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color: gray; font-size: 12px;")
            layout.addWidget(lbl, stretch=1)

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _on_source_type_changed(self, index: int):
        self._config_stack.setCurrentIndex(index)

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def get_config(self) -> dict:
        return {
            "source_type": self._source_combo.currentIndex(),
            # File-based
            "watch_dir": self._dir_edit.text(),
            "calibration_file": self._cal_edit.text(),
            "sphere_diameter_um": self._diam_edit.text(),
            "position_channel": self._pos_ch_spin.value(),
            "drive_channel": self._drive_ch_spin.value(),
            # Lock-in (ESP32)
            "li_serial_port": self._li_port_edit.text(),
            "li_baud_rate": self._li_baud_edit.text(),
            "li_volts_per_electron": self._li_vpe_edit.text(),
            "sr530_serial_port": self._sr530_port_edit.text(),
            # Lock-in (SR530 direct)
            "sr_port": self._sr_port_edit.text(),
            "sr_poll_hz": self._sr_poll_edit.text(),
            "sr_volts_per_electron": self._sr_vpe_edit.text(),
        }

    def restore_config(self, cfg: dict):
        if "source_type" in cfg:
            self._source_combo.setCurrentIndex(int(cfg["source_type"]))
        if "watch_dir" in cfg:
            self._dir_edit.setText(str(cfg["watch_dir"]))
        if "calibration_file" in cfg:
            self._cal_edit.setText(str(cfg["calibration_file"]))
        if "sphere_diameter_um" in cfg:
            self._diam_edit.setText(str(cfg["sphere_diameter_um"]))
        if "position_channel" in cfg:
            self._pos_ch_spin.setValue(int(cfg["position_channel"]))
        if "drive_channel" in cfg:
            self._drive_ch_spin.setValue(int(cfg["drive_channel"]))
        if "li_serial_port" in cfg:
            self._li_port_edit.setText(str(cfg["li_serial_port"]))
        if "li_baud_rate" in cfg:
            self._li_baud_edit.setText(str(cfg["li_baud_rate"]))
        if "li_volts_per_electron" in cfg:
            self._li_vpe_edit.setText(str(cfg["li_volts_per_electron"]))
        if "sr530_serial_port" in cfg:
            self._sr530_port_edit.setText(str(cfg["sr530_serial_port"]))
        if "sr_port" in cfg:
            self._sr_port_edit.setText(str(cfg["sr_port"]))
        if "sr_poll_hz" in cfg:
            self._sr_poll_edit.setText(str(cfg["sr_poll_hz"]))
        if "sr_volts_per_electron" in cfg:
            self._sr_vpe_edit.setText(str(cfg["sr_volts_per_electron"]))

    # ------------------------------------------------------------------
    # Browse dialogs
    # ------------------------------------------------------------------

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select DAQ output directory")
        if d:
            self._dir_edit.setText(d)

    def _browse_cal(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Select calibration JSON", "", "JSON (*.json);;All (*)"
        )
        if f:
            self._cal_edit.setText(f)

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    def _on_start(self):
        source_type = self._source_combo.currentIndex()
        self._history.clear()

        if source_type == 0:
            # --- File-based source ---
            watch_dir = self._dir_edit.text().strip()
            if not watch_dir or not os.path.isdir(watch_dir):
                self._status_lbl.setText("Invalid directory")
                self._status_lbl.setStyleSheet("color: red;")
                return
            try:
                diam = float(self._diam_edit.text())
            except ValueError:
                self._status_lbl.setText("Invalid sphere diameter")
                self._status_lbl.setStyleSheet("color: red;")
                return

            self._source = FileBasedSource(
                watch_dir=watch_dir,
                calibration_file=self._cal_edit.text().strip(),
                sphere_diameter_um=diam,
                position_channel=self._pos_ch_spin.value(),
                drive_channel=self._drive_ch_spin.value(),
            )
            self._source.start()
            thread = self._source._thread
            if thread is not None:
                thread.result_ready.connect(self._on_new_result)
                thread.error.connect(self._on_source_error)

        elif source_type == 1:
            # --- Lock-in source ---
            port = self._li_port_edit.text().strip()
            if not port:
                self._status_lbl.setText("Enter ESP32 serial port")
                self._status_lbl.setStyleSheet("color: red;")
                return
            try:
                baud = int(self._li_baud_edit.text())
            except ValueError:
                baud = 115200
            try:
                vpe = float(self._li_vpe_edit.text())
            except ValueError:
                vpe = 1.0

            self._source = LockInSource(
                serial_port=port,
                volts_per_electron=vpe,
                baudrate=baud,
            )
            self._source.start()
            thread = self._source._thread
            if thread is not None:
                thread.voltage_ready.connect(
                    lambda _v: self._on_new_result(self._source.get_latest())
                )
                thread.error.connect(self._on_source_error)

        elif source_type == 2:
            # --- SR530 direct RS232 source ---
            port = self._sr_port_edit.text().strip()
            if not port:
                self._status_lbl.setText("Enter SR530 serial port")
                self._status_lbl.setStyleSheet("color: red;")
                return
            try:
                poll_hz = float(self._sr_poll_edit.text())
            except ValueError:
                poll_hz = 30.0
            try:
                vpe = float(self._sr_vpe_edit.text())
            except ValueError:
                vpe = 1.0

            self._source = SR530SerialSource(
                serial_port=port,
                volts_per_electron=vpe,
                poll_hz=poll_hz,
            )
            self._source.start()
            thread = self._source._thread
            if thread is not None:
                thread.voltage_ready.connect(
                    lambda _v: self._on_new_result(self._source.get_latest())
                )
                thread.error.connect(self._on_source_error)

        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._status_lbl.setText("Monitoring…")
        self._status_lbl.setStyleSheet("color: green;")

    def _on_stop(self):
        if self._source is not None:
            self._source.stop()
            self._source = None
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._status_lbl.setText("Stopped")
        self._status_lbl.setStyleSheet("color: gray;")

    # ------------------------------------------------------------------
    # Result handling
    # ------------------------------------------------------------------

    def _on_new_result(self, result: dict):
        if result is None:
            return

        self._history.append(result)

        # Throttle GUI updates for lock-in mode (~30-200 Hz input → 10 Hz display)
        is_lockin = isinstance(self._source, (LockInSource, SR530SerialSource))
        if is_lockin and len(self._history) % 20 != 0:
            # Still emit for control loop on every sample
            self.charge_updated.emit(result)
            return

        q = result["charge_e"]
        pol = result["polarity"]

        if result["calibrated"]:
            self._charge_lbl.setText(f"{q:+.1f} e")
            self._corr_detail.setText(
                f"{q:+.2f} e  (scale {result['drive_scale']:.3f})"
            )
            self._fft_detail.setText(
                f"{result['charge_pos']:+.2f} e  (phase {result['phase']:.2f} rad)"
            )
        elif is_lockin:
            raw_v = result.get("raw_voltage", q)
            self._charge_lbl.setText(f"{q:+.3f} e")
            self._corr_detail.setText(f"V_x = {raw_v:+.6f} V")
            self._fft_detail.setText(f"sample #{result.get('file', '—')}")
        else:
            self._charge_lbl.setText(f"~{abs(q):.2e} (raw)")
            self._corr_detail.setText(f"{q:.4e} (raw, no calibration)")
            self._fft_detail.setText(f"{result['charge_pos']:.4e} (raw)")

        self._f0_lbl.setText(f"{result['f0']:.2f} Hz" if result['f0'] > 0 else "—")
        self._file_lbl.setText(result.get("file", "—"))

        # Color by polarity
        if pol > 0:
            self._charge_lbl.setStyleSheet(
                "font-size: 28px; font-weight: bold; color: #2196F3;"
            )
        elif pol < 0:
            self._charge_lbl.setStyleSheet(
                "font-size: 28px; font-weight: bold; color: #F44336;"
            )
        else:
            self._charge_lbl.setStyleSheet(
                "font-size: 28px; font-weight: bold; color: gray;"
            )

        # Update plot
        self._update_plot()

        # Emit for control loop
        self.charge_updated.emit(result)

    def _on_source_error(self, msg: str):
        self._status_lbl.setText(f"Error: {msg}")
        self._status_lbl.setStyleSheet("color: red;")

    def _update_plot(self):
        if self._plot_widget is None or not self._history:
            return
        # For lock-in mode, only display the most recent 500 points
        display = self._history[-500:] if len(self._history) > 500 else self._history
        xs = list(range(len(self._history) - len(display) + 1,
                        len(self._history) + 1))
        ys_corr = [r["charge_e"] for r in display]
        ys_pos = [r["charge_pos"] for r in display]
        self._plot_line_corr.setData(xs, ys_corr)
        self._plot_line_pos.setData(xs, ys_pos)

    # ------------------------------------------------------------------
    # Public API (for control loop)
    # ------------------------------------------------------------------

    @property
    def source(self) -> ChargeStateSource | None:
        return self._source

    @property
    def history(self) -> list[dict]:
        return list(self._history)
