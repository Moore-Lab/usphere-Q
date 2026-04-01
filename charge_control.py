"""
charge_control.py

Charge state control engine for usphere-charge.

GUI-independent: all logic lives here.  The GUI (or a headless script)
creates a ChargeController, wires up a ChargeStateSource, and calls
set_target() / add_threshold_rule().

Classes:
    ChargeController    — bang-bang controller that commands charge state
    ThresholdRule       — "if charge crosses X, go to target Y"
    ControlEvent        — timestamped record of every action taken

Typical usage (headless)::

    from charge_analysis import SR530SerialSource
    from charge_control import ChargeController

    source = SR530SerialSource("COM5", volts_per_electron=0.003)
    source.start()

    ctrl = ChargeController(
        source=source,
        flashlamp=flashlamp_controller,
        filament=filament_controller,
    )
    ctrl.set_target(charge_e=0, tolerance=0.5)
    ctrl.add_threshold_rule(lower=-3, upper=3, target_charge=0, tolerance=0.5)
    ctrl.start()
    # ... ctrl.stop() when done
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from PyQt5.QtCore import QObject, QThread, Qt, pyqtSignal

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
    """
    lower: float              # lower bound (electrons)
    upper: float              # upper bound (electrons)
    target_charge: float      # target to command when triggered
    tolerance: float = 0.5    # tolerance for the target
    enabled: bool = True
    name: str = ""


# ---------------------------------------------------------------------------
# ChargeController
# ---------------------------------------------------------------------------

