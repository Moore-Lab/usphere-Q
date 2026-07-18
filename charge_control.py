"""
charge_control.py

Charge state control engine for usphere-charge.

GUI-independent: all logic lives here.  The GUI (or a headless script)
creates a ChargeController, wires up a ChargeStateSource, sets a target (and
policy), and calls start().

Two tools move the charge in opposite directions:
    flash lamp  → removes electrons → charge more POSITIVE (raise +)
    filament    → adds electrons    → charge more NEGATIVE (lower −)

Classes:
    ChargeController    — direction-based controller (flash to raise, filament
                          to lower) with a device policy + safety timeout
    PulseRampRunner     — pulse-wait-read filament ramp state machine
    ControlEvent        — timestamped record of every action taken

Typical usage (headless)::

    from charge_analysis import SR530SerialSource
    from charge_control import ChargeController

    source = SR530SerialSource("COM5", volts_per_electron=0.003)
    source.start()

    ctrl = ChargeController(flashlamp=flash, filament=filament)
    ctrl.set_target(charge_e=-5, tolerance=0.5)     # go to −5 e
    ctrl.set_policy("auto")                          # pick tool by direction
    ctrl.set_timeout(600)                            # 10-min safety stop
    ctrl.start()
    # ... ctrl.stop() / ctrl.cancel() when done
"""

from __future__ import annotations

import enum
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

from PyQt5.QtCore import QObject, QThread, QTimer, Qt, pyqtSignal

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class Action(enum.Enum):
    NONE = "none"
    FLASH = "flash"           # UV flash lamp — removes charge
    HEAT = "heat"             # Filament heating — adds negative charge
    WAIT = "wait"             # Waiting for charge to settle
    AT_TARGET = "at_target"   # Charge is within tolerance


@dataclass
class ControlEvent:
    """Timestamped record of a control action."""
    timestamp: float
    charge_e: float
    target_e: float
    tolerance: float
    action: Action
    detail: str = ""


@dataclass
class ThresholdRule:
    """
    If the measured charge crosses outside [lower, upper],
    command the controller to go to target_charge ± tolerance.

    Kept for backward compatibility (older configs / imports); the current
    Control tab does not use rules.
    """
    lower: float              # lower bound (electrons)
    upper: float              # upper bound (electrons)
    target_charge: float      # target to command when triggered
    tolerance: float = 0.5    # tolerance for the target
    enabled: bool = True
    name: str = ""


@dataclass
class FilamentRamp:
    """
    Pulse-then-wait-then-read filament heating ramp.

    The filament (a WG pulse triggering an SSR) runs away and makes the lock-in
    noisy while it's on, so a fixed setting is hard to control and the threshold
    drifts day to day.  Instead: fire ONE pulse of the current width, then wait
    ``timeout_cycles`` read cycles with the filament OFF (clean signal), read the
    charge, evaluate the stop condition, and if not met increment the pulse
    width and fire again.  Because the read is taken with the filament off, the
    loop is immune to the heating noise.

    Only the pulse WIDTH is ramped (the effective firing rate follows from the
    timeout).  The single pulse is fired hardware-timed via a very-low-frequency
    carrier (one pulse per firing; see ChargeSequencerActuators / FilamentAdapter).
    """
    enabled: bool = False
    start_width_ms: float = 5.0    # SSR minimum pulse ~5 ms
    increment_ms: float = 5.0
    max_width_ms: float = 200.0
    timeout_cycles: int = 6        # read cycles to wait (filament off) before reading


@dataclass
class RampCycle:
    """One diagnostic record: charge change over one read cycle.  For the
    filament ramp: a pulse of ``width_ms`` fired, then the charge read after the
    wait.  For the flash lamp: ``width_ms`` is 0 and ``delta_q`` is the charge
    removed since the previous logged read."""
    width_ms: float
    delta_q: float                 # charge change since the previous read cycle
    charge: float                  # charge at this read
    met: bool                      # did the stop condition trip at this read?
    device: str = "filament"       # "filament" | "flash"


