"""
charge_sequence.py

Compound charge-control command sequencer.

A sequence is an ordered list of SeqSteps.  Each step is one action —
discharge (flash lamp), recharge (filament), or wait — that runs until a
signed charge threshold is crossed (or a safety timeout / global charge limit).
The whole list can repeat N times (0 = until stopped), and each step is fully
parameterized so cycles can differ (flash control level, filament frequency,
thresholds, ...).

Actuator control is decoupled behind an actuators object with three methods
(start_discharge / start_recharge / stop_all) plus prepare(); the real
implementation (ChargeSequencerActuators in wg_control_tab) resolves the
hardware handles on the GUI thread at start and programs the AFG/NGE
controllers directly from the worker thread.  A mock actuators object makes the
engine fully headless-testable.

GUI-independent: the engine lives here; the SequencerTab wires it up.
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Optional, Callable

from PyQt5.QtCore import QObject, QThread, pyqtSignal

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step data model
# ---------------------------------------------------------------------------

# Stop-condition comparisons: reported charge (in electrons) vs threshold.
COMPARES = {
    "abs_le": "|charge| ≤",
    "abs_ge": "|charge| ≥",
    "le":     "charge ≤",
    "ge":     "charge ≥",
}


@dataclass
class SeqStep:
    action: str = "discharge"          # "discharge" | "recharge" | "wait"

    # discharge (flash lamp)
    flash_rate_hz: float = 10.0
    flash_ctrl_v: float = 0.0          # NGE flash-control voltage

    # recharge (filament)
    fil_freq_hz: float = 10.0
    fil_width_ms: float = 100.0
    fil_power_v: float = 0.0           # NGE filament power; 0 = leave unchanged

    # stop condition (actuator steps only)
    compare: str = "abs_le"            # key into COMPARES
    threshold_e: float = 1.0
    timeout_s: float = 60.0

    # set electrode drive field (on the monitored axis)
    electrode_amp_vpp: float = 0.5
    electrode_settle_s: float = 0.5

    # wait
    wait_s: float = 1.0

    def satisfied(self, charge: float) -> bool:
        """True when the reported charge meets this step's stop condition."""
        t = self.threshold_e
        if self.compare == "abs_le":
            return abs(charge) <= t
        if self.compare == "abs_ge":
            return abs(charge) >= t
        if self.compare == "le":
            return charge <= t
        if self.compare == "ge":
            return charge >= t
        return False

    def summary(self) -> str:
        if self.action == "wait":
            return f"Wait {self.wait_s:g} s"
        if self.action == "set_electrode":
            return (f"Set electrode drive → {self.electrode_amp_vpp:g} Vpp"
                    f" (settle {self.electrode_settle_s:g} s)")
        cmp = COMPARES.get(self.compare, self.compare)
        if self.action == "discharge":
            return (f"Discharge (flash {self.flash_rate_hz:g} Hz, "
                    f"ctrl {self.flash_ctrl_v:g} V) until {cmp} {self.threshold_e:g} e"
                    f"  [≤{self.timeout_s:g}s]")
        if self.action == "recharge":
            p = f", power {self.fil_power_v:g} V" if self.fil_power_v > 0 else ""
            return (f"Recharge (filament {self.fil_freq_hz:g} Hz, "
                    f"{self.fil_width_ms:g} ms{p}) until {cmp} {self.threshold_e:g} e"
                    f"  [≤{self.timeout_s:g}s]")
        return f"{self.action}?"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SeqStep":
        fields = {f: d[f] for f in cls().__dict__ if f in d}
        return cls(**fields)