class ChargeController(QObject):
    """
    Bang-bang controller for microsphere charge state.

    Strategy:
        charge too positive  → flash lamp (UV removes electrons → charge → 0)
        charge too negative  → also flash lamp (UV photoionises → charge → 0)
        charge near zero but target is nonzero
                             → filament adds negative charge
                                (overshoot past target → flash to come back)
        at target ± tolerance → do nothing

    The controller does NOT directly read the source — it receives
    charge updates via ``on_charge_update(result_dict)``.  The GUI or
    script wires the source's signal to this method.

    Actuator protocol:
        flashlamp.enable()  / flashlamp.disable()
        filament.enable()   / filament.disable()
    """

    # Signals for GUI
    action_changed = pyqtSignal(str)        # human-readable status
    event_logged = pyqtSignal(object)       # ControlEvent
    target_reached = pyqtSignal(float)      # charge when target is reached

    def __init__(
        self,
        flashlamp=None,
        filament=None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._flashlamp = flashlamp
        self._filament = filament

        # Target
        self._target_charge: float = 0.0
        self._tolerance: float = 0.5
        self._enabled: bool = False

        # Threshold rules
        self._rules: list[ThresholdRule] = []

        # State
        self._current_action = Action.NONE
        self._last_charge: float | None = None
        self._event_log: list[ControlEvent] = []

        # Timing
        self._flash_duration_s: float = 2.0    # how long to keep flash lamp on
        self._heat_duration_s: float = 3.0     # how long to keep filament on
        self._settle_time_s: float = 2.0       # wait after actuation
        self._action_start: float = 0.0
        self._settling: bool = False
        self._settle_start: float = 0.0

        # Safety: max consecutive actions before pausing
        self._max_consecutive: int = 20
        self._consecutive_count: int = 0

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_target(self, charge_e: float, tolerance: float = 0.5) -> None:
        """Set the target charge state (in units of electron charges)."""
        self._target_charge = charge_e
        self._tolerance = tolerance
        self._consecutive_count = 0
        log.info("Target set: %+.1f e  (±%.1f)", charge_e, tolerance)

    def get_target(self) -> tuple[float, float]:
        """Return (target_charge, tolerance)."""
        return self._target_charge, self._tolerance

    def set_timing(
        self,
        flash_duration_s: float | None = None,
        heat_duration_s: float | None = None,
        settle_time_s: float | None = None,
    ) -> None:
        if flash_duration_s is not None:
            self._flash_duration_s = flash_duration_s
        if heat_duration_s is not None:
            self._heat_duration_s = heat_duration_s
        if settle_time_s is not None:
            self._settle_time_s = settle_time_s

    def set_actuators(self, flashlamp=None, filament=None) -> None:
        """Attach or replace actuator controllers."""
        if flashlamp is not None:
            self._flashlamp = flashlamp
        if filament is not None:
            self._filament = filament

    # ------------------------------------------------------------------
    # Threshold rules
    # ------------------------------------------------------------------

    def add_threshold_rule(
        self,
        lower: float,
        upper: float,
        target_charge: float,
        tolerance: float = 0.5,
        name: str = "",
    ) -> ThresholdRule:
        """Add a threshold rule.  Returns the rule for later reference."""
        rule = ThresholdRule(
            lower=lower, upper=upper,
            target_charge=target_charge, tolerance=tolerance,
            name=name or f"rule_{len(self._rules)}",
        )
        self._rules.append(rule)
        return rule

    def remove_rule(self, rule: ThresholdRule) -> None:
        if rule in self._rules:
            self._rules.remove(rule)

    def clear_rules(self) -> None:
        self._rules.clear()

    def get_rules(self) -> list[ThresholdRule]:
        return list(self._rules)

    # ------------------------------------------------------------------
    # Enable / Disable
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Enable the control loop."""
        self._enabled = True
        self._consecutive_count = 0
        self._settling = False
        log.info("Control loop started — target %+.1f e", self._target_charge)
        self.action_changed.emit(f"Started — target {self._target_charge:+.1f} e")

    def stop(self) -> None:
        """Disable the control loop and turn off all actuators."""
        self._enabled = False
        self._stop_all_actuators()
        self._current_action = Action.NONE
        log.info("Control loop stopped")
        self.action_changed.emit("Stopped")

    @property
    def is_running(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------
    # Core: charge update callback
    # ------------------------------------------------------------------

    def on_charge_update(self, result: dict) -> None:
        """
        Called on every new charge measurement.  This is the main entry
        point — connect the source's charge_updated signal here.
        """
        if not self._enabled:
            return

        charge = result.get("charge_e")
        if charge is None:
            return

        self._last_charge = charge

        # --- Check threshold rules first ---
        for rule in self._rules:
            if not rule.enabled:
                continue
            if charge < rule.lower or charge > rule.upper:
                log.info(
                    "Threshold '%s' triggered: charge %.1f outside [%.1f, %.1f] "
                    "→ target %+.1f",
                    rule.name, charge, rule.lower, rule.upper, rule.target_charge,
                )
                self._target_charge = rule.target_charge
                self._tolerance = rule.tolerance
                self._consecutive_count = 0
                self._settling = False
                self._stop_all_actuators()
                self.action_changed.emit(
                    f"Rule '{rule.name}' triggered → target {rule.target_charge:+.1f} e"
                )
                break

        # --- Are we settling after an actuation? ---
        if self._settling:
            if time.time() - self._settle_start < self._settle_time_s:
                return  # still settling
            self._settling = False

        # --- Check if we're at target ---
        error = charge - self._target_charge
        if abs(error) <= self._tolerance:
            if self._current_action != Action.AT_TARGET:
                self._current_action = Action.AT_TARGET
                self._consecutive_count = 0
                self._log_event(charge, Action.AT_TARGET, "At target")
                self.action_changed.emit(
                    f"At target: {charge:+.1f} e  (target {self._target_charge:+.1f})"
                )
                self.target_reached.emit(charge)
            return

        # --- Safety check ---
        if self._consecutive_count >= self._max_consecutive:
            self._stop_all_actuators()
            self._current_action = Action.NONE
            self.action_changed.emit(
                f"SAFETY: {self._max_consecutive} consecutive actions — paused"
            )
            log.warning("Safety limit reached — pausing control loop")
            return

        # --- Decide action ---
        action = self._decide_action(charge, error)
        self._execute_action(action, charge, error)

    # ------------------------------------------------------------------
    # Decision logic
    # ------------------------------------------------------------------

    def _decide_action(self, charge: float, error: float) -> Action:
        """
        Decide what to do based on current charge and error.

        Strategy:
            - If target is 0 (neutral):
                charge != 0 → flash to neutralise
            - If target > 0 (positive):
                charge < target → flash (remove negative / photoionise)
                charge > target → flash (remove excess positive)
                Note: filament adds negative charge, so it moves away
                      from positive targets. Use flash only.
            - If target < 0 (negative):
                charge > target (less negative) → heat filament (add negative)
                charge < target (more negative) → flash (remove)
            - If target is 0 and charge is 0:
                AT_TARGET (handled above)
        """
        target = self._target_charge

        if target >= 0:
            # Positive or zero target — flash lamp in all cases
            # (flash removes charge toward neutral; if target > 0 there's
            #  no way to add positive charge except photoionisation which
            #  is stochastic — flash is the only tool)
            return Action.FLASH
        else:
            # Negative target
            if error > 0:
                # charge is above target (less negative or more positive)
                # → filament adds negative charge to bring charge down
                return Action.HEAT
            else:
                # charge is below target (more negative)
                # → flash removes some charge to bring toward 0/target
                return Action.FLASH

    # ------------------------------------------------------------------
    # Actuation
    # ------------------------------------------------------------------

    def _execute_action(self, action: Action, charge: float, error: float):
        """Fire the appropriate actuator."""
        now = time.time()

        # If we're already doing this action and it hasn't timed out, skip
        if self._current_action == action and now - self._action_start < self._get_duration(action):
            return

        # Stop the other actuator
        self._stop_all_actuators()

        if action == Action.FLASH:
            if self._flashlamp is None or not self._flashlamp.is_connected:
                self.action_changed.emit("Flash lamp not connected!")
                return
            try:
                self._flashlamp.enable()
            except Exception as e:
                self.action_changed.emit(f"Flash lamp error: {e}")
                return
            self._current_action = Action.FLASH
            self._action_start = now
            self._consecutive_count += 1
            detail = f"charge={charge:+.1f}, error={error:+.1f}, flashing"
            self._log_event(charge, Action.FLASH, detail)
            self.action_changed.emit(f"Flashing — charge {charge:+.1f} e")

            # Schedule stop after duration
            self._schedule_stop(Action.FLASH, self._flash_duration_s)

        elif action == Action.HEAT:
            if self._filament is None or not self._filament.is_connected:
                self.action_changed.emit("Filament not connected!")
                return
            try:
                self._filament.enable()
            except Exception as e:
                self.action_changed.emit(f"Filament error: {e}")
                return
            self._current_action = Action.HEAT
            self._action_start = now
            self._consecutive_count += 1
            detail = f"charge={charge:+.1f}, error={error:+.1f}, heating"
            self._log_event(charge, Action.HEAT, detail)
            self.action_changed.emit(f"Heating filament — charge {charge:+.1f} e")

            # Schedule stop after duration
            self._schedule_stop(Action.HEAT, self._heat_duration_s)

    def _schedule_stop(self, action: Action, duration_s: float):
        """
        After actuating for duration_s, disable the actuator and enter
        settle mode.  Uses a background thread to avoid blocking.
        """
        def _stop_after_delay():
            time.sleep(duration_s)
            self._stop_all_actuators()
            self._settling = True
            self._settle_start = time.time()
            self._current_action = Action.WAIT
            self.action_changed.emit("Settling…")

        import threading
        t = threading.Thread(target=_stop_after_delay, daemon=True)
        t.start()

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

    def _get_duration(self, action: Action) -> float:
        if action == Action.FLASH:
            return self._flash_duration_s
        elif action == Action.HEAT:
            return self._heat_duration_s
        return 0.0

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
            "current_action": self._current_action.value,
            "last_charge": self._last_charge,
            "consecutive_actions": self._consecutive_count,
            "settling": self._settling,
            "n_rules": len(self._rules),
            "n_events": len(self._event_log),
        }

    # ------------------------------------------------------------------
    # Serialisation helpers (for GUI config save/restore)
    # ------------------------------------------------------------------

    def get_config(self) -> dict:
        return {
            "target_charge": self._target_charge,
            "tolerance": self._tolerance,
            "flash_duration_s": self._flash_duration_s,
            "heat_duration_s": self._heat_duration_s,
            "settle_time_s": self._settle_time_s,
            "max_consecutive": self._max_consecutive,
            "rules": [
                {
                    "lower": r.lower,
                    "upper": r.upper,
                    "target_charge": r.target_charge,
                    "tolerance": r.tolerance,
                    "enabled": r.enabled,
                    "name": r.name,
                }
                for r in self._rules
            ],
        }

    def restore_config(self, cfg: dict) -> None:
        if "target_charge" in cfg:
            self._target_charge = float(cfg["target_charge"])
        if "tolerance" in cfg:
            self._tolerance = float(cfg["tolerance"])
        if "flash_duration_s" in cfg:
            self._flash_duration_s = float(cfg["flash_duration_s"])
        if "heat_duration_s" in cfg:
            self._heat_duration_s = float(cfg["heat_duration_s"])
        if "settle_time_s" in cfg:
            self._settle_time_s = float(cfg["settle_time_s"])
        if "max_consecutive" in cfg:
            self._max_consecutive = int(cfg["max_consecutive"])
        if "rules" in cfg:
            self._rules.clear()
            for rd in cfg["rules"]:
                self._rules.append(ThresholdRule(**rd))
