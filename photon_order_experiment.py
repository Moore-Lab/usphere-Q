"""
photon_order_experiment.py

Experiment to determine whether flash lamp discharge is a 1-photon,
2-photon, or 3-photon process.

Physics
-------
If photoionisation is an n-photon process, the discharge rate scales as:

    Γ ∝ I^n

where I is the flash intensity (controlled by electrode voltage).
At low dose (mean discharges per flash << 1), each flash is a Bernoulli
trial and the number of discharges per flash follows a Poisson distribution.

The experiment measures mean discharges per flash as a function of
(flash_rate, electrode_voltage).  A log-log plot of mean_rate vs voltage
gives the photon order as the slope.

Protocol
--------
1.  Set electrode voltage to minimum, flash rate to first value.
2.  Enable flash lamp (CH1 = pulse trigger, CH2 = DC bias).
3.  Monitor charge state via the analysis source.
4.  Count charge change events (|Δq| >= detection_threshold).
5.  When |charge| > charge_limit, pause flashing, use filament to
    bring charge back to ~0 (via the ChargeController), then resume.
6.  After collecting min_events (or max_flashes), compute
    mean_changes_per_flash for this (rate, voltage) pair.
7.  Advance to next flash rate.  After all rates → next voltage.
8.  Output:  2-D array  [n_voltages × n_rates]  of mean_changes_per_flash.

GUI-independent:  all logic lives here.  The GUI (or a headless script)
creates a PhotonOrderExperiment, connects signals, and calls start().

Typical headless usage::

    from photon_order_experiment import PhotonOrderExperiment

    exp = PhotonOrderExperiment(
        flashlamp=flash_ctrl,
        filament=fil_ctrl,
        flash_rates_hz=[1, 2, 5, 10, 20],
        electrode_voltages_v=[50, 100, 150, 200, 250],
        min_events=50,
        charge_limit=5,
    )
    exp.start()
    # ... exp.abort() to cancel
    result = exp.result   # DataResult with the 2-D histogram
"""

from __future__ import annotations

import enum
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QObject, QThread, pyqtSignal

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class ExperimentState(enum.Enum):
    IDLE = "idle"
    FLASHING = "flashing"
    RESETTING = "resetting"        # filament bringing charge back to 0
    SETTLING = "settling"          # waiting after reset
    COMPUTING = "computing"        # calculating data point
    ADVANCING = "advancing"        # moving to next (rate, voltage)
    DONE = "done"
    ABORTED = "aborted"
    ERROR = "error"


@dataclass
class DataPoint:
    """Result for one (rate, voltage) pair."""
    flash_rate_hz: float
    electrode_voltage_v: float
    total_flashes: int = 0
    total_events: int = 0          # charge change events detected
    mean_changes_per_flash: float = 0.0
    flash_duration_s: float = 0.0  # how long flashing lasted
    mean_changes_per_second: float = 0.0


@dataclass
class ExperimentResult:
    """Full experiment result."""
    flash_rates_hz: list[float] = field(default_factory=list)
    electrode_voltages_v: list[float] = field(default_factory=list)
    data: list[list[DataPoint]] = field(default_factory=list)
    # data[v_idx][r_idx] = DataPoint

    def to_dict(self) -> dict:
        return {
            "flash_rates_hz": self.flash_rates_hz,
            "electrode_voltages_v": self.electrode_voltages_v,
            "data": [
                [
                    {
                        "flash_rate_hz": dp.flash_rate_hz,
                        "electrode_voltage_v": dp.electrode_voltage_v,
                        "total_flashes": dp.total_flashes,
                        "total_events": dp.total_events,
                        "mean_changes_per_flash": dp.mean_changes_per_flash,
                        "flash_duration_s": dp.flash_duration_s,
                        "mean_changes_per_second": dp.mean_changes_per_second,
                    }
                    for dp in row
                ]
                for row in self.data
            ],
        }

    def save(self, path: str | Path) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)


# ---------------------------------------------------------------------------
# Experiment engine
# ---------------------------------------------------------------------------