class PulseRampRunner:
    """
    Poll-driven state machine for the pulse-wait-read ramp.  ``step(charge)`` is
    called on every lock-in poll and returns what the caller should do this
    poll: fire a pulse, turn the filament output off, and/or a completed
    RampCycle to log, plus whether the stop condition is met.

    The stop condition is a callable ``condition(charge) -> bool`` evaluated only
    at the clean read (never mid-pulse), so noise during heating can't trip it.
    """

    def __init__(self, ramp: FilamentRamp, condition, now_fn=time.time):
        self.ramp = ramp
        self.condition = condition
        self._now = now_fn
        self.width = ramp.start_width_ms
        self.phase = "fire"        # fire | wait | done
        self.cycle = 0
        self.fire_time = 0.0
        self.turned_off = True
        self.last_read = None
        self.history: "deque[RampCycle]" = deque(maxlen=8)
        self.done = False

    def step(self, charge: float) -> dict:
        """Return {fire: width|None, off: bool, cycle: RampCycle|None, done: bool}."""
        if self.done:
            return {"fire": None, "off": False, "cycle": None, "done": True}
        if self.last_read is None:
            self.last_read = charge

        if self.phase == "fire":
            self.fire_time = self._now()
            self.cycle = 0
            self.turned_off = False
            self.phase = "wait"
            return {"fire": self.width, "off": False, "cycle": None, "done": False}

        # wait phase
        self.cycle += 1
        off = False
        # Turn the output off once the hardware-timed pulse has elapsed (so we
        # never truncate it and no second pulse can arrive).
        if not self.turned_off and (self._now() - self.fire_time) >= self.width / 1000.0:
            self.turned_off = True
            off = True

        if self.cycle < self.ramp.timeout_cycles:
            return {"fire": None, "off": off, "cycle": None, "done": False}

        # timeout reached: read + evaluate (filament is off -> clean)
        if not self.turned_off:
            self.turned_off = True
            off = True
        delta = charge - self.last_read
        self.last_read = charge
        met = bool(self.condition(charge))
        cyc = RampCycle(self.width, delta, charge, met)
        self.history.append(cyc)
        if met:
            self.done = True
            return {"fire": None, "off": True, "cycle": cyc, "done": True,
                    "reason": "met"}
        # Not met: if the width has reached the max, stop (bounded ramp — with
        # start == max this fires exactly one pulse then turns off).  Otherwise
        # step the width up and fire again next poll.
        if self.width >= self.ramp.max_width_ms:
            self.done = True
            return {"fire": None, "off": True, "cycle": cyc, "done": True,
                    "reason": "maxed"}
        self.width = min(self.width + self.ramp.increment_ms, self.ramp.max_width_ms)
        self.phase = "fire"
        return {"fire": None, "off": off, "cycle": cyc, "done": False,
                "reason": None}


# ---------------------------------------------------------------------------
# ChargeController
# ---------------------------------------------------------------------------

