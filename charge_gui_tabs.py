"""
charge_gui_tabs.py

Additional PyQt5 tab widgets for charge_gui.py:

    WaveformGenTab   — control panel for a RIGOL DG822 or INSTEK AFG-2225
    ControlTab       — control loop UI  (wraps charge_control.ChargeController)
    CalibrationTab   — calibration workflow UI

All logic lives in charge_control.py / charge_calibration.py.
These are thin GUI wrappers.
"""

from __future__ import annotations

import math
import time
from functools import partial

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QCheckBox,
    QFileDialog,
)

from charge_control import ChargeController, ControlEvent, Action


# ======================================================================
# WaveformGenTab — manual waveform generator control
# ======================================================================

class WaveformGenTab(QWidget):
    """
    Manual control panel for a waveform generator.

    Works with any controller that follows the DG822Controller /
    AFG2225Controller API (setup_sine, setup_pulse, setup_dc, output_on,
    output_off, set_frequency, set_amplitude, get_status, etc.).

    The tab does NOT own the controller — it gets it from the
    ConnectionsTab via a callable (controller_getter).
    """

    def __init__(
        self,
        tab_name: str,
        controller_getter,
        channels: int = 2,
        parent=None,
    ):
        """
        Parameters
        ----------
        tab_name : str
            Display name (e.g. "Flash Lamp", "Filament", "Drive").
        controller_getter : callable
            Returns the live controller instance or None.
            e.g.  lambda: connections_tab.get_controller("FlashLamp")
        channels : int
            Number of output channels (1 or 2).
        """
        super().__init__(parent)
        self._tab_name = tab_name
        self._get_ctrl = controller_getter
        self._n_channels = channels
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(8)
        outer.setContentsMargins(6, 6, 6, 6)

        # Status bar
        status_row = QHBoxLayout()
        self._status_lbl = QLabel("Not connected")
        self._status_lbl.setStyleSheet("color: gray;")
        status_row.addWidget(self._status_lbl)
        status_row.addStretch()
        refresh_btn = QPushButton("Refresh status")
        refresh_btn.setMaximumWidth(120)
        refresh_btn.clicked.connect(self._refresh_status)
        status_row.addWidget(refresh_btn)
        outer.addLayout(status_row)

        # Per-channel groups
        self._ch_widgets: list[dict] = []
        for ch in range(1, self._n_channels + 1):
            grp = QGroupBox(f"Channel {ch}")
            g = QGridLayout(grp)
            g.setColumnStretch(1, 1)
            row = 0
            w: dict = {"channel": ch}

            # Waveform type
            g.addWidget(QLabel("Waveform:"), row, 0)
            wf_combo = QComboBox()
            wf_combo.addItems(["Sine", "Square", "Pulse", "Ramp", "DC", "Noise"])
            w["wf_combo"] = wf_combo
            g.addWidget(wf_combo, row, 1)
            row += 1

            # Frequency
            g.addWidget(QLabel("Frequency (Hz):"), row, 0)
            freq_spin = QDoubleSpinBox()
            freq_spin.setRange(0.001, 25e6)
            freq_spin.setDecimals(3)
            freq_spin.setValue(100.0)
            w["freq"] = freq_spin
            g.addWidget(freq_spin, row, 1)
            row += 1

            # Amplitude
            g.addWidget(QLabel("Amplitude (Vpp):"), row, 0)
            amp_spin = QDoubleSpinBox()
            amp_spin.setRange(0.0, 20.0)
            amp_spin.setDecimals(3)
            amp_spin.setValue(5.0)
            w["amp"] = amp_spin
            g.addWidget(amp_spin, row, 1)
            row += 1

            # Offset
            g.addWidget(QLabel("Offset (V):"), row, 0)
            off_spin = QDoubleSpinBox()
            off_spin.setRange(-10.0, 10.0)
            off_spin.setDecimals(3)
            off_spin.setValue(0.0)
            w["offset"] = off_spin
            g.addWidget(off_spin, row, 1)
            row += 1

            # Duty cycle (for pulse/square)
            g.addWidget(QLabel("Duty cycle (%):"), row, 0)
            duty_spin = QDoubleSpinBox()
            duty_spin.setRange(0.1, 99.9)
            duty_spin.setDecimals(1)
            duty_spin.setValue(50.0)
            w["duty"] = duty_spin
            g.addWidget(duty_spin, row, 1)
            row += 1

            # Apply / Enable / Disable
            btn_row = QHBoxLayout()
            apply_btn = QPushButton("Apply settings")
            apply_btn.clicked.connect(partial(self._on_apply, ch))
            btn_row.addWidget(apply_btn)

            on_btn = QPushButton("Output ON")
            on_btn.setStyleSheet("background-color: #4CAF50; color: white;")
            on_btn.clicked.connect(partial(self._on_output_on, ch))
            btn_row.addWidget(on_btn)

            off_btn = QPushButton("Output OFF")
            off_btn.setStyleSheet("background-color: #F44336; color: white;")
            off_btn.clicked.connect(partial(self._on_output_off, ch))
            btn_row.addWidget(off_btn)

            btn_row.addStretch()
            g.addLayout(btn_row, row, 0, 1, 2)
            row += 1

            # Per-channel status
            ch_status = QLabel("—")
            ch_status.setStyleSheet("color: gray;")
            w["status"] = ch_status
            g.addWidget(ch_status, row, 0, 1, 2)

            self._ch_widgets.append(w)
            outer.addWidget(grp)

        outer.addStretch()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _get_controller(self):
        ctrl = self._get_ctrl()
        if ctrl is None or not ctrl.is_connected:
            self._status_lbl.setText("Not connected — go to Connections tab")
            self._status_lbl.setStyleSheet("color: red;")
            return None
        self._status_lbl.setText("Connected")
        self._status_lbl.setStyleSheet("color: green;")
        return ctrl

    def _on_apply(self, channel: int):
        ctrl = self._get_controller()
        if ctrl is None:
            return
        w = self._ch_widgets[channel - 1]
        wf = w["wf_combo"].currentText()
        freq = w["freq"].value()
        amp = w["amp"].value()
        offset = w["offset"].value()
        duty = w["duty"].value()
        try:
            if wf == "Sine":
                ctrl.setup_sine(channel, frequency=freq, amplitude=amp, offset=offset)
            elif wf == "Square":
                ctrl.setup_square(channel, frequency=freq, amplitude=amp,
                                  offset=offset, duty_cycle=duty)
            elif wf == "Pulse":
                ctrl.setup_pulse(channel, frequency=freq, amplitude=amp,
                                 offset=offset, duty_cycle=duty)
            elif wf == "Ramp":
                ctrl.setup_ramp(channel, frequency=freq, amplitude=amp, offset=offset)
            elif wf == "DC":
                ctrl.setup_dc(channel, voltage=offset)
            elif wf == "Noise":
                ctrl.setup_noise(channel, amplitude=amp, offset=offset)
            w["status"].setText(f"{wf} — {freq:.3f} Hz, {amp:.3f} Vpp applied")
            w["status"].setStyleSheet("color: green;")
        except Exception as e:
            w["status"].setText(f"Error: {e}")
            w["status"].setStyleSheet("color: red;")

    def _on_output_on(self, channel: int):
        ctrl = self._get_controller()
        if ctrl is None:
            return
        try:
            ctrl.output_on(channel)
            self._ch_widgets[channel - 1]["status"].setText("Output ON")
            self._ch_widgets[channel - 1]["status"].setStyleSheet("color: green;")
        except Exception as e:
            self._ch_widgets[channel - 1]["status"].setText(f"Error: {e}")
            self._ch_widgets[channel - 1]["status"].setStyleSheet("color: red;")

    def _on_output_off(self, channel: int):
        ctrl = self._get_controller()
        if ctrl is None:
            return
        try:
            ctrl.output_off(channel)
            self._ch_widgets[channel - 1]["status"].setText("Output OFF")
            self._ch_widgets[channel - 1]["status"].setStyleSheet("color: gray;")
        except Exception as e:
            self._ch_widgets[channel - 1]["status"].setText(f"Error: {e}")
            self._ch_widgets[channel - 1]["status"].setStyleSheet("color: red;")

    def _refresh_status(self):
        ctrl = self._get_controller()
        if ctrl is None:
            return
        try:
            status = ctrl.get_status()
            self._status_lbl.setText(f"Connected — {ctrl.idn or 'unknown'}")
            self._status_lbl.setStyleSheet("color: green;")
            # Update per-channel status
            for w in self._ch_widgets:
                ch = w["channel"]
                ch_info = status.get(f"ch{ch}", status.get("channels", {}).get(str(ch), {}))
                if ch_info:
                    w["status"].setText(str(ch_info))
                    w["status"].setStyleSheet("color: green;")
        except Exception as e:
            self._status_lbl.setText(f"Error reading status: {e}")
            self._status_lbl.setStyleSheet("color: red;")


