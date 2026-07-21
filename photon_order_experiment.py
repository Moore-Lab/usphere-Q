"""
photon_order_experiment.py

Power-sweep / photon-order experiment engine.

Physics
-------
If photoionisation is an n-photon process, the discharge rate scales as:

    Γ ∝ I^n

where I is the flash intensity (set by the flash-lamp control voltage).
At low dose (mean discharges per flash << 1) each flash is a Bernoulli trial
and the number of discharges per flash is Poisson.  The experiment measures
the mean discharge events per flash as a function of (flash control voltage,
flash rate).  A log-log plot of mean rate vs voltage gives the photon order as
the slope.

Protocol (per grid point (flash rate, control voltage))
-------------------------------------------------------
1.  Reset the charge to a set point with the FILAMENT ramp (pulse-wait-read or
    the power ramp) — every step starts from the same charge state.  The
    filament only *lowers* charge (adds electrons → more negative), so the
    reset target is normally at or below the post-flash charge.
2.  Set the flash control voltage + rate; park the electrode drive if the
    per-tool drive setback selects the flash.
3.  Start a fresh DAQ recording run (n_files=0, continuous) named
    ``{root}_{stamp}_V{v}_f{f}``.
4.  Enable the flash lamp; count charge-change events (|Δq| >= detection
    threshold) until ``min_events`` (or ``max_flashes``).
5.  Stop the flash, stop the DAQ run, restore the drive; record the data point.
6.  Advance to the next grid point.  Output: a 2-D array
    [n_voltages × n_rates] of mean_changes_per_flash.

Safeties: a lock-in overload (debounced) stops the sweep if enabled; an
optional per-step |charge| limit ends a step early; the reset ramp is bounded
(stops at its max width/voltage) and time-limited.

GUI-independent: all logic lives here.  The GUI (or a headless script) creates
a PhotonOrderExperiment, wires actuators / a DAQ recorder / a drive callback,
connects signals, and calls start().
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

# --- ZMQ DAQ client (degrades to unavailable off the lab machine) -----------
try:
    import os as _os
    import sys as _sys
    _ZMQ_BASE = _os.path.dirname(__file__)
    if _ZMQ_BASE not in _sys.path:
        _sys.path.insert(0, _ZMQ_BASE)
    from zmq_base import ModuleClient as _ModuleClient
    _ZMQ_AVAILABLE = True
except Exception:
    _ModuleClient = None
    _ZMQ_AVAILABLE = False


# ---------------------------------------------------------------------------
# DAQ recorder — per-step continuous recording over ZMQ
# ---------------------------------------------------------------------------

class DAQRecorder:
    """Thin wrapper over the DAQ ZMQ ModuleClient for per-step *continuous*
    recording (n_files=0): ``start(basename)`` begins a run that records until
    ``stop()``.  One run per sweep step so each file set is named for its
    (voltage, frequency).  Degrades to unavailable (start() returns False) when
    zmq / the DAQ server is absent, so the engine can run headless."""

    def __init__(self, host: str = "localhost", rep_port: int = 5552,
                 output_dir: str = "", sample_rate: float = 10000.0,
                 n_bits: int = 17, timeout_ms: int = 8000):
        self._host = host
        self._rep_port = int(rep_port)
        self._output_dir = output_dir
        self._sample_rate = float(sample_rate)
        self._n_bits = int(n_bits)
        self._timeout_ms = int(timeout_ms)
        self._client = None
        self._recording = False
        self._last_basename: str | None = None

    @property
    def available(self) -> bool:
        return _ZMQ_AVAILABLE

    @property
    def recording(self) -> bool:
        return self._recording

    def ping(self) -> bool:
        c = self._client_or_none()
        if c is None:
            return False
        try:
            return bool(c.ping())
        except Exception:
            return False

    def _client_or_none(self):
        if not _ZMQ_AVAILABLE:
            return None
        if self._client is None:
            self._client = _ModuleClient(
                "daq", rep_port=self._rep_port, pub_port=self._rep_port + 1,
                host=self._host, timeout_ms=self._timeout_ms)
        return self._client

    def start(self, basename: str) -> tuple[bool, str]:
        """Begin a continuous recording run.  Returns (ok, message)."""
        c = self._client_or_none()
        if c is None:
            return False, "zmq not available — cannot record"
        kwargs: dict = dict(n_files=0, basename=basename,
                            sample_rate=self._sample_rate, n_bits=self._n_bits)
        if self._output_dir:
            kwargs["output_dir"] = self._output_dir
        try:
            resp = c.send("start_recording", **kwargs)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            msg = resp.get("message", resp) if isinstance(resp, dict) else resp
            return False, f"DAQ refused: {msg}"
        self._recording = True
        self._last_basename = basename
        return True, basename

    def stop(self) -> None:
        """Stop the current run (safe to call when not recording)."""
        if not self._recording:
            return
        self._recording = False
        c = self._client_or_none()
        if c is None:
            return
        try:
            c.send("stop_recording")
        except Exception:
            pass

    def close(self) -> None:
        self.stop()
        try:
            if self._client is not None:
                self._client.close()
        except Exception:
            pass
        self._client = None


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class ExperimentState(enum.Enum):
    IDLE = "idle"
    FLASHING = "flashing"
    RESETTING = "resetting"        # filament ramp bringing charge to the set point
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
    basename: str = ""             # DAQ file basename for this point (if recorded)


@dataclass
class ExperimentResult:
    """Full experiment result."""
    flash_rates_hz: list[float] = field(default_factory=list)
    electrode_voltages_v: list[float] = field(default_factory=list)
    data: list[list[DataPoint]] = field(default_factory=list)
    stamp: str = ""
    # data[v_idx][r_idx] = DataPoint

    def to_dict(self) -> dict:
        return {
            "flash_rates_hz": self.flash_rates_hz,
            "electrode_voltages_v": self.electrode_voltages_v,
            "stamp": self.stamp,
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
                        "basename": dp.basename,
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
    State-machine sweep engine (see module docstring for the protocol).

    Signals
    -------
    state_changed(str)        — human-readable status
    progress(int, int)        — (current_pair_index, total_pairs)
    data_point_ready(object)  — DataPoint for the just-completed pair
    experiment_done(object)   — ExperimentResult when finished
    log_msg(str)              — detailed log line (ramp reads, DAQ, warnings)
    """

    state_changed = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    data_point_ready = pyqtSignal(object)
    experiment_done = pyqtSignal(object)
    log_msg = pyqtSignal(str)

    def __init__(
        self,
        flashlamp=None,
        filament=None,
        flash_rates_hz: list[float] | None = None,
        electrode_voltages_v: list[float] | None = None,
        min_events: int = 50,
        max_flashes: int = 10000,
        charge_limit: float = 0.0,
        reset_target: float = 0.0,
        reset_tolerance: float = 0.5,
        detection_threshold: float = 0.4,
        settle_time_s: float = 2.0,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._flashlamp = flashlamp
        self._filament = filament          # normally the shared DriveSetbackAdapter

        self._flash_rates = list(flash_rates_hz or [1, 2, 5, 10, 20])
        self._voltages = list(electrode_voltages_v or [50, 100, 150, 200, 250])

        self._min_events = min_events
        self._max_flashes = max_flashes
        self._charge_limit = charge_limit          # 0 = disabled
        self._reset_target = reset_target
        self._reset_tolerance = reset_tolerance
        self._detection_threshold = detection_threshold
        self._settle_time_s = settle_time_s

        # Reset-ramp config (defaults; overridden via set_filament_ramp).
        from charge_control import FilamentRamp
        self._filament_ramp = FilamentRamp(enabled=True)
        self._cycle_period_s = 0.5                 # worker read-cycle spacing
        self._reset_timeout_s = 120.0

        # Drive / setback / recording (wired by the GUI).  The nominal drive is
        # applied by the tab on the GUI thread *before* start() (so the setback
        # reduces from it); the engine only records it for the log.
        self._nominal_drive_vpp = 0.0
        self._setback_params: Optional[dict] = None
        self._recorder: Optional[DAQRecorder] = None
        self._recording_root = "psweep"

        # Overload safety.
        self._stop_on_overload = True
        self._overload_count = 0
        self._overload_stop = False
        self._terminal_emitted = False

        # State
        self._state = ExperimentState.IDLE
        self._result = ExperimentResult()
        self._thread: Optional[_ExperimentThread] = None

        # Live state (updated by the thread, read by GUI)
        self._v_idx = 0
        self._r_idx = 0
        self._current_charge: float = 0.0
        self._have_charge = False
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
        reset_target=None,
        reset_tolerance=None,
        cycle_period_s=None,
        reset_timeout_s=None,
        stop_on_overload=None,
        nominal_drive_vpp=None,
        recording_root=None,
        setback_params=None,
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
        if reset_target is not None:
            self._reset_target = reset_target
        if reset_tolerance is not None:
            self._reset_tolerance = reset_tolerance
        if cycle_period_s is not None:
            self._cycle_period_s = cycle_period_s
        if reset_timeout_s is not None:
            self._reset_timeout_s = reset_timeout_s
        if stop_on_overload is not None:
            self._stop_on_overload = bool(stop_on_overload)
        if nominal_drive_vpp is not None:
            self._nominal_drive_vpp = nominal_drive_vpp
        if recording_root is not None:
            self._recording_root = recording_root
        if setback_params is not None:
            self._setback_params = dict(setback_params)

    def set_filament_ramp(self, ramp) -> None:
        """The FilamentRamp used to reset the charge before each step."""
        self._filament_ramp = ramp

    def set_recorder(self, recorder: Optional[DAQRecorder]) -> None:
        """Attach a DAQ recorder (None = don't record)."""
        self._recorder = recorder

    @property
    def state(self) -> ExperimentState:
        return self._state

    @property
    def result(self) -> ExperimentResult:
        return self._result

    @property
    def is_running(self) -> bool:
        # A live worker thread counts as running even in the ABORTED state, so
        # a new start() can't spawn a second worker over one that hasn't torn
        # down yet.
        if self._thread is not None and self._thread.isRunning():
            return True
        return self._state in (
            ExperimentState.FLASHING,
            ExperimentState.RESETTING,
            ExperimentState.SETTLING,
            ExperimentState.COMPUTING,
            ExperimentState.ADVANCING,
        )

    # ------------------------------------------------------------------
    # Charge update callback (GUI thread) — stores latest + overload watch
    # ------------------------------------------------------------------

    def on_charge_update(self, result: dict) -> None:
        # Drive-setback settle gate (mirrors ChargeController).  A drive-
        # amplitude change saturates the lock-in for a few seconds; those reads
        # are meaningless AND overloaded.  Without this gate the overload
        # watcher would trip on the very transient that _wait_drive_settled()
        # is blocking to wait out, aborting the sweep before any data.  Keep the
        # last good charge rather than storing the transient.
        if self._settle_remaining() > 0.0:
            self._overload_count = 0
            return
        charge = result.get("charge_e")
        if charge is not None:
            self._current_charge = charge
            self._have_charge = True
        # Debounced lock-in overload stop (mirrors ChargeController).
        if self._stop_on_overload and self.is_running:
            if result.get("sr530_overloaded"):
                self._overload_count += 1
                if self._overload_count >= 2:
                    self._overload_stop = True
            else:
                self._overload_count = 0

    def _settle_remaining(self) -> float:
        """Seconds left of the shared drive-setback settle (0 if none/absent)."""
        fn = getattr(self._filament, "settle_remaining", None)
        if fn is None:
            return 0.0
        try:
            return float(fn())
        except Exception:
            return 0.0

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self.is_running:
            # is_running is True while the previous worker thread is still
            # alive (even ABORTED), so this refuses a second concurrent worker.
            raise RuntimeError("A sweep is already running (or still stopping)")
        if self._flashlamp is None:
            raise RuntimeError("No flashlamp controller attached")
        if self._filament is None:
            raise RuntimeError("No filament controller attached")

        self._overload_count = 0
        self._overload_stop = False
        self._terminal_emitted = False
        self._result = ExperimentResult(
            flash_rates_hz=list(self._flash_rates),
            electrode_voltages_v=list(self._voltages),
            stamp=time.strftime("%Y%m%d_%H%M%S"),
            data=[],
        )

        self._thread = _ExperimentThread(self)
        self._thread.start()

    def abort(self) -> None:
        # Signal the worker; its loops check _should_stop() between short,
        # interruptible sleep slices, so it exits promptly.  Wait long enough to
        # cover a blocking DAQ call (~8 s) so we don't return (and let the UI
        # re-arm) while the worker is still alive.  The worker's own finally
        # runs _cleanup() before it exits; this trailing _cleanup() is a
        # best-effort backstop if the wait times out (idempotent).
        if self._thread is not None and self._thread.isRunning():
            self._state = ExperimentState.ABORTED
            if not self._thread.wait(15000):
                log.error("Sweep worker did not stop within 15 s")
        self._cleanup()

    # ------------------------------------------------------------------
    # Internals (called from the worker thread)
    # ------------------------------------------------------------------

    _TERMINAL = (ExperimentState.DONE, ExperimentState.ERROR,
                 ExperimentState.ABORTED)

    def _set_state(self, state: ExperimentState, msg: str = ""):
        self._state = state
        if state in self._TERMINAL:
            self._terminal_emitted = True
        display = msg or state.value
        log.info("Experiment state: %s", display)
        self.state_changed.emit(display)

    def _should_stop(self) -> bool:
        return self._state == ExperimentState.ABORTED or self._overload_stop

    def _cleanup(self):
        """Best-effort return to a safe idle state (idempotent).  Called at the
        end of every run (worker finally + abort), so it closes the DAQ client
        too — one recorder is built per sweep, so leaving it open would leak a
        socket each run."""
        if self._recorder is not None:
            try:
                self._recorder.close()
            except Exception:
                pass
        try:
            self._flashlamp.disable()
        except Exception:
            pass
        for name in ("pulse_off", "disable"):
            fn = getattr(self._filament, name, None)
            if fn:
                try:
                    fn()
                except Exception:
                    pass
        # Session off + restore the drive to the measurement setpoint.
        for obj in (self._flashlamp, self._filament):
            fn = getattr(obj, "disarm", None)
            if fn:
                try:
                    fn()
                except Exception:
                    pass
        for name in ("restore", "clear_params_source"):
            fn = getattr(self._filament, name, None)
            if fn:
                try:
                    fn()
                except Exception:
                    pass

    def _sleep_cycles(self, seconds: float) -> bool:
        """Sleep in short slices, returning False if a stop is requested."""
        t0 = time.time()
        while time.time() - t0 < seconds:
            if self._should_stop():
                return False
            time.sleep(min(0.05, seconds))
        return not self._should_stop()

    def _run_experiment(self):
        """Main sweep loop — runs in a worker thread."""
        total_pairs = len(self._voltages) * len(self._flash_rates)
        pair_idx = 0
        try:
            if self._nominal_drive_vpp > 0:
                self.log_msg.emit(
                    f"Nominal drive {self._nominal_drive_vpp:g} Vpp "
                    f"(setback reduces from it)")
            # Source setback params from THIS experiment for the shared adapter.
            if self._setback_params is not None:
                fn = getattr(self._filament, "set_params_source", None)
                if fn:
                    fn(lambda: self._setback_params)
            # Session on (NGE flash-control + filament-power outputs).
            for obj in (self._flashlamp, self._filament):
                arm = getattr(obj, "arm", None)
                if arm:
                    try:
                        arm()
                    except Exception:
                        pass

            for v_idx, voltage in enumerate(self._voltages):
                self._v_idx = v_idx
                row: list[DataPoint] = []
                for r_idx, rate in enumerate(self._flash_rates):
                    if self._should_stop():
                        return
                    self._r_idx = r_idx
                    pair_idx += 1
                    self.progress.emit(pair_idx, total_pairs)

                    dp = self._run_single_point(rate, voltage)
                    row.append(dp)
                    self.data_point_ready.emit(dp)
                    log.info(
                        "Point %d/%d: rate=%.3g Hz, V=%.3g V → "
                        "%.4f changes/flash  (%.4f changes/s)",
                        pair_idx, total_pairs, rate, voltage,
                        dp.mean_changes_per_flash, dp.mean_changes_per_second)
                    if self._should_stop():
                        self._result.data.append(row)
                        return
                self._result.data.append(row)

            self._set_state(ExperimentState.DONE, "Experiment complete")
            self.experiment_done.emit(self._result)
        finally:
            self._cleanup()
            # Always emit a terminal state so the GUI leaves the running state
            # on every exit.  Skip if a terminal was already emitted (normal
            # DONE above, or the DAQ-failure path's specific ABORTED message).
            if not self._terminal_emitted:
                if self._overload_stop:
                    self._set_state(ExperimentState.ERROR,
                                    "Lock-in OVERLOAD — sweep stopped")
                else:
                    self._set_state(ExperimentState.ABORTED, "Sweep stopped")

    def _run_single_point(self, rate_hz: float, voltage_v: float) -> DataPoint:
        """Reset the charge, then flash + count events for one grid point."""
        dp = DataPoint(flash_rate_hz=rate_hz, electrode_voltage_v=voltage_v)

        # --- 1. Reset the charge to the set point with the filament ramp ---
        self._reset_charge()
        if self._should_stop():
            return dp

        # --- 2. Program the flash, park the drive for the flash ---
        self._set_state(
            ExperimentState.ADVANCING,
            f"Setting rate={rate_hz:.3g} Hz, V={voltage_v:.3g} V")
        # The reset left the drive parked (filament pulses park it and never
        # restore); un-park first so _park_flash() re-establishes the drive per
        # the per-tool 'flash' gate.  Without this, "Filament only" would leave
        # the drive parked low through the whole flash-count + DAQ recording.
        self._restore_drive()
        if not self._wait_drive_settled():   # restoring the drive rings too
            return dp
        try:
            self._flashlamp.set_flash_rate(rate_hz)
            self._flashlamp.set_electrode_voltage(voltage_v)
        except Exception as e:
            self.log_msg.emit(f"Flash set error: {e}")
        self._park_flash()
        if not self._wait_drive_settled():   # parking kicks the biggest spike
            self._restore_drive()
            return dp
        if not self._sleep_cycles(0.2):      # let the instrument settle
            self._restore_drive()
            return dp

        # --- 3. Start a fresh DAQ run for this grid point ---
        basename = self._basename(rate_hz, voltage_v)
        if self._recorder is not None:
            ok, msg = self._recorder.start(basename)
            if ok:
                dp.basename = basename
                self.log_msg.emit(f"  DAQ recording → {msg}")
            else:
                # Requested recording but it failed → stop rather than silently
                # collecting no data.  Route through _set_state so the GUI gets
                # a terminal state_changed (not just a buried log line).
                self.log_msg.emit(f"  DAQ start FAILED: {msg} — stopping sweep")
                self._restore_drive()
                self._set_state(ExperimentState.ABORTED,
                                f"DAQ start failed — sweep stopped ({msg})")
                return dp

        # --- 4. Flash and count charge-change events ---
        self._set_state(
            ExperimentState.FLASHING,
            f"Flashing: rate={rate_hz:.3g} Hz, V={voltage_v:.3g} V  "
            f"(0/{self._min_events} events)")
        try:
            self._flashlamp.enable()
        except Exception as e:
            self.log_msg.emit(f"Flash enable error: {e}")

        self._last_charge = self._current_charge
        self._flash_count = 0
        self._event_count = 0
        self._flash_start_time = time.time()
        # The lamp flashes autonomously at rate_hz; we poll once per flash
        # period to read the charge.  Use the TRUE period (no low clamp) so one
        # poll == one flash and flash_count stays honest at sub-0.1 Hz rates;
        # the sleep is interruptible so an abort/overload is honored promptly.
        flash_period = 1.0 / max(rate_hz, 1e-6)

        while self._event_count < self._min_events:
            if self._should_stop():
                break
            if not self._sleep_cycles(flash_period):
                break
            self._flash_count += 1

            current = self._current_charge
            if self._last_charge is not None:
                if abs(current - self._last_charge) >= self._detection_threshold:
                    self._event_count += 1
                    self._set_state(
                        ExperimentState.FLASHING,
                        f"Flashing: rate={rate_hz:.3g} Hz, V={voltage_v:.3g} V  "
                        f"({self._event_count}/{self._min_events} events, "
                        f"{self._flash_count} flashes)")
            self._last_charge = current

            # Per-step runaway safety (0 = disabled): end the step early.
            if self._charge_limit > 0 and abs(current) > self._charge_limit:
                self.log_msg.emit(
                    f"  |charge| {current:+.2f} e exceeded limit "
                    f"{self._charge_limit:g} e — ending step early")
                break

            if self._flash_count >= self._max_flashes:
                log.warning("Max flashes (%d) reached with only %d events",
                            self._max_flashes, self._event_count)
                self.log_msg.emit(
                    f"  Max flashes {self._max_flashes} reached "
                    f"({self._event_count} events)")
                break

        # --- 5. Stop the flash + DAQ, restore the drive, compute result ---
        try:
            self._flashlamp.disable()
        except Exception:
            pass
        if self._recorder is not None and dp.basename:
            self._recorder.stop()
        self._restore_drive()
        elapsed = time.time() - self._flash_start_time

        dp.total_flashes = self._flash_count
        dp.total_events = self._event_count
        dp.flash_duration_s = elapsed
        if self._flash_count > 0:
            dp.mean_changes_per_flash = self._event_count / self._flash_count
        if elapsed > 0:
            dp.mean_changes_per_second = self._event_count / elapsed
        return dp

    # ------------------------------------------------------------------
    # Reset-before-each-step: drive the filament ramp to the set point
    # ------------------------------------------------------------------

    def _reset_charge(self):
        """Bring the charge to the reset set point with the filament ramp
        (pulse-wait-read or the power ramp).  The filament only lowers the
        charge, so the stop condition is ``q <= target + tolerance`` — if the
        sphere is already at/below the target the ramp exits immediately."""
        from charge_control import PulseRampRunner, PowerRampRunner

        self._set_state(ExperimentState.RESETTING,
                        f"Resetting charge to {self._reset_target:+.3g} e")
        ramp = self._filament_ramp
        target, tol = self._reset_target, self._reset_tolerance
        cond = lambda q: q <= target + tol       # noqa: E731

        # Park the drive and wait out the transient BEFORE any heating starts.
        # Doing it first (rather than as a side effect of the first heat action)
        # means the filament is never on during the blind settle window —
        # mirrors ChargeController._start_pulse_ramp's park-then-settle-then-heat
        # ordering.
        self._park_filament()
        if not self._wait_drive_settled():
            return

        # Power mode: hold the SSR closed + configure EasyRamp up front.
        if getattr(ramp, "mode", "pulse") == "power":
            if not hasattr(self._filament, "hold_ssr_on"):
                self.log_msg.emit("  Filament has no power-ramp support — reset skipped")
                return
            try:
                if getattr(ramp, "easyramp_ms", 0) > 0 and hasattr(
                        self._filament, "set_power_easyramp"):
                    self._filament.set_power_easyramp(ramp.easyramp_ms, True)
                # Program the ramp's START voltage BEFORE closing the SSR: the
                # supply is still sitting at the previous grid point's ceiling
                # (nothing lowers it between points), so closing first would
                # heat the filament at max_v until the runner's first step.
                self._filament.set_power_voltage(ramp.start_v)
                self._filament.hold_ssr_on()
            except Exception as e:
                self.log_msg.emit(f"  Filament error: {e}")
                return
            if not self._wait_drive_settled():
                return
            runner = PowerRampRunner(ramp, condition=cond)
        else:
            if not hasattr(self._filament, "fire_pulse"):
                self.log_msg.emit("  Filament has no pulse support — reset skipped")
                return
            runner = PulseRampRunner(ramp, condition=cond)

        t0 = time.time()
        reason = "timeout"
        while not self._should_stop():
            if time.time() - t0 >= self._reset_timeout_s:
                reason = "reset timeout"
                break
            if not self._have_charge:
                if not self._sleep_cycles(self._cycle_period_s):
                    return
                continue
            q = self._current_charge
            try:
                r = runner.step(q)
                if r.get("off"):
                    self._filament.pulse_off()
                if r.get("fire") is not None:
                    self._filament.fire_pulse(r["fire"])
                if r.get("set_voltage") is not None:
                    self._filament.set_power_voltage(r["set_voltage"])
            except Exception as e:
                self.log_msg.emit(f"  Filament error: {e}")
                break
            # The first pulse-mode fire parks the drive; hold off reading until
            # the transient has decayed (no-op once settled).
            if not self._wait_drive_settled():
                return
            c = r.get("cycle")
            if c is not None:
                self.log_msg.emit(
                    f"  reset {c.width_ms:g} {c.unit}  Δq={c.delta_q:+.2f} e  "
                    f"q={c.charge:+.2f} e")
            if r.get("done"):
                reason = r.get("reason", "done")
                break
            if not self._sleep_cycles(self._cycle_period_s):
                return

        # Stop heating; keep the drive parked through the brief settle.
        try:
            self._filament.pulse_off()
        except Exception:
            pass
        self.log_msg.emit(
            f"  reset → {reason} at q={self._current_charge:+.2f} e "
            f"({time.time() - t0:.1f}s)")

        self._set_state(ExperimentState.SETTLING, "Settling after reset")
        self._sleep_cycles(self._settle_time_s)

    # ------------------------------------------------------------------
    # Drive-setback helpers (per-tool; guarded for plain fakes)
    # ------------------------------------------------------------------

    def _park_flash(self):
        fn = getattr(self._filament, "park", None)
        if fn:
            try:
                fn("flash")
            except Exception:
                pass

    def _restore_drive(self):
        fn = getattr(self._filament, "restore", None)
        if fn:
            try:
                fn()
            except Exception:
                pass

    def _wait_drive_settled(self) -> bool:
        """Block (interruptibly) until the drive-setback settle has elapsed.
        Changing the drive amplitude kicks a large transient into the lock-in,
        so no reading may be acted on until it has decayed.  Returns False if a
        stop was requested meanwhile."""
        while True:
            remaining = self._settle_remaining()
            if remaining <= 0.0:
                return not self._should_stop()
            if not self._sleep_cycles(min(0.2, remaining)):
                return False

    def _park_filament(self):
        fn = getattr(self._filament, "park", None)
        if fn:
            try:
                fn("filament")
            except Exception:
                pass

    def _basename(self, rate_hz: float, voltage_v: float) -> str:
        return (f"{self._recording_root}_{self._result.stamp}"
                f"_V{voltage_v:g}_f{rate_hz:g}")


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
                ExperimentState.ERROR, f"Error: {type(e).__name__}: {e}")
            self._exp._cleanup()