class ChargeController(QObject):
    """
    Direction-based charge controller with a device policy.

    Two tools move the charge in opposite directions:
        flash lamp  → removes electrons → charge more POSITIVE (raise +)
        filament    → adds electrons    → charge more NEGATIVE (lower −)

    To reach a target charge (± tolerance) the controller picks the tool by
    direction: charge below target → flash to raise it; charge above target →
    filament to lower it.  The policy can force one tool ("flash"/"filament")
    for a manual "change charge by this much" run, or "auto" to pick by
    direction for a "go to target" run.

    Flash is continuous (on until the target is reached, evaluated every read).
    Filament uses the pulse-wait-read ramp (PulseRampRunner) so reads are taken
    with the filament off.  A safety timeout stops the loop if the target isn't
    reached in time.  While actuating, an optional drive setback parks the
    electrode drive low (protects a highly-charged sphere) for BOTH tools.

    The controller receives charge updates via ``on_charge_update(result)``;
    the GUI/script wires the source's signal to it.

    Actuator protocol:
        flashlamp: enable()/disable(), is_connected, arm()/disarm(),
                   set_flash_rate(hz), set_electrode_voltage(v)
        filament:  fire_pulse(width_ms)/pulse_off(), is_connected,
                   arm()/disarm()
        setback (optional): park()/restore()
    """

    # Signals for GUI
    action_changed = pyqtSignal(str)        # gray status line
    event_logged = pyqtSignal(object)       # ControlEvent
    target_reached = pyqtSignal(float)      # charge when the target is reached
    cycle_logged = pyqtSignal(object)       # RampCycle (device-tagged diagnostics)
    stopped = pyqtSignal(str)               # loop stopped (reason)

    POLICIES = ("auto", "flash", "filament")

    def __init__(
        self,
        flashlamp=None,
        filament=None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._flashlamp = flashlamp
        self._filament = filament
        self._setback = None                       # optional drive-setback

        # Target / policy
        self._mode: str = "target"                 # "target" | "change"
        self._target_charge: float = 0.0
        self._tolerance: float = 0.5
        self._policy: str = "auto"                 # auto | flash | filament
        self._timeout_s: float = 600.0             # safety stop (10 min)
        self._enabled: bool = False

        # "Change charge by this amount" mode: stop when |charge - start| >= amount
        # (sign-independent), so a move past the requested magnitude in EITHER
        # direction ends the run.
        self._change_amount: float = 0.0
        self._start_charge: float | None = None

        # Overload safety: end the loop if the lock-in reading overloads.
        self._stop_on_overload: bool = True
        self._overload_count: int = 0

        # State
        self._current_action = Action.NONE
        self._last_charge: float | None = None
        self._event_log: list[ControlEvent] = []
        self._start_time: float = 0.0

        # Flash lamp (continuous) settings + diagnostics
        self._flash_rate_hz: float = 10.0
        self._flash_ctrl_v: float = 0.0
        self._flash_on: bool = False
        self._flash_ref_charge: float | None = None   # last logged flash charge

        # Filament pulse-wait-read ramp
        self._filament_ramp = FilamentRamp()
        self._pulse_runner: PulseRampRunner | None = None

        # Independent safety watchdog: enforces the timeout even if charge
        # updates stall (e.g. the lock-in source thread dies mid-run), so a
        # continuous flash / parked drive can't be left on forever.
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(1000)
        self._watchdog.timeout.connect(self._check_timeout)

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_target(self, charge_e: float, tolerance: float = 0.5) -> None:
        """Go-to-target mode: reach an absolute target charge (± tolerance)."""
        self._mode = "target"
        self._target_charge = charge_e
        self._tolerance = tolerance
        log.info("Target set: %+.1f e  (±%.1f)", charge_e, tolerance)

    def set_change_by(self, amount_e: float, tool: str) -> None:
        """Change-by-amount mode: run ``tool`` ('flash' or 'filament') until the
        charge has moved by ``|amount_e|`` from where it started — in EITHER
        direction (sign-independent), so a runaway past the requested magnitude
        also stops it.  ``amount_e`` <= 0 means run until cancel."""
        self._mode = "change"
        self._change_amount = abs(float(amount_e))
        self._policy = tool if tool in ("flash", "filament") else "flash"
        self._start_charge = None

    def get_target(self) -> tuple[float, float]:
        """Return (target_charge, tolerance)."""
        return self._target_charge, self._tolerance

    def set_stop_on_overload(self, on: bool) -> None:
        """When on (default), end the loop on a lock-in overload reading."""
        self._stop_on_overload = bool(on)

    def _goal_met(self, charge: float) -> bool:
        """True when the run's goal is reached."""
        if self._mode == "change":
            return (self._start_charge is not None and self._change_amount > 0
                    and abs(charge - self._start_charge) >= self._change_amount)
        return abs(charge - self._target_charge) <= self._tolerance

    def set_policy(self, policy: str) -> None:
        """'auto' picks flash/filament by direction; 'flash' or 'filament'
        forces one tool (manual mode)."""
        self._policy = policy if policy in self.POLICIES else "auto"

    def get_policy(self) -> str:
        return self._policy

    def set_timeout(self, timeout_s: float) -> None:
        """Safety timeout: stop if the target isn't reached within this long."""
        self._timeout_s = max(1.0, float(timeout_s))

    def set_flash_params(self, rate_hz: float | None = None,
                         control_v: float | None = None) -> None:
        """Flash-lamp device settings applied when the flash turns on."""
        if rate_hz is not None:
            self._flash_rate_hz = float(rate_hz)
        if control_v is not None:
            self._flash_ctrl_v = float(control_v)

    def set_actuators(self, flashlamp=None, filament=None) -> None:
        """Attach or replace actuator controllers."""
        if flashlamp is not None:
            self._flashlamp = flashlamp
        if filament is not None:
            self._filament = filament

    def set_drive_setback(self, setback) -> None:
        """Attach a drive-setback object (park()/restore()) applied around both
        flash and filament actuation."""
        self._setback = setback

    def set_filament_ramp(self, ramp: FilamentRamp) -> None:
        """Configure the filament pulse-wait-read ramp (see FilamentRamp)."""
        self._filament_ramp = ramp
        log.info("Filament ramp: enabled=%s start=%.3g inc=%.3g max=%.3g cycles=%d",
                 ramp.enabled, ramp.start_width_ms, ramp.increment_ms,
                 ramp.max_width_ms, ramp.timeout_cycles)

    def get_filament_ramp(self) -> FilamentRamp:
        return self._filament_ramp

    # ------------------------------------------------------------------
    # Enable / Disable
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Enable the control loop and arm both actuators for the session.

        Arming turns the NGE flash-control and filament-power DC outputs on at
        their setpoints so the trigger pulses actually flash/heat; the pulses
        gate the actuation, the DC stays on.
        """
        self._enabled = True
        self._start_time = time.time()
        self._pulse_runner = None
        self._flash_on = False
        self._flash_ref_charge = None
        self._start_charge = None
        self._overload_count = 0
        self._current_action = Action.NONE
        self._arm_actuators(True)
        self._watchdog.start()
        if self._mode == "change":
            if self._change_amount > 0:
                msg = (f"Started — change charge by {self._change_amount:.1f} e "
                       f"({self._policy})")
            else:
                msg = f"Started — {self._policy} until cancel"
        else:
            msg = (f"Started — target {self._target_charge:+.1f} e "
                   f"± {self._tolerance:.1f} ({self._policy})")
        log.info(msg)
        self.action_changed.emit(msg)

    def stop(self, reason: str = "Stopped") -> None:
        """Disable the loop, stop actuating, restore the drive, and disarm."""
        was_enabled = self._enabled
        self._enabled = False
        self._watchdog.stop()
        self._stop_all_actuators()
        self._pulse_runner = None
        self._flash_on = False
        self._restore_setback()
        self._arm_actuators(False)
        self._current_action = Action.NONE
        log.info("Control loop stopped: %s", reason)
        self.action_changed.emit(reason)
        if was_enabled:
            self.stopped.emit(reason)

    def cancel(self) -> None:
        """User cancel — stop and turn the outputs off."""
        self.stop("Cancelled — outputs off")

    def _arm_actuators(self, on: bool) -> None:
        """Arm/disarm the session DC outputs on both actuators (if supported)."""
        method = "arm" if on else "disarm"
        for act in (self._flashlamp, self._filament):
            fn = getattr(act, method, None) if act is not None else None
            if fn is not None:
                try:
                    fn()
                except Exception as e:
                    log.warning("actuator %s() failed: %s", method, e)

    @property
    def is_running(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------
    # Core: charge update callback
    # ------------------------------------------------------------------

    def on_charge_update(self, result: dict) -> None:
        """Main entry — called on every new charge measurement."""
        if not self._enabled:
            return
        charge = result.get("charge_e")
        if charge is None:
            return
        self._last_charge = charge

        # Safety: end the loop on a lock-in overload — an overloaded reading is
        # invalid (e.g. the drive parked low + wrong range → a pinned, bogus
        # charge), so acting on it would run away.  Debounced by 2 reads so a
        # single spike doesn't trip it.
        if self._stop_on_overload and result.get("sr530_overloaded"):
            self._overload_count += 1
            if self._overload_count >= 2:
                self.stop("Lock-in OVERLOAD — stopped (increase the range / "
                          "auto-range)")
                return
        else:
            self._overload_count = 0

        # Record the starting charge for a change-by-amount run.
        if self._mode == "change" and self._start_charge is None:
            self._start_charge = charge

        # The pulse ramp owns the loop while it runs (reads only when the
        # filament is off); only the safety timeout can interrupt it.
        if self._pulse_runner is not None:
            if time.time() - self._start_time >= self._timeout_s:
                self.stop(self._timeout_msg())
                return
            self._run_pulse_ramp(charge)
            return

        # Safety timeout.
        if time.time() - self._start_time >= self._timeout_s:
            self.stop(self._timeout_msg())
            return

        # Goal reached?
        if self._goal_met(charge):
            self._reach_goal(charge)
            return

        # Decide which tool to run.
        dev = self._decide(charge)
        if dev == "flash":
            if self._pulse_runner is not None:
                return
            self._do_flash(charge)
        elif dev == "filament":
            if self._flash_on:
                self._flash_stop()
            self._start_pulse_ramp(charge)
        else:
            # Forced (target-mode) policy can't correct in the needed direction.
            self.stop(
                f"Overshot: {charge:+.1f} e vs target "
                f"{self._target_charge:+.1f} e — {self._policy} can't reverse it")

    def _check_timeout(self):
        """Watchdog slot — stop on timeout even if charge updates have stalled."""
        if self._enabled and time.time() - self._start_time >= self._timeout_s:
            self.stop(self._timeout_msg())

    def _timeout_msg(self) -> str:
        t = self._timeout_s
        span = f"{t / 60.0:.1f} min" if t >= 60 else f"{t:.0f} s"
        if self._mode == "change":
            return (f"Safety timeout — charge didn't move by "
                    f"{self._change_amount:.1f} e in {span}")
        return (f"Safety timeout — target {self._target_charge:+.1f} e not "
                f"reached in {span}")

    # ------------------------------------------------------------------
    # Decision logic
    # ------------------------------------------------------------------

    def _decide(self, charge: float) -> str | None:
        """Return the tool to use: 'flash' (raise +), 'filament' (lower −), or
        None if the policy can't move in the needed direction."""
        if self._mode == "change":
            # Forced tool; the |Δq| goal (either direction) stops the run.
            return self._policy if self._policy in ("flash", "filament") else "flash"
        err = charge - self._target_charge   # >0: too high → lower; <0: too low → raise
        if self._policy == "flash":
            return "flash" if err < 0 else None
        if self._policy == "filament":
            return "filament" if err > 0 else None
        # auto
        if err < 0:
            return "flash"
        if err > 0:
            return "filament"
        return None

    # ------------------------------------------------------------------
    # Flash lamp (continuous)
    # ------------------------------------------------------------------

    def _do_flash(self, charge: float):
        """Continuous flash to raise the charge; evaluated every read."""
        if self._flashlamp is None or not getattr(self._flashlamp, "is_connected", False):
            self.action_changed.emit("Flash lamp not connected!")
            return
        if not self._flash_on:
            self._park_setback()
            # Program the device settings, then turn on.
            for name, arg in (("set_flash_rate", self._flash_rate_hz),
                              ("set_electrode_voltage", self._flash_ctrl_v)):
                fn = getattr(self._flashlamp, name, None)
                if fn is not None:
                    try:
                        fn(arg)
                    except Exception:
                        pass
            try:
                self._flashlamp.enable()
            except Exception as e:
                self.action_changed.emit(f"Flash lamp error: {e}")
                self._restore_setback()
                return
            self._flash_on = True
            self._flash_ref_charge = charge
            self._current_action = Action.FLASH
            self._log_event(charge, Action.FLASH, "flash on")
        else:
            # Continuing — log a flash read once the charge has moved enough.
            ref = self._flash_ref_charge if self._flash_ref_charge is not None else charge
            if abs(charge - ref) >= 0.5:
                cyc = RampCycle(0.0, charge - ref, charge, False, device="flash")
                self._flash_ref_charge = charge
                self.cycle_logged.emit(cyc)
        self.action_changed.emit(f"Flashing (raise +) — charge {charge:+.1f} e")

    def _flash_stop(self):
        """Turn the flash lamp off (leave the loop running)."""
        if self._flash_on:
            try:
                if self._flashlamp is not None and getattr(self._flashlamp, "is_connected", False):
                    self._flashlamp.disable()
            except Exception:
                pass
            self._flash_on = False

    # ------------------------------------------------------------------
    # Filament (pulse-wait-read ramp)
    # ------------------------------------------------------------------

    def _start_pulse_ramp(self, charge: float):
        """Begin the pulse-wait-read filament ramp (lower −)."""
        if self._filament is None or not getattr(self._filament, "is_connected", False):
            self.action_changed.emit("Filament not connected!")
            return
        if not hasattr(self._filament, "fire_pulse"):
            self.action_changed.emit("Filament actuator has no pulse support")
            return
        self._park_setback()
        if self._mode == "change":
            # Stop once the charge has moved by the requested amount (either way).
            self._pulse_runner = PulseRampRunner(
                self._filament_ramp, condition=self._goal_met)
        else:
            tgt, tol = self._target_charge, self._tolerance
            # Filament lowers the charge; stop once it reaches the target band.
            self._pulse_runner = PulseRampRunner(
                self._filament_ramp, condition=lambda q: q <= tgt + tol)
        self._current_action = Action.HEAT
        self.action_changed.emit(
            f"Ramping filament (lower −) — "
            f"start {self._filament_ramp.start_width_ms:.3g} ms, "
            f"{self._filament_ramp.timeout_cycles} cycles/read")

    def _run_pulse_ramp(self, charge: float):
        """Advance the active pulse-wait-read runner by one poll and carry out
        its requested actions (fire a pulse / turn the output off / log a read)."""
        runner = self._pulse_runner
        if runner is None:
            return
        r = runner.step(charge)
        if r["off"]:
            self._filament_pulse_off()
        if r["fire"] is not None:
            self._filament_fire_pulse(r["fire"], charge)
        cyc = r["cycle"]
        if cyc is not None:
            self._log_event(
                charge, Action.HEAT,
                f"read width={cyc.width_ms:.3g} ms  Δq={cyc.delta_q:+.2f} e  "
                f"charge={cyc.charge:+.1f} e",
            )
            self.cycle_logged.emit(cyc)
        if r["done"]:
            self._pulse_runner = None
            self._filament_pulse_off()
            if self._goal_met(charge):
                self._reach_goal(charge)
            elif r.get("reason") == "maxed":
                # Ramp reached the max width without meeting the goal — stop
                # (bounded ramp; the filament couldn't get there).
                self.stop(
                    f"Filament reached max width "
                    f"{self._filament_ramp.max_width_ms:.3g} ms — goal not "
                    f"reached (charge {charge:+.1f} e)")
            elif self._mode == "target" and self._policy == "filament":
                # Forced filament overshot below the band — it can't raise back.
                self.stop(f"Overshot: {charge:+.1f} e below target "
                          f"{self._target_charge:+.1f} e — filament can't raise it")
            elif self._mode == "target":
                # Auto: overshot below the band; the next poll re-decides and
                # flashes the charge back up toward the target.
                self._current_action = Action.NONE
                self.action_changed.emit(
                    f"Filament overshot to {charge:+.1f} e — correcting with flash")
            else:
                self.stop(f"Filament stopped at {charge:+.1f} e")

    def _filament_fire_pulse(self, width_ms: float, charge: float):
        """Fire a single filament pulse of the given width (see FilamentAdapter
        .fire_pulse — one hardware-timed pulse via a very-low-freq carrier)."""
        if self._filament is None or not getattr(self._filament, "is_connected", False):
            self.action_changed.emit("Filament not connected!")
            return
        fire = getattr(self._filament, "fire_pulse", None)
        if fire is None:
            self.action_changed.emit("Filament actuator has no pulse support")
            return
        try:
            fire(width_ms)
        except Exception as e:
            self.action_changed.emit(f"Filament error: {e}")
            return
        self.action_changed.emit(
            f"Filament pulse {width_ms:.3g} ms — charge {charge:+.1f} e"
        )

    def _filament_pulse_off(self):
        """Turn the filament pulse output off (idempotent)."""
        off = getattr(self._filament, "pulse_off", None) if self._filament is not None else None
        if off is not None:
            try:
                off()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Target reached / actuator + setback helpers
    # ------------------------------------------------------------------

    def _reach_goal(self, charge: float):
        """Goal reached — record it and stop (outputs off)."""
        self._log_event(charge, Action.AT_TARGET, "goal reached")
        self.target_reached.emit(charge)
        if self._mode == "change":
            dq = charge - (self._start_charge if self._start_charge is not None else charge)
            self.stop(f"Changed charge by {dq:+.1f} e — now {charge:+.1f} e")
        else:
            self.stop(f"Reached target: {charge:+.1f} e "
                      f"(target {self._target_charge:+.1f})")

    def _park_setback(self):
        if self._setback is not None:
            try:
                self._setback.park()
            except Exception:
                pass

    def _restore_setback(self):
        if self._setback is not None:
            try:
                self._setback.restore()
            except Exception:
                pass

    def _stop_all_actuators(self):
        """Disable both actuators (safe to call even if not active)."""
        if self._flashlamp is not None:
            try:
                if self._flashlamp.is_connected:
                    self._flashlamp.disable()
            except Exception:
                pass
        if self._filament is not None:
            try:
                if self._filament.is_connected:
                    self._filament.disable()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Event log
    # ------------------------------------------------------------------

    def _log_event(self, charge: float, action: Action, detail: str = ""):
        event = ControlEvent(
            timestamp=time.time(),
            charge_e=charge,
            target_e=self._target_charge,
            tolerance=self._tolerance,
            action=action,
            detail=detail,
        )
        self._event_log.append(event)
        self.event_logged.emit(event)
        log.info("CONTROL: %s  %s", action.value, detail)

    def get_event_log(self) -> list[ControlEvent]:
        return list(self._event_log)

    def clear_event_log(self) -> None:
        self._event_log.clear()

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        return {
            "enabled": self._enabled,
            "target_charge": self._target_charge,
            "tolerance": self._tolerance,
            "policy": self._policy,
            "timeout_s": self._timeout_s,
            "current_action": self._current_action.value,
            "last_charge": self._last_charge,
            "flash_on": self._flash_on,
            "ramping": self._pulse_runner is not None,
            "n_events": len(self._event_log),
        }

    # ------------------------------------------------------------------
    # Serialisation helpers (for GUI config save/restore)
    # ------------------------------------------------------------------

    def get_config(self) -> dict:
        return {
            "target_charge": self._target_charge,
            "tolerance": self._tolerance,
            "policy": self._policy,
            "timeout_s": self._timeout_s,
            "flash_rate_hz": self._flash_rate_hz,
            "flash_ctrl_v": self._flash_ctrl_v,
        }

    def restore_config(self, cfg: dict) -> None:
        if "target_charge" in cfg:
            self._target_charge = float(cfg["target_charge"])
        if "tolerance" in cfg:
            self._tolerance = float(cfg["tolerance"])
        if cfg.get("policy") in self.POLICIES:
            self._policy = cfg["policy"]
        if "timeout_s" in cfg:
            self._timeout_s = max(1.0, float(cfg["timeout_s"]))
        if "flash_rate_hz" in cfg:
            self._flash_rate_hz = float(cfg["flash_rate_hz"])
        if "flash_ctrl_v" in cfg:
            self._flash_ctrl_v = float(cfg["flash_ctrl_v"])