# ======================================================================
# ControlTab — closed-loop charge control
# ======================================================================

class ControlTab(QWidget):
    """
    Charge control loop UI (wraps charge_control.ChargeController).

    Three ways to move the charge, all sharing one controller (one runs at a
    time):
      • Flash lamp — raise charge (+); continuous, stops at the target.
      • Filament   — lower charge (−); pulse-wait-read ramp (reads are clean
        because the filament is off during the wait).
      • Go to target — auto-picks flash (below target) or filament (above), to
        a target ± tolerance, with a safety timeout.

    A drive setback (optional) parks the electrode drive low while either tool
    runs, so a highly-charged sphere isn't over-driven.  Gray diagnostics show
    what it's doing and the per-read Δq for each tool.
    """

    def __init__(self, controller: ChargeController, parent=None):
        super().__init__(parent)
        self._ctrl = controller
        self._build_ui()
        self._ctrl.action_changed.connect(self._on_action_changed)
        self._ctrl.event_logged.connect(self._on_event_logged)
        self._ctrl.cycle_logged.connect(self._on_cycle_logged)
        self._ctrl.stopped.connect(self._on_stopped)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    @staticmethod
    def _spin(lo, hi, dec, val, suffix):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(dec)
        s.setValue(val)
        s.setSuffix(suffix)
        s.setMaximumWidth(110)
        return s

    def _build_ui(self):
        from wg_control_tab import FilamentRampConfig, CycleLog

        outer = QVBoxLayout(self)
        outer.setSpacing(8)
        outer.setContentsMargins(6, 6, 6, 6)

        intro = QLabel(
            "Change the sphere's charge with the flash lamp (raises +) or the "
            "filament (lowers −), or set a target and let it pick the tool. Only "
            "one runs at a time; Cancel turns the outputs off.")
        intro.setStyleSheet("color: #9E9E9E; font-size: 11px;")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        # --- Flash lamp -----------------------------------------------------
        flash_grp = QGroupBox("Flash lamp — raise charge (+)")
        fg = QGridLayout(flash_grp)
        fg.addWidget(QLabel("Flash rate:"), 0, 0, Qt.AlignRight)
        self._flash_rate = self._spin(0.001, 1e6, 3, 10.0, " Hz")
        fg.addWidget(self._flash_rate, 0, 1)
        fg.addWidget(QLabel("Control voltage:"), 0, 2, Qt.AlignRight)
        self._flash_ctrl = self._spin(0.0, 32.0, 3, 0.0, " V")
        fg.addWidget(self._flash_ctrl, 0, 3)
        fg.addWidget(QLabel("Raise by:"), 1, 0, Qt.AlignRight)
        self._flash_delta = self._spin(0.0, 1000.0, 1, 0.0, " e")
        self._flash_delta.setToolTip("Raise the charge by this many e, then stop.\n"
                                     "0 = flash until you press Cancel (or timeout).")
        fg.addWidget(self._flash_delta, 1, 1)
        self._flash_btn = QPushButton("Flash (raise +)")
        self._flash_btn.setStyleSheet("background-color: #4CAF50; color: white;")
        self._flash_btn.clicked.connect(self._on_flash)
        fg.addWidget(self._flash_btn, 1, 3)
        fg.setColumnStretch(4, 1)
        outer.addWidget(flash_grp)

        # --- Filament -------------------------------------------------------
        fil_grp = QGroupBox("Filament — lower charge (−)")
        flg = QVBoxLayout(fil_grp)
        note = QLabel(
            "Fires one pulse, waits N read cycles with the filament off (clean "
            "signal), reads, and increments the pulse width until the target — "
            "immune to the filament noise.")
        note.setStyleSheet("color: #9E9E9E; font-size: 11px;")
        note.setWordWrap(True)
        flg.addWidget(note)
        self._ramp_config = FilamentRampConfig()
        # This panel IS the pulse-wait-read loop, so the enable box is implicit.
        self._ramp_config._enable.setChecked(True)
        self._ramp_config._enable.setVisible(False)
        flg.addWidget(self._ramp_config)
        row = QHBoxLayout()
        row.addWidget(QLabel("Lower by:"))
        self._fil_delta = self._spin(0.0, 1000.0, 1, 0.0, " e")
        self._fil_delta.setToolTip("Lower the charge by this many e, then stop.\n"
                                   "0 = ramp until you press Cancel (or timeout).")
        row.addWidget(self._fil_delta)
        row.addStretch()
        self._fil_btn = QPushButton("Ramp filament (lower −)")
        self._fil_btn.setStyleSheet("background-color: #4CAF50; color: white;")
        self._fil_btn.clicked.connect(self._on_filament)
        row.addWidget(self._fil_btn)
        flg.addLayout(row)
        flg.addWidget(QLabel("Cycle history — pulse width · Δq added since last read:"))
        self._ramp_log = CycleLog()
        flg.addWidget(self._ramp_log)
        outer.addWidget(fil_grp)

        # --- Go to target ---------------------------------------------------
        tgt_grp = QGroupBox("Go to target (auto — picks flash or filament)")
        tg = QGridLayout(tgt_grp)
        tg.addWidget(QLabel("Target:"), 0, 0, Qt.AlignRight)
        self._target_spin = self._spin(-1000.0, 1000.0, 1, 0.0, " e")
        tg.addWidget(self._target_spin, 0, 1)
        tg.addWidget(QLabel("Tolerance:"), 0, 2, Qt.AlignRight)
        self._tol_spin = self._spin(0.1, 100.0, 1, 0.5, " e")
        tg.addWidget(self._tol_spin, 0, 3)
        tg.addWidget(QLabel("Safety timeout:"), 0, 4, Qt.AlignRight)
        self._timeout_spin = self._spin(0.1, 600.0, 1, 10.0, " min")
        self._timeout_spin.setToolTip("Stop if the target isn't reached within this long.")
        tg.addWidget(self._timeout_spin, 0, 5)
        self._target_btn = QPushButton("Go to target")
        self._target_btn.setStyleSheet("background-color: #2196F3; color: white;")
        self._target_btn.clicked.connect(self._on_target)
        tg.addWidget(self._target_btn, 1, 5)
        tg.setColumnStretch(6, 1)
        outer.addWidget(tgt_grp)

        # --- Drive setback (applies to both tools) --------------------------
        sb_grp = QGroupBox("Drive setback while charging (flash or filament)")
        sbg = QGridLayout(sb_grp)
        self._setback_cb = QCheckBox("Reduce the electrode drive while charging")
        self._setback_cb.setToolTip(
            "Park the monitored axis' drive low while flashing/heating so a\n"
            "highly-charged sphere isn't over-driven; restored right after.\n"
            "Charge readings stay normalized, so the reported charge is unchanged.")
        sbg.addWidget(self._setback_cb, 0, 0, 1, 2)
        sbg.addWidget(QLabel("Charging amplitude:"), 1, 0, Qt.AlignRight)
        self._setback_amp = self._spin(0.001, 20.0, 3, 0.100, " Vpp")
        sbg.addWidget(self._setback_amp, 1, 1)
        sbg.setColumnStretch(2, 1)
        outer.addWidget(sb_grp)

        # --- Status + Cancel ------------------------------------------------
        status_row = QHBoxLayout()
        self._status = QLabel("Idle")
        self._status.setStyleSheet("color: gray; font-size: 14px; font-weight: bold;")
        self._status.setWordWrap(True)
        status_row.addWidget(self._status, 1)
        self._cancel_btn = QPushButton("Cancel / Stop")
        self._cancel_btn.setStyleSheet("background-color: #F44336; color: white;")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._on_cancel)
        status_row.addWidget(self._cancel_btn)
        outer.addLayout(status_row)

        # --- Flash diagnostics ----------------------------------------------
        flash_diag = QGroupBox("Flash — charge added since last read")
        fdg = QVBoxLayout(flash_diag)
        self._flash_log = CycleLog(empty="(no flashes yet)")
        fdg.addWidget(self._flash_log)
        outer.addWidget(flash_diag)

        # --- Event log ------------------------------------------------------
        log_grp = QGroupBox("Event log")
        lg = QVBoxLayout(log_grp)
        self._event_log_text = QTextEdit()
        self._event_log_text.setReadOnly(True)
        self._event_log_text.setMaximumHeight(120)
        lg.addWidget(self._event_log_text)
        outer.addWidget(log_grp)

        outer.addStretch()

    # ------------------------------------------------------------------
    # Run controls
    # ------------------------------------------------------------------

    def _apply_common(self):
        """Settings applied on every run (safety timeout)."""
        self._ctrl.set_timeout(self._timeout_spin.value() * 60.0)

    def _on_flash(self):
        self._apply_common()
        self._ctrl.set_policy("flash")
        self._ctrl.set_flash_params(rate_hz=self._flash_rate.value(),
                                    control_v=self._flash_ctrl.value())
        delta = self._flash_delta.value()
        self._ctrl.set_relative_target(delta if delta > 0 else 1e6, tolerance=0.5)
        self._begin()

    def _on_filament(self):
        self._apply_common()
        self._ctrl.set_policy("filament")
        self._ctrl.set_filament_ramp(self._ramp_config.get_ramp())
        delta = self._fil_delta.value()
        self._ctrl.set_relative_target(-(delta if delta > 0 else 1e6), tolerance=0.5)
        self._begin()

    def _on_target(self):
        self._apply_common()
        self._ctrl.set_policy("auto")
        self._ctrl.set_target(self._target_spin.value(), self._tol_spin.value())
        self._ctrl.set_flash_params(rate_hz=self._flash_rate.value(),
                                    control_v=self._flash_ctrl.value())
        self._ctrl.set_filament_ramp(self._ramp_config.get_ramp())
        self._begin()

    def _begin(self):
        self._ramp_log.clear()
        self._flash_log.clear()
        for b in (self._flash_btn, self._fil_btn, self._target_btn):
            b.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._ctrl.start()

    def _on_cancel(self):
        self._ctrl.cancel()

    def _on_stopped(self, reason: str):
        for b in (self._flash_btn, self._fil_btn, self._target_btn):
            b.setEnabled(True)
        self._cancel_btn.setEnabled(False)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _on_action_changed(self, msg: str):
        self._status.setText(msg)
        low = msg.lower()
        if any(k in low for k in ("not connected", "error", "can't", "overshot")):
            color = "#C62828"
        elif "reached target" in low:
            color = "#2E7D32"
        elif any(k in low for k in ("timeout", "cancel", "stopped")):
            color = "gray"
        elif "flashing" in low:
            color = "#EF6C00"
        elif "filament" in low or "ramping" in low:
            color = "#6A1B9A"
        else:
            color = "#1565C0"
        self._status.setStyleSheet(f"color: {color}; font-size: 14px; font-weight: bold;")

    def _on_event_logged(self, event: ControlEvent):
        ts = time.strftime("%H:%M:%S", time.localtime(event.timestamp))
        self._event_log_text.append(
            f"[{ts}] {event.action.value:9s}  charge={event.charge_e:+.1f}  "
            f"target={event.target_e:+.1f}  {event.detail}")

    def _on_cycle_logged(self, cyc):
        if getattr(cyc, "device", "filament") == "flash":
            self._flash_log.add(cyc)
        else:
            self._ramp_log.add(cyc)

    # ------------------------------------------------------------------
    # Drive-setback params (read by DriveSetbackAdapter)
    # ------------------------------------------------------------------

    def get_setback_params(self) -> dict:
        """Drive-setback settings, read by DriveSetbackAdapter at charging time."""
        return {
            "enabled": self._setback_cb.isChecked(),
            "charging_vpp": self._setback_amp.value(),
        }

    # ------------------------------------------------------------------
    # Config save / restore
    # ------------------------------------------------------------------

    def get_config(self) -> dict:
        cfg = self._ctrl.get_config()
        cfg["_gui_flash_rate"] = self._flash_rate.value()
        cfg["_gui_flash_ctrl"] = self._flash_ctrl.value()
        cfg["_gui_flash_delta"] = self._flash_delta.value()
        cfg["_gui_fil_delta"] = self._fil_delta.value()
        cfg["_gui_target"] = self._target_spin.value()
        cfg["_gui_tolerance"] = self._tol_spin.value()
        cfg["_gui_timeout_min"] = self._timeout_spin.value()
        cfg["_gui_setback_enabled"] = self._setback_cb.isChecked()
        cfg["_gui_setback_vpp"] = self._setback_amp.value()
        cfg["_gui_filament_ramp"] = self._ramp_config.get_config()
        return cfg

    def restore_config(self, cfg: dict):
        self._ctrl.restore_config(cfg)
        setters = {
            "_gui_flash_rate": self._flash_rate,
            "_gui_flash_ctrl": self._flash_ctrl,
            "_gui_flash_delta": self._flash_delta,
            "_gui_fil_delta": self._fil_delta,
            "_gui_target": self._target_spin,
            "_gui_tolerance": self._tol_spin,
            "_gui_timeout_min": self._timeout_spin,
            "_gui_setback_vpp": self._setback_amp,
        }
        for key, spin in setters.items():
            if key in cfg:
                spin.setValue(float(cfg[key]))
        if "_gui_setback_enabled" in cfg:
            self._setback_cb.setChecked(bool(cfg["_gui_setback_enabled"]))
        if "_gui_filament_ramp" in cfg:
            self._ramp_config.restore_config(cfg["_gui_filament_ramp"])
            self._ramp_config._enable.setChecked(True)