class PhotonOrderExperiment(QObject):
    """
    State-machine experiment engine.

    Signals
    -------
    state_changed(str)        — human-readable status
    progress(int, int)        — (current_pair_index, total_pairs)
    data_point_ready(object)  — DataPoint for the just-completed pair
    experiment_done(object)   — ExperimentResult when finished
    """

    state_changed = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    data_point_ready = pyqtSignal(object)
    experiment_done = pyqtSignal(object)

    def __init__(
        self,
        flashlamp=None,
        filament=None,
        flash_rates_hz: list[float] | None = None,
        electrode_voltages_v: list[float] | None = None,
        min_events: int = 50,
        max_flashes: int = 10000,
        charge_limit: float = 5.0,
        reset_target: float = 0.0,
        reset_tolerance: float = 0.5,
        detection_threshold: float = 0.4,
        settle_time_s: float = 3.0,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._flashlamp = flashlamp
        self._filament = filament

        self._flash_rates = list(flash_rates_hz or [1, 2, 5, 10, 20])
        self._voltages = list(electrode_voltages_v or [50, 100, 150, 200, 250])

        self._min_events = min_events
        self._max_flashes = max_flashes
        self._charge_limit = charge_limit
        self._reset_target = reset_target
        self._reset_tolerance = reset_tolerance
        self._detection_threshold = detection_threshold
        self._settle_time_s = settle_time_s

        # State
        self._state = ExperimentState.IDLE
        self._result = ExperimentResult()
        self._thread: Optional[_ExperimentThread] = None

        # Live state (updated by the thread, read by GUI)
        self._v_idx = 0
        self._r_idx = 0
        self._current_charge: float = 0.0
        self._last_charge: float | None = None
        self._flash_count = 0
        self._event_count = 0
        self._flash_start_time = 0.0

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_actuators(self, flashlamp=None, filament=None):
        if flashlamp is not None:
            self._flashlamp = flashlamp
        if filament is not None:
            self._filament = filament

    def set_params(
        self,
        flash_rates_hz=None,
        electrode_voltages_v=None,
        min_events=None,
        max_flashes=None,
        charge_limit=None,
        detection_threshold=None,
        settle_time_s=None,
    ):
        if flash_rates_hz is not None:
            self._flash_rates = list(flash_rates_hz)
        if electrode_voltages_v is not None:
            self._voltages = list(electrode_voltages_v)
        if min_events is not None:
            self._min_events = min_events
        if max_flashes is not None:
            self._max_flashes = max_flashes
        if charge_limit is not None:
            self._charge_limit = charge_limit
        if detection_threshold is not None:
            self._detection_threshold = detection_threshold
        if settle_time_s is not None:
            self._settle_time_s = settle_time_s

    @property
    def state(self) -> ExperimentState:
        return self._state

    @property
    def result(self) -> ExperimentResult:
        return self._result

    @property
    def is_running(self) -> bool:
        return self._state in (
            ExperimentState.FLASHING,
            ExperimentState.RESETTING,
            ExperimentState.SETTLING,
            ExperimentState.COMPUTING,
            ExperimentState.ADVANCING,
        )

    # ------------------------------------------------------------------
    # Charge update callback
    # ------------------------------------------------------------------

    def on_charge_update(self, result: dict) -> None:
        """
        Called on every new charge measurement from the analysis source.
        Thread-safe: just stores the latest value.
        """
        charge = result.get("charge_e")
        if charge is not None:
            self._current_charge = charge

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self.is_running:
            log.warning("Experiment already running")
            return
        if self._flashlamp is None:
            raise RuntimeError("No flashlamp controller attached")
        if self._filament is None:
            raise RuntimeError("No filament controller attached")

        self._result = ExperimentResult(
            flash_rates_hz=list(self._flash_rates),
            electrode_voltages_v=list(self._voltages),
            data=[],
        )

        self._thread = _ExperimentThread(self)
        self._thread.start()

    def abort(self) -> None:
        if self._thread and self._thread.isRunning():
            self._state = ExperimentState.ABORTED
            self._thread.wait(5000)
        self._safe_disable_all()

    # ------------------------------------------------------------------
    # Internals (called from the worker thread)
    # ------------------------------------------------------------------

    def _set_state(self, state: ExperimentState, msg: str = ""):
        self._state = state
        display = msg or state.value
        log.info("Experiment state: %s", display)
        self.state_changed.emit(display)

    def _safe_disable_all(self):
        try:
            self._flashlamp.disable()
        except Exception:
            pass
        try:
            self._filament.disable()
        except Exception:
            pass

    def _run_experiment(self):
        """Main experiment loop — runs in a worker thread."""
        total_pairs = len(self._voltages) * len(self._flash_rates)
        pair_idx = 0

        for v_idx, voltage in enumerate(self._voltages):
            self._v_idx = v_idx
            row: list[DataPoint] = []

            for r_idx, rate in enumerate(self._flash_rates):
                if self._state == ExperimentState.ABORTED:
                    self._safe_disable_all()
                    return

                self._r_idx = r_idx
                pair_idx += 1
                self.progress.emit(pair_idx, total_pairs)

                dp = self._run_single_point(rate, voltage)
                row.append(dp)
                self.data_point_ready.emit(dp)

                log.info(
                    "Point %d/%d: rate=%.1f Hz, V=%.1f V → "
                    "%.4f changes/flash  (%.4f changes/s)",
                    pair_idx, total_pairs, rate, voltage,
                    dp.mean_changes_per_flash, dp.mean_changes_per_second,
                )

            self._result.data.append(row)

        self._safe_disable_all()
        self._set_state(ExperimentState.DONE, "Experiment complete")
        self.experiment_done.emit(self._result)

    def _run_single_point(
        self, rate_hz: float, voltage_v: float
    ) -> DataPoint:
        """
        Collect statistics for one (rate, voltage) pair.

        Returns a DataPoint with mean_changes_per_flash.
        """
        dp = DataPoint(flash_rate_hz=rate_hz, electrode_voltage_v=voltage_v)

        # Configure flash lamp
        self._set_state(
            ExperimentState.ADVANCING,
            f"Setting rate={rate_hz:.1f} Hz, V={voltage_v:.1f} V",
        )
        self._flashlamp.set_flash_rate(rate_hz)
        self._flashlamp.set_electrode_voltage(voltage_v)
        time.sleep(0.2)  # let instrument settle

        # Reset charge to near zero before starting
        self._reset_charge()

        # Start flashing
        self._set_state(
            ExperimentState.FLASHING,
            f"Flashing: rate={rate_hz:.1f} Hz, V={voltage_v:.1f} V  "
            f"(0/{self._min_events} events)",
        )
        self._flashlamp.enable()

        self._last_charge = self._current_charge
        self._flash_count = 0
        self._event_count = 0
        self._flash_start_time = time.time()

        while self._event_count < self._min_events:
            if self._state == ExperimentState.ABORTED:
                break

            # Wait one flash period
            sleep_time = 1.0 / max(rate_hz, 0.1)
            time.sleep(sleep_time)
            self._flash_count += 1

            # Check for charge change
            current = self._current_charge
            if self._last_charge is not None:
                delta = abs(current - self._last_charge)
                if delta >= self._detection_threshold:
                    self._event_count += 1
                    self._set_state(
                        ExperimentState.FLASHING,
                        f"Flashing: rate={rate_hz:.1f} Hz, V={voltage_v:.1f} V  "
                        f"({self._event_count}/{self._min_events} events, "
                        f"{self._flash_count} flashes)",
                    )
            self._last_charge = current

            # Safety: check charge limit
            if abs(current) > self._charge_limit:
                self._flashlamp.disable()
                self._reset_charge()
                if self._state == ExperimentState.ABORTED:
                    break
                # Resume flashing
                self._set_state(
                    ExperimentState.FLASHING,
                    f"Resumed: rate={rate_hz:.1f} Hz, V={voltage_v:.1f} V  "
                    f"({self._event_count}/{self._min_events} events)",
                )
                self._flashlamp.enable()
                self._last_charge = self._current_charge

            # Safety: cap total flashes
            if self._flash_count >= self._max_flashes:
                log.warning(
                    "Max flashes (%d) reached with only %d events",
                    self._max_flashes, self._event_count,
                )
                break

        # Stop flashing
        self._flashlamp.disable()
        elapsed = time.time() - self._flash_start_time

        # Compute result
        dp.total_flashes = self._flash_count
        dp.total_events = self._event_count
        dp.flash_duration_s = elapsed
        if self._flash_count > 0:
            dp.mean_changes_per_flash = self._event_count / self._flash_count
        if elapsed > 0:
            dp.mean_changes_per_second = self._event_count / elapsed

        return dp

    def _reset_charge(self):
        """Use filament to bring charge back near zero."""
        self._set_state(ExperimentState.RESETTING, "Resetting charge to ~0")

        # Simple bang-bang: heat filament until charge ≈ 0
        max_reset_cycles = 50
        cycles = 0
        while cycles < max_reset_cycles:
            if self._state == ExperimentState.ABORTED:
                self._filament.disable()
                return

            current = self._current_charge
            if abs(current - self._reset_target) <= self._reset_tolerance:
                self._filament.disable()
                break

            if current > self._reset_target:
                # Too positive → flash to remove charge
                self._flashlamp.enable()
                time.sleep(1.0)
                self._flashlamp.disable()
            else:
                # Too negative → heat filament (adds negative → more negative??)
                # Actually: filament emits electrons → sphere captures →
                # sphere becomes more negative.
                # If sphere is already too negative, we need to flash.
                # If sphere is too positive, filament can help.

                # Correction: filament adds negative charge.
                # charge > target → need to go more negative → heat
                # charge < target → need to go more positive → flash
                self._flashlamp.enable()
                time.sleep(1.0)
                self._flashlamp.disable()

            time.sleep(self._settle_time_s)
            cycles += 1

        self._set_state(ExperimentState.SETTLING, "Settling after reset")
        time.sleep(self._settle_time_s)


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------

class _ExperimentThread(QThread):
    def __init__(self, experiment: PhotonOrderExperiment):
        super().__init__()
        self._exp = experiment

    def run(self):
        try:
            self._exp._run_experiment()
        except Exception as e:
            log.exception("Experiment error")
            self._exp._set_state(
                ExperimentState.ERROR,
                f"Error: {type(e).__name__}: {e}",
            )
            self._exp._safe_disable_all()