class SeqState(enum.Enum):
    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    STOPPED = "stopped"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class ChargeSequencer(QObject):
    """
    Runs a list of SeqSteps, optionally repeating, driving the charge actuators
    until each step's signed-threshold stop condition is met.

    Signals
    -------
    state_changed(str)                 human-readable status
    step_changed(int, int, str)        (step_index, n_steps, summary) — 1-based cycle info in msg
    log_msg(str)                       per-event log line
    sequence_done(bool)                True if finished, False if stopped/error
    """

    state_changed = pyqtSignal(str)
    step_changed = pyqtSignal(int, int, str)
    log_msg = pyqtSignal(str)
    sequence_done = pyqtSignal(bool)
    # Requests the GUI set the monitored-axis drive amplitude (Vpp).  Kept as a
    # signal so the engine stays GUI-agnostic and headless-testable.
    set_electrode_requested = pyqtSignal(float)

    def __init__(self, actuators=None, parent: QObject | None = None):
        super().__init__(parent)
        self._act = actuators
        self._steps: list[SeqStep] = []
        self._repeat = 1              # 0 = until stopped
        self._charge_limit = 30.0     # absolute safety ceiling (e)
        self._poll_s = 0.2
        self._state = SeqState.IDLE
        self._current_charge = 0.0
        self._have_charge = False
        self._thread: Optional[_SeqThread] = None

    # -- configuration --------------------------------------------------------

    def set_actuators(self, actuators):
        self._act = actuators

    def set_steps(self, steps: list[SeqStep]):
        self._steps = list(steps)

    def set_repeat(self, n: int):
        self._repeat = max(0, int(n))

    def set_charge_limit(self, limit: float):
        self._charge_limit = abs(float(limit))

    def set_poll_interval(self, s: float):
        self._poll_s = max(0.02, float(s))

    @property
    def state(self) -> SeqState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state == SeqState.RUNNING

    # -- charge feed ----------------------------------------------------------

    def on_charge_update(self, result: dict) -> None:
        """Store the latest reported charge (electrons).  Thread-safe."""
        q = result.get("charge_e")
        if q is not None:
            self._current_charge = float(q)
            self._have_charge = True

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self.is_running:
            return
        if self._act is None:
            raise RuntimeError("No actuators attached")
        if not self._steps:
            raise RuntimeError("Sequence is empty")
        # Resolve hardware handles on the calling (GUI) thread.
        self._act.prepare()
        self._state = SeqState.RUNNING
        self._thread = _SeqThread(self)
        self._thread.start()

    def stop(self) -> None:
        if self._state == SeqState.RUNNING:
            self._state = SeqState.STOPPED
        if self._thread and self._thread.isRunning():
            self._thread.wait(5000)
        self._safe_stop()

    # -- internals (worker thread) -------------------------------------------

    def _emit_state(self, msg: str):
        self.state_changed.emit(msg)

    def _safe_stop(self):
        try:
            if self._act is not None:
                # Prefer a full quiescent stop (triggers + DC outputs off).
                if hasattr(self._act, "all_off"):
                    self._act.all_off()
                else:
                    self._act.stop_all()
        except Exception:
            pass

    def _sleep(self, seconds: float) -> bool:
        """Sleep in small slices; return False if stopped."""
        end = 0.0
        step = min(self._poll_s, 0.1)
        elapsed = 0.0
        while elapsed < seconds:
            if self._state != SeqState.RUNNING:
                return False
            time.sleep(min(step, seconds - elapsed))
            elapsed += step
        return self._state == SeqState.RUNNING

    def _run(self):
        reps = self._repeat
        n_steps = len(self._steps)
        cycle = 0
        try:
            while self._state == SeqState.RUNNING:
                cycle += 1
                for i, step in enumerate(self._steps):
                    if self._state != SeqState.RUNNING:
                        break
                    rep_txt = f"cycle {cycle}" + (f"/{reps}" if reps else "")
                    self.step_changed.emit(i, n_steps, f"[{rep_txt}] {step.summary()}")
                    self._run_step(step, cycle)
                if reps and cycle >= reps:
                    break
        except Exception as e:
            log.exception("Sequencer error")
            self._state = SeqState.ERROR
            self.log_msg.emit(f"ERROR: {type(e).__name__}: {e}")
        finally:
            self._safe_stop()

        if self._state == SeqState.RUNNING:
            self._state = SeqState.DONE
        ok = self._state == SeqState.DONE
        self._emit_state("Sequence complete" if ok else
                         ("Stopped" if self._state == SeqState.STOPPED else "Error"))
        self.sequence_done.emit(ok)

    def _run_step(self, step: SeqStep, cycle: int):
        if step.action == "wait":
            self.log_msg.emit(f"Wait {step.wait_s:g} s")
            self._sleep(step.wait_s)
            return

        if step.action == "set_electrode":
            # Request the GUI change the drive amplitude (source of truth =
            # the drive spinbox), then settle so the field/normalization update.
            self.set_electrode_requested.emit(step.electrode_amp_vpp)
            self.log_msg.emit(f"Set electrode drive → {step.electrode_amp_vpp:g} Vpp")
            self._sleep(step.electrode_settle_s)
            return

        # Start the actuator
        try:
            if step.action == "discharge":
                self._act.start_discharge(step.flash_rate_hz, step.flash_ctrl_v)
                self.log_msg.emit(
                    f"Discharge: flash {step.flash_rate_hz:g} Hz, "
                    f"ctrl {step.flash_ctrl_v:g} V")
            elif step.action == "recharge":
                self._act.start_recharge(step.fil_freq_hz, step.fil_width_ms,
                                         step.fil_power_v)
                self.log_msg.emit(
                    f"Recharge: filament {step.fil_freq_hz:g} Hz, "
                    f"{step.fil_width_ms:g} ms"
                    + (f", power {step.fil_power_v:g} V" if step.fil_power_v > 0 else ""))
            else:
                self.log_msg.emit(f"Unknown action '{step.action}' — skipped")
                return
        except Exception as e:
            self.log_msg.emit(f"Actuator error: {type(e).__name__}: {e}")
            return

        cmp = COMPARES.get(step.compare, step.compare)
        t0 = time.time()
        reason = "timeout"
        while self._state == SeqState.RUNNING:
            if time.time() - t0 >= step.timeout_s:
                reason = "timeout"
                break
            q = self._current_charge
            # Safety ceiling first
            if self._have_charge and abs(q) >= self._charge_limit:
                reason = f"SAFETY charge limit {self._charge_limit:g} e"
                break
            if self._have_charge and step.satisfied(q):
                reason = f"reached {cmp} {step.threshold_e:g} e"
                break
            time.sleep(self._poll_s)

        # Stop this actuator before moving on
        self._act.stop_all()
        q = self._current_charge
        self.log_msg.emit(
            f"  → stopped ({reason}) at charge={q:+.2f} e"
            f", {time.time() - t0:.1f}s")


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------

class _SeqThread(QThread):
    def __init__(self, seq: ChargeSequencer):
        super().__init__()
        self._seq = seq

    def run(self):
        self._seq._run()