# ======================================================================
# CalibrationTab — calibration workflow
# ======================================================================

class CalibrationTab(QWidget):
    """
    GUI for running and managing calibrations.

    Workflows:
        File-based    — select a directory of known-charge H5 files, run checkQ
                        calibration
        Lock-in auto  — enter |charge| and polarity, press Start: samples the
                        running lock-in source until the standard error of the
                        mean X voltage reaches the target (or the sample cap),
                        then computes and saves volts-per-electron
        Lock-in manual — enter a measured voltage at a known charge directly

    For the auto workflow, connect the AnalysisTab stream to
    ``on_charge_update`` and ``lockin_cal_saved`` back to
    ``AnalysisTab.set_volts_per_electron`` (done in charge_gui.py).
    """

    # Emitted after a successful lock-in calibration:
    # (volts_per_electron, drive_amplitude_vpp, kind) where kind is 'sr530'/'esp32'.
    lockin_cal_saved = pyqtSignal(float, float, str)

    # SEM criterion may only trigger after this many samples.
    _AUTO_MIN_SAMPLES = 100

    def __init__(self, parent=None):
        super().__init__(parent)
        self._auto_active = False
        self._auto_n = 0
        self._auto_mean = 0.0
        self._auto_m2 = 0.0
        self._auto_kind = ""
        self._auto_snap: dict = {}
        # Optional callable() -> current electrode drive amplitude (Vpp), set by
        # the ChargeWidget so calibrations record the drive amplitude in use.
        self._drive_amp_provider = None
        self._build_ui()

    def _current_drive_amp(self) -> float:
        """Current drive amplitude (Vpp), or 0.0 if unknown."""
        if self._drive_amp_provider is None:
            return 0.0
        try:
            v = self._drive_amp_provider()
            return float(v) if v and v > 0 else 0.0
        except Exception:
            return 0.0

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(8)
        outer.setContentsMargins(6, 6, 6, 6)

        # --- Sphere parameters ---
        sphere_grp = QGroupBox("Sphere parameters")
        sg = QGridLayout(sphere_grp)

        sg.addWidget(QLabel("Sphere diameter (µm):"), 0, 0)
        self._diam_edit = QLineEdit("10.0")
        self._diam_edit.setMaximumWidth(100)
        sg.addWidget(self._diam_edit, 0, 1)

        sg.addWidget(QLabel("Drive frequency (Hz):"), 0, 2)
        self._freq_edit = QLineEdit("100.0")
        self._freq_edit.setMaximumWidth(100)
        sg.addWidget(self._freq_edit, 0, 3)
        sg.setColumnStretch(4, 1)
        outer.addWidget(sphere_grp)

        # --- File-based calibration ---
        file_grp = QGroupBox("File-based calibration (checkQ)")
        fg = QGridLayout(file_grp)

        fg.addWidget(QLabel("Data directory:"), 0, 0)
        self._cal_dir_edit = QLineEdit()
        fg.addWidget(self._cal_dir_edit, 0, 1)
        browse_dir = QPushButton("Browse…")
        browse_dir.setMaximumWidth(80)
        browse_dir.clicked.connect(self._browse_cal_dir)
        fg.addWidget(browse_dir, 0, 2)

        fg.addWidget(QLabel("Calibration file:"), 1, 0)
        self._cal_file_edit = QLineEdit()
        from charge_calibration import DEFAULT_CAL_FILE
        self._cal_file_edit.setText(DEFAULT_CAL_FILE)
        fg.addWidget(self._cal_file_edit, 1, 1)
        browse_file = QPushButton("Browse…")
        browse_file.setMaximumWidth(80)
        browse_file.clicked.connect(self._browse_cal_file)
        fg.addWidget(browse_file, 1, 2)

        fg.addWidget(QLabel("Known polarity:"), 2, 0)
        self._polarity_combo = QComboBox()
        self._polarity_combo.addItems(["positive", "negative"])
        self._polarity_combo.setMaximumWidth(120)
        fg.addWidget(self._polarity_combo, 2, 1)

        fg.addWidget(QLabel("Known # charges:"), 3, 0)
        self._n_charges_spin = QSpinBox()
        self._n_charges_spin.setRange(1, 100)
        self._n_charges_spin.setValue(1)
        self._n_charges_spin.setMaximumWidth(80)
        fg.addWidget(self._n_charges_spin, 3, 1)

        fg.addWidget(QLabel("Position channel (ai):"), 4, 0)
        self._pos_ch = QSpinBox()
        self._pos_ch.setRange(0, 31)
        self._pos_ch.setValue(0)
        self._pos_ch.setMaximumWidth(80)
        fg.addWidget(self._pos_ch, 4, 1)

        fg.addWidget(QLabel("Drive channel (ai):"), 5, 0)
        self._drive_ch = QSpinBox()
        self._drive_ch.setRange(0, 31)
        self._drive_ch.setValue(10)
        self._drive_ch.setMaximumWidth(80)
        fg.addWidget(self._drive_ch, 5, 1)

        run_file_btn = QPushButton("Run file-based calibration")
        run_file_btn.setMinimumWidth(200)
        run_file_btn.clicked.connect(self._on_run_file_cal)
        fg.addWidget(run_file_btn, 6, 0, 1, 3)

        self._file_cal_status = QLabel("—")
        self._file_cal_status.setWordWrap(True)
        fg.addWidget(self._file_cal_status, 7, 0, 1, 3)
        fg.setColumnStretch(1, 1)
        outer.addWidget(file_grp)

        # --- Lock-in calibration (auto) ---
        auto_grp = QGroupBox("Lock-in calibration — auto (sample the live source)")
        ag = QGridLayout(auto_grp)

        ag.addWidget(QLabel("Known charge |e|:"), 0, 0)
        self._auto_charge_spin = QSpinBox()
        self._auto_charge_spin.setRange(1, 1000)
        self._auto_charge_spin.setValue(1)
        self._auto_charge_spin.setMaximumWidth(80)
        self._auto_charge_spin.setToolTip(
            "Magnitude of the known charge on the sphere, in electrons."
        )
        ag.addWidget(self._auto_charge_spin, 0, 1)

        ag.addWidget(QLabel("Polarity:"), 0, 2)
        self._auto_polarity_combo = QComboBox()
        self._auto_polarity_combo.addItems(["positive", "negative"])
        self._auto_polarity_combo.setMaximumWidth(100)
        ag.addWidget(self._auto_polarity_combo, 0, 3)

        ag.addWidget(QLabel("Target SEM (%):"), 1, 0)
        self._auto_target_spin = QDoubleSpinBox()
        self._auto_target_spin.setRange(0.01, 50.0)
        self._auto_target_spin.setDecimals(2)
        self._auto_target_spin.setValue(1.0)
        self._auto_target_spin.setMaximumWidth(80)
        self._auto_target_spin.setToolTip(
            "Stop when the standard error of the mean X voltage falls below\n"
            "this fraction of the mean (needs at least "
            f"{self._AUTO_MIN_SAMPLES} samples)."
        )
        ag.addWidget(self._auto_target_spin, 1, 1)

        ag.addWidget(QLabel("Max samples:"), 1, 2)
        self._auto_max_spin = QSpinBox()
        self._auto_max_spin.setRange(self._AUTO_MIN_SAMPLES, 1_000_000)
        self._auto_max_spin.setValue(10_000)
        self._auto_max_spin.setMaximumWidth(100)
        self._auto_max_spin.setToolTip(
            "Hard cap: finish with whatever precision has been reached\n"
            "after this many samples."
        )
        ag.addWidget(self._auto_max_spin, 1, 3)

        self._auto_btn = QPushButton("Start auto-calibration")
        self._auto_btn.setMinimumWidth(180)
        self._auto_btn.clicked.connect(self._on_auto_cal_clicked)
        ag.addWidget(self._auto_btn, 2, 0, 1, 2)

        self._auto_status = QLabel(
            "Requires a running lock-in source in the Analysis tab."
        )
        self._auto_status.setWordWrap(True)
        ag.addWidget(self._auto_status, 3, 0, 1, 4)
        ag.setColumnStretch(4, 1)
        outer.addWidget(auto_grp)

        # --- Lock-in calibration (manual) ---
        li_grp = QGroupBox("Lock-in calibration — manual (known voltage)")
        lg = QGridLayout(li_grp)

        lg.addWidget(QLabel("Measured voltage (V):"), 0, 0)
        self._li_voltage = QLineEdit("0.001")
        self._li_voltage.setMaximumWidth(120)
        lg.addWidget(self._li_voltage, 0, 1)

        lg.addWidget(QLabel("Known charge (e):"), 0, 2)
        self._li_charge = QSpinBox()
        self._li_charge.setRange(-100, 100)
        self._li_charge.setValue(1)
        self._li_charge.setMaximumWidth(80)
        lg.addWidget(self._li_charge, 0, 3)

        run_li_btn = QPushButton("Calibrate lock-in")
        run_li_btn.setMaximumWidth(140)
        run_li_btn.clicked.connect(self._on_run_lockin_cal)
        lg.addWidget(run_li_btn, 1, 0)

        self._li_cal_status = QLabel("—")
        self._li_cal_status.setWordWrap(True)
        lg.addWidget(self._li_cal_status, 1, 1, 1, 3)
        lg.setColumnStretch(1, 1)
        outer.addWidget(li_grp)

        # --- Existing calibrations ---
        list_grp = QGroupBox("Stored calibrations")
        listg = QVBoxLayout(list_grp)
        self._cal_list_text = QTextEdit()
        self._cal_list_text.setReadOnly(True)
        self._cal_list_text.setMaximumHeight(120)
        listg.addWidget(self._cal_list_text)
        refresh_btn = QPushButton("Refresh list")
        refresh_btn.setMaximumWidth(100)
        refresh_btn.clicked.connect(self._refresh_cal_list)
        listg.addWidget(refresh_btn)
        outer.addWidget(list_grp)

        outer.addStretch()

        # Initial list
        self._refresh_cal_list()

    # ------------------------------------------------------------------
    # Browse dialogs
    # ------------------------------------------------------------------

    def _browse_cal_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select calibration data directory")
        if d:
            self._cal_dir_edit.setText(d)

    def _browse_cal_file(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Select calibration JSON", "", "JSON (*.json);;All (*)"
        )
        if f:
            self._cal_file_edit.setText(f)

    # ------------------------------------------------------------------
    # Run calibrations
    # ------------------------------------------------------------------

    def _on_run_file_cal(self):
        import os
        data_dir = self._cal_dir_edit.text().strip()
        if not data_dir or not os.path.isdir(data_dir):
            self._file_cal_status.setText("Invalid data directory")
            self._file_cal_status.setStyleSheet("color: red;")
            return

        try:
            diam = float(self._diam_edit.text())
        except ValueError:
            self._file_cal_status.setText("Invalid sphere diameter")
            self._file_cal_status.setStyleSheet("color: red;")
            return

        self._file_cal_status.setText("Running calibration…")
        self._file_cal_status.setStyleSheet("color: #2196F3;")

        # Run in background to avoid freezing GUI
        from PyQt5.QtCore import QThread

        class _CalThread(QThread):
            from PyQt5.QtCore import pyqtSignal as _sig
            done = _sig(bool, str)

            def __init__(self, **kw):
                super().__init__()
                self._kw = kw

            def run(self):
                try:
                    from charge_calibration import run_file_calibration
                    cal = run_file_calibration(**self._kw)
                    msg = (
                        f"Saved: d={cal['sphere_diameter_um']}µm, "
                        f"f={cal['drive_frequency_hz']:.1f}Hz, "
                        f"corr/e={cal['correlation_per_electron']:.4e}, "
                        f"pos/e={cal['position_response_per_electron']:.4e}"
                    )
                    self.done.emit(True, msg)
                except Exception as e:
                    self.done.emit(False, f"{type(e).__name__}: {e}")

        self._cal_thread = _CalThread(
            data_dir=data_dir,
            sphere_diameter_um=diam,
            calibration_file=self._cal_file_edit.text().strip(),
            position_channel=self._pos_ch.value(),
            drive_channel=self._drive_ch.value(),
            polarity=self._polarity_combo.currentText(),
            n_charges=self._n_charges_spin.value(),
        )

        def _on_done(ok, msg):
            self._file_cal_status.setText(msg)
            self._file_cal_status.setStyleSheet(
                "color: green;" if ok else "color: red;"
            )
            self._refresh_cal_list()

        self._cal_thread.done.connect(_on_done)
        self._cal_thread.start()

    # ------------------------------------------------------------------
    # Auto lock-in calibration
    # ------------------------------------------------------------------

    def on_charge_update(self, result: dict):
        """
        Receives every measurement from the Analysis tab stream.

        While an auto-calibration is active, accumulates the raw lock-in X
        voltage (Welford mean/variance) and finishes when the SEM criterion
        or the sample cap is reached.  Ignores results with no raw_voltage
        (i.e. the file-based source).
        """
        if not self._auto_active or "raw_voltage" not in result:
            return

        v = float(result["raw_voltage"])
        self._auto_n += 1
        delta = v - self._auto_mean
        self._auto_mean += delta / self._auto_n
        self._auto_m2 += delta * (v - self._auto_mean)
        self._auto_kind = (
            "sr530" if "sr530_sensitivity" in result else "esp32"
        )
        if isinstance(result.get("raw"), dict):
            self._auto_snap = result["raw"]

        n = self._auto_n
        sem_frac = self._auto_sem_frac()
        if n % 10 == 0 or n <= 10:
            sem_pct = sem_frac * 100.0
            self._auto_status.setText(
                f"Sampling… n={n}   mean={self._auto_mean:.4e} V   "
                f"SEM={'—' if math.isinf(sem_pct) else f'{sem_pct:.2f}%'}"
            )

        target = self._auto_target_spin.value() / 100.0
        if ((n >= self._AUTO_MIN_SAMPLES and sem_frac <= target)
                or n >= self._auto_max_spin.value()):
            self._finish_auto_cal()

    def _auto_sem_frac(self) -> float:
        """Standard error of the mean as a fraction of |mean|."""
        n = self._auto_n
        if n < 2 or self._auto_mean == 0.0:
            return float("inf")
        sem = math.sqrt(self._auto_m2 / (n - 1) / n)
        return sem / abs(self._auto_mean)

    def _on_auto_cal_clicked(self):
        if self._auto_active:
            self._set_auto_active(False)
            self._auto_status.setText(
                f"Cancelled after {self._auto_n} samples — nothing saved."
            )
            self._auto_status.setStyleSheet("color: gray;")
            return

        try:
            float(self._diam_edit.text())
            float(self._freq_edit.text())
        except ValueError:
            self._auto_status.setText("Invalid sphere diameter / drive frequency")
            self._auto_status.setStyleSheet("color: red;")
            return

        self._auto_n = 0
        self._auto_mean = 0.0
        self._auto_m2 = 0.0
        self._auto_kind = ""
        self._auto_snap = {}
        self._set_auto_active(True)
        self._auto_status.setText(
            "Sampling… waiting for lock-in data (if this stays at 0, start a "
            "lock-in source in the Analysis tab)."
        )
        self._auto_status.setStyleSheet("color: #2196F3;")

    def _set_auto_active(self, active: bool):
        self._auto_active = active
        self._auto_btn.setText(
            "Stop (cancel)" if active else "Start auto-calibration"
        )
        for w in (self._auto_charge_spin, self._auto_polarity_combo,
                  self._auto_target_spin, self._auto_max_spin):
            w.setEnabled(not active)

    def _finish_auto_cal(self):
        self._set_auto_active(False)
        n = self._auto_n
        mean_v = self._auto_mean
        sem_pct = self._auto_sem_frac() * 100.0

        sign = 1 if self._auto_polarity_combo.currentText() == "positive" else -1
        charge = sign * self._auto_charge_spin.value()

        if mean_v == 0.0:
            self._auto_status.setText("Mean voltage is exactly 0 — nothing saved.")
            self._auto_status.setStyleSheet("color: red;")
            return

        # NOTE: a negative volts-per-electron (X sign opposite to the charge
        # polarity) is a VALID calibration — the sign just records the arbitrary
        # lock-in phase alignment.  It is stored signed and the sources apply it
        # correctly; we do not reject it.

        drive_amp = self._current_drive_amp()
        try:
            from charge_calibration import calibrate_lockin_from_voltage
            cal = calibrate_lockin_from_voltage(
                measured_voltage=mean_v,
                known_charge=charge,
                sphere_diameter_um=float(self._diam_edit.text()),
                drive_frequency_hz=float(self._freq_edit.text()),
                calibration_file=self._cal_file_edit.text().strip(),
                drive_amplitude_vpp=drive_amp,
                sr530_sensitivity_idx=int(
                    self._auto_snap.get("sensitivity_idx", -1)),
                sr530_phase=float(self._auto_snap.get("phase", 0.0)),
            )
            vpe = cal["volts_per_electron"]
            amp_txt = (f", drive={drive_amp:.4g} Vpp" if drive_amp > 0
                       else " (drive amp unknown)")
            self._auto_status.setText(
                f"Saved: {vpe:.6e} V/e from n={n} samples "
                f"(mean={mean_v:.4e} V, SEM={sem_pct:.2f}%, "
                f"charge={charge:+d}e{amp_txt}) — Analysis tab updated."
            )
            self._auto_status.setStyleSheet("color: green;")
            self._refresh_cal_list()
            self.lockin_cal_saved.emit(vpe, drive_amp, self._auto_kind or "sr530")
        except Exception as e:
            self._auto_status.setText(f"{type(e).__name__}: {e}")
            self._auto_status.setStyleSheet("color: red;")

    def _on_run_lockin_cal(self):
        try:
            voltage = float(self._li_voltage.text())
            charge = self._li_charge.value()
            diam = float(self._diam_edit.text())
            freq = float(self._freq_edit.text())
        except ValueError:
            self._li_cal_status.setText("Invalid input values")
            self._li_cal_status.setStyleSheet("color: red;")
            return

        if charge == 0:
            self._li_cal_status.setText("Charge must be nonzero")
            self._li_cal_status.setStyleSheet("color: red;")
            return

        drive_amp = self._current_drive_amp()
        try:
            from charge_calibration import calibrate_lockin_from_voltage
            cal = calibrate_lockin_from_voltage(
                measured_voltage=voltage,
                known_charge=charge,
                sphere_diameter_um=diam,
                drive_frequency_hz=freq,
                calibration_file=self._cal_file_edit.text().strip(),
                drive_amplitude_vpp=drive_amp,
            )
            vpe = cal["volts_per_electron"]
            amp_txt = f"  drive={drive_amp:.4g}Vpp" if drive_amp > 0 else ""
            self._li_cal_status.setText(
                f"Saved: {vpe:.6f} V/e  (d={diam}µm, f={freq}Hz){amp_txt}"
            )
            self._li_cal_status.setStyleSheet("color: green;")
            self._refresh_cal_list()
            # Manual cal also feeds the Analysis tab (kind: assume SR530 direct)
            self.lockin_cal_saved.emit(vpe, drive_amp, "sr530")
        except Exception as e:
            self._li_cal_status.setText(f"{type(e).__name__}: {e}")
            self._li_cal_status.setStyleSheet("color: red;")

    # ------------------------------------------------------------------
    # Display stored calibrations
    # ------------------------------------------------------------------

    def _refresh_cal_list(self):
        try:
            from charge_calibration import CalibrationStore
            store = CalibrationStore(self._cal_file_edit.text().strip())
            lines = []
            for c in store.list_file_cals():
                lines.append(
                    f"[File] d={c['sphere_diameter_um']}µm  "
                    f"f={c['drive_frequency_hz']:.1f}Hz  "
                    f"corr/e={c['correlation_per_electron']:.4e}  "
                    f"date={c.get('calibration_date', '?')}"
                )
            for c in store.list_lockin_cals():
                lines.append(
                    f"[Lock-in] d={c['sphere_diameter_um']}µm  "
                    f"f={c['drive_frequency_hz']:.1f}Hz  "
                    f"V/e={c['volts_per_electron']:.6f}  "
                    f"date={c.get('calibration_date', '?')}"
                )
            self._cal_list_text.setPlainText(
                "\n".join(lines) if lines else "No calibrations found"
            )
        except Exception as e:
            self._cal_list_text.setPlainText(f"Error: {e}")

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def get_config(self) -> dict:
        return {
            "sphere_diameter_um": self._diam_edit.text(),
            "drive_frequency_hz": self._freq_edit.text(),
            "cal_data_dir": self._cal_dir_edit.text(),
            "cal_file": self._cal_file_edit.text(),
            "polarity": self._polarity_combo.currentText(),
            "n_charges": self._n_charges_spin.value(),
            "position_channel": self._pos_ch.value(),
            "drive_channel": self._drive_ch.value(),
            "auto_charge_e": self._auto_charge_spin.value(),
            "auto_polarity": self._auto_polarity_combo.currentText(),
            "auto_target_pct": self._auto_target_spin.value(),
            "auto_max_samples": self._auto_max_spin.value(),
        }

    def restore_config(self, cfg: dict):
        if "sphere_diameter_um" in cfg:
            self._diam_edit.setText(str(cfg["sphere_diameter_um"]))
        if "drive_frequency_hz" in cfg:
            self._freq_edit.setText(str(cfg["drive_frequency_hz"]))
        if "cal_data_dir" in cfg:
            self._cal_dir_edit.setText(str(cfg["cal_data_dir"]))
        if "cal_file" in cfg:
            self._cal_file_edit.setText(str(cfg["cal_file"]))
        if "polarity" in cfg:
            idx = self._polarity_combo.findText(str(cfg["polarity"]))
            if idx >= 0:
                self._polarity_combo.setCurrentIndex(idx)
        if "n_charges" in cfg:
            self._n_charges_spin.setValue(int(cfg["n_charges"]))
        if "position_channel" in cfg:
            self._pos_ch.setValue(int(cfg["position_channel"]))
        if "drive_channel" in cfg:
            self._drive_ch.setValue(int(cfg["drive_channel"]))
        if "auto_charge_e" in cfg:
            self._auto_charge_spin.setValue(int(cfg["auto_charge_e"]))
        if "auto_polarity" in cfg:
            idx = self._auto_polarity_combo.findText(str(cfg["auto_polarity"]))
            if idx >= 0:
                self._auto_polarity_combo.setCurrentIndex(idx)
        if "auto_target_pct" in cfg:
            self._auto_target_spin.setValue(float(cfg["auto_target_pct"]))
        if "auto_max_samples" in cfg:
            self._auto_max_spin.setValue(int(cfg["auto_max_samples"]))


# ======================================================================
# ExperimentTab — photon order experiment
# ======================================================================

class ExperimentTab(QWidget):
    """
    GUI for the photon-order experiment.

    Wraps a PhotonOrderExperiment instance.  The experiment's actuators
    come from the Connections tab; charge measurements come from the
    Analysis tab's source.

    Displays a live 2-D heatmap of mean_changes_per_flash(rate, voltage).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._experiment = None
        self._heatmap_data = None       # 2-D numpy array (or list-of-lists)
        self._plot_widget = None        # pyqtgraph ImageItem
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(8)
        outer.setContentsMargins(6, 6, 6, 6)

        # --- Scan parameters ---
        scan_grp = QGroupBox("Scan parameters")
        sg = QGridLayout(scan_grp)

        sg.addWidget(QLabel("Flash rates (Hz), comma-separated:"), 0, 0)
        self._rates_edit = QLineEdit("1, 2, 5, 10, 20, 50")
        sg.addWidget(self._rates_edit, 0, 1)

        sg.addWidget(QLabel("Electrode voltages (V), comma-separated:"), 1, 0)
        self._voltages_edit = QLineEdit("50, 100, 150, 200, 250")
        sg.addWidget(self._voltages_edit, 1, 1)

        sg.setColumnStretch(1, 1)
        outer.addWidget(scan_grp)

        # --- Collection parameters ---
        collect_grp = QGroupBox("Collection parameters")
        cg = QGridLayout(collect_grp)

        cg.addWidget(QLabel("Min events per point:"), 0, 0)
        self._min_events_spin = QSpinBox()
        self._min_events_spin.setRange(5, 10000)
        self._min_events_spin.setValue(50)
        self._min_events_spin.setMaximumWidth(100)
        cg.addWidget(self._min_events_spin, 0, 1)

        cg.addWidget(QLabel("Max flashes per point:"), 0, 2)
        self._max_flashes_spin = QSpinBox()
        self._max_flashes_spin.setRange(100, 1000000)
        self._max_flashes_spin.setValue(10000)
        self._max_flashes_spin.setMaximumWidth(100)
        cg.addWidget(self._max_flashes_spin, 0, 3)

        cg.addWidget(QLabel("Charge limit (e):"), 1, 0)
        self._charge_limit_spin = QDoubleSpinBox()
        self._charge_limit_spin.setRange(1.0, 50.0)
        self._charge_limit_spin.setValue(5.0)
        self._charge_limit_spin.setDecimals(1)
        self._charge_limit_spin.setMaximumWidth(100)
        cg.addWidget(self._charge_limit_spin, 1, 1)

        cg.addWidget(QLabel("Detection threshold (e):"), 1, 2)
        self._det_thresh_spin = QDoubleSpinBox()
        self._det_thresh_spin.setRange(0.1, 5.0)
        self._det_thresh_spin.setValue(0.4)
        self._det_thresh_spin.setDecimals(2)
        self._det_thresh_spin.setMaximumWidth(100)
        cg.addWidget(self._det_thresh_spin, 1, 3)

        cg.addWidget(QLabel("Settle time (s):"), 2, 0)
        self._settle_spin = QDoubleSpinBox()
        self._settle_spin.setRange(0.5, 30.0)
        self._settle_spin.setValue(3.0)
        self._settle_spin.setDecimals(1)
        self._settle_spin.setMaximumWidth(100)
        cg.addWidget(self._settle_spin, 2, 1)

        cg.setColumnStretch(1, 1)
        cg.setColumnStretch(3, 1)
        outer.addWidget(collect_grp)

        # --- Start / Stop / Progress ---
        ctrl_row = QHBoxLayout()
        self._start_btn = QPushButton("Start experiment")
        self._start_btn.setMinimumWidth(150)
        self._start_btn.setStyleSheet("background-color: #4CAF50; color: white;")
        self._start_btn.clicked.connect(self._on_start)
        ctrl_row.addWidget(self._start_btn)

        self._abort_btn = QPushButton("Abort")
        self._abort_btn.setMinimumWidth(80)
        self._abort_btn.setEnabled(False)
        self._abort_btn.setStyleSheet("background-color: #F44336; color: white;")
        self._abort_btn.clicked.connect(self._on_abort)
        ctrl_row.addWidget(self._abort_btn)

        self._save_btn = QPushButton("Save result…")
        self._save_btn.setMaximumWidth(100)
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._on_save)
        ctrl_row.addWidget(self._save_btn)

        ctrl_row.addStretch()

        self._status_lbl = QLabel("Idle")
        self._status_lbl.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: gray;"
        )
        ctrl_row.addWidget(self._status_lbl)
        outer.addLayout(ctrl_row)

        # --- Progress ---
        self._progress_lbl = QLabel("")
        outer.addWidget(self._progress_lbl)

        # --- 2-D Heatmap ---
        plot_grp = QGroupBox("Mean charge changes per flash — heatmap")
        pg = QVBoxLayout(plot_grp)
        try:
            import pyqtgraph as _pg
            self._pg = _pg
            self._plot_gv = _pg.GraphicsLayoutWidget()
            self._plot_gv.setMinimumHeight(300)
            self._heatmap_plot = self._plot_gv.addPlot(
                title="Mean Δq / flash",
                labels={"bottom": "Flash rate (Hz)", "left": "Electrode voltage (V)"},
            )
            self._heatmap_img = _pg.ImageItem()
            self._heatmap_plot.addItem(self._heatmap_img)
            self._colorbar = None
            pg.addWidget(self._plot_gv)
            self._has_plot = True
        except ImportError:
            self._pg = None
            self._has_plot = False
            lbl = QLabel("Install pyqtgraph for live heatmap: pip install pyqtgraph")
            lbl.setStyleSheet("color: gray;")
            pg.addWidget(lbl)
        outer.addWidget(plot_grp)

        # --- Log ---
        log_grp = QGroupBox("Experiment log")
        lg = QVBoxLayout(log_grp)
        self._log_text = QTextEdit()
        self._log_text.setReadOnly(True)
        self._log_text.setMaximumHeight(150)
        lg.addWidget(self._log_text)
        outer.addWidget(log_grp)

    # ------------------------------------------------------------------
    # Parse helpers
    # ------------------------------------------------------------------

    def _parse_list(self, text: str) -> list[float]:
        parts = [s.strip() for s in text.replace(";", ",").split(",")]
        return [float(p) for p in parts if p]

    # ------------------------------------------------------------------
    # Start / Abort
    # ------------------------------------------------------------------

    def _on_start(self):
        try:
            rates = self._parse_list(self._rates_edit.text())
            voltages = self._parse_list(self._voltages_edit.text())
        except ValueError:
            self._status_lbl.setText("Invalid rate or voltage list")
            self._status_lbl.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: red;"
            )
            return

        if not rates or not voltages:
            self._status_lbl.setText("Rates and voltages must be non-empty")
            self._status_lbl.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: red;"
            )
            return

        # Initialize heatmap data
        import numpy as np
        self._heatmap_data = np.full((len(voltages), len(rates)), np.nan)
        self._rates = rates
        self._voltages = voltages

        if self._has_plot:
            self._heatmap_img.setImage(self._heatmap_data.T)
            self._heatmap_plot.setLabel("bottom", "Flash rate index")
            self._heatmap_plot.setLabel("left", "Voltage index")

        # Get or create experiment
        from photon_order_experiment import PhotonOrderExperiment
        if self._experiment is None:
            self._experiment = PhotonOrderExperiment()
        self._experiment.set_params(
            flash_rates_hz=rates,
            electrode_voltages_v=voltages,
            min_events=self._min_events_spin.value(),
            max_flashes=self._max_flashes_spin.value(),
            charge_limit=self._charge_limit_spin.value(),
            detection_threshold=self._det_thresh_spin.value(),
            settle_time_s=self._settle_spin.value(),
        )

        # Connect signals
        self._experiment.state_changed.connect(self._on_state_changed)
        self._experiment.progress.connect(self._on_progress)
        self._experiment.data_point_ready.connect(self._on_data_point)
        self._experiment.experiment_done.connect(self._on_done)

        self._start_btn.setEnabled(False)
        self._abort_btn.setEnabled(True)
        self._save_btn.setEnabled(False)
        self._log_text.clear()

        try:
            self._experiment.start()
        except RuntimeError as e:
            self._status_lbl.setText(str(e))
            self._status_lbl.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: red;"
            )
            self._start_btn.setEnabled(True)
            self._abort_btn.setEnabled(False)

    def _on_abort(self):
        if self._experiment:
            self._experiment.abort()
        self._start_btn.setEnabled(True)
        self._abort_btn.setEnabled(False)
        self._status_lbl.setText("Aborted")
        self._status_lbl.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #FF9800;"
        )

    def _on_save(self):
        if self._experiment and self._experiment.result:
            path, _ = QFileDialog.getSaveFileName(
                self, "Save experiment result", "", "JSON (*.json);;All (*)"
            )
            if path:
                self._experiment.result.save(path)
                self._log_text.append(f"Saved to {path}")

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _on_state_changed(self, msg: str):
        self._status_lbl.setText(msg)
        color = "gray"
        if "error" in msg.lower():
            color = "red"
        elif "flash" in msg.lower():
            color = "#FF9800"
        elif "reset" in msg.lower():
            color = "#2196F3"
        elif "complete" in msg.lower() or "done" in msg.lower():
            color = "green"
        self._status_lbl.setStyleSheet(
            f"font-size: 14px; font-weight: bold; color: {color};"
        )
        self._log_text.append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def _on_progress(self, current: int, total: int):
        self._progress_lbl.setText(
            f"Progress: {current}/{total} data points"
        )

    def _on_data_point(self, dp):
        """Update heatmap when a new data point arrives."""
        if self._heatmap_data is None:
            return

        # Find indices
        try:
            r_idx = self._rates.index(dp.flash_rate_hz)
            v_idx = self._voltages.index(dp.electrode_voltage_v)
        except ValueError:
            return

        self._heatmap_data[v_idx, r_idx] = dp.mean_changes_per_flash

        if self._has_plot:
            import numpy as np
            display = np.nan_to_num(self._heatmap_data, nan=0.0)
            self._heatmap_img.setImage(display.T)

        self._log_text.append(
            f"  → rate={dp.flash_rate_hz:.1f} Hz, V={dp.electrode_voltage_v:.1f} V: "
            f"{dp.mean_changes_per_flash:.4f} changes/flash "
            f"({dp.total_events} events / {dp.total_flashes} flashes)"
        )

    def _on_done(self, result):
        self._start_btn.setEnabled(True)
        self._abort_btn.setEnabled(False)
        self._save_btn.setEnabled(True)
        self._status_lbl.setText("Experiment complete")
        self._status_lbl.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: green;"
        )

    # ------------------------------------------------------------------
    # Public: set experiment's actuators
    # ------------------------------------------------------------------

    @property
    def experiment(self):
        return self._experiment

    def set_experiment(self, exp):
        self._experiment = exp

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def get_config(self) -> dict:
        return {
            "flash_rates": self._rates_edit.text(),
            "electrode_voltages": self._voltages_edit.text(),
            "min_events": self._min_events_spin.value(),
            "max_flashes": self._max_flashes_spin.value(),
            "charge_limit": self._charge_limit_spin.value(),
            "detection_threshold": self._det_thresh_spin.value(),
            "settle_time_s": self._settle_spin.value(),
        }

    def restore_config(self, cfg: dict):
        if "flash_rates" in cfg:
            self._rates_edit.setText(str(cfg["flash_rates"]))
        if "electrode_voltages" in cfg:
            self._voltages_edit.setText(str(cfg["electrode_voltages"]))
        if "min_events" in cfg:
            self._min_events_spin.setValue(int(cfg["min_events"]))
        if "max_flashes" in cfg:
            self._max_flashes_spin.setValue(int(cfg["max_flashes"]))
        if "charge_limit" in cfg:
            self._charge_limit_spin.setValue(float(cfg["charge_limit"]))
        if "detection_threshold" in cfg:
            self._det_thresh_spin.setValue(float(cfg["detection_threshold"]))
        if "settle_time_s" in cfg:
            self._settle_spin.setValue(float(cfg["settle_time_s"]))
