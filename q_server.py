"""
q_server.py  —  usphere-Q ZMQ server

Owns the charge-control actuators (flash lamp, filament) and exposes
the ChargeController state over ZMQ so that usphere-DAQ can write
charge readings into H5 files and usphere-EXPT scripts can command
the charge loop.

Run standalone::

    python q_server.py                     # default ports 5554/5555
    python q_server.py --no-gui

ZMQ commands (send to REP port 5554)
--------------------------------------
Built-in (from zmq_base):
    ping                        liveness check
    get_state                   current charge controller state
    get_info                    port / module info

Charge state:
    get_charge                  → latest charge reading dict
    update_charge  charge_e [polarity] [raw]
                                push a new charge measurement into the
                                controller (from external source / lock-in)

Controller:
    start_control               enable the bang-bang loop
    stop_control                disable the loop + turn off actuators
    set_target     charge_e [tolerance]
    set_timing     [flash_duration_s] [heat_duration_s] [settle_time_s]
    get_config                  → full controller config dict
    get_status                  → controller status dict

Rules:
    add_rule    lower upper target_charge [tolerance] [name]
    clear_rules

Actuators (manual / test):
    flash        [duration_s]   fire flash lamp immediately
    heat         [duration_s]   fire filament immediately
    stop_actuators              disable both actuators

Flashlamp / filament connection:
    connect_flashlamp  port [baud_rate]
    connect_filament   port [baud_rate]
    disconnect_actuators
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from zmq_base import ModuleServer

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# QCoreApplication guard — ChargeController is a QObject so Qt must be
# running even in headless mode.
# ---------------------------------------------------------------------------

def _ensure_qt_app():
    """Create a QCoreApplication if no QApplication exists yet."""
    try:
        from PyQt5.QtCore import QCoreApplication
        if QCoreApplication.instance() is None:
            return QCoreApplication(sys.argv)
    except ImportError:
        pass
    return None


# ---------------------------------------------------------------------------
# QServer
# ---------------------------------------------------------------------------

class QServer(ModuleServer):
    """
    ZMQ server wrapping ChargeController + flashlamp + filament.

    The controller's on_charge_update() is called directly when a new
    charge measurement arrives (via update_charge command or internal
    file watcher).  The latest charge reading is cached for ZMQ queries.
    """

    def __init__(
        self,
        rep_port: int = 5554,
        pub_port: int = 5555,
        publish_interval_s: float = 0.5,
    ) -> None:
        super().__init__(
            module_name="q",
            rep_port=rep_port,
            pub_port=pub_port,
            publish_interval_s=publish_interval_s,
        )
        self._lock = threading.Lock()

        # Lazy imports so the module loads even without PyQt5
        from charge_control import ChargeController
        self._controller = ChargeController()

        # Latest charge reading
        self._latest_charge: dict | None = None

        # Connect controller signals to our cache updater
        try:
            self._controller.action_changed.connect(self._on_action_changed)
        except Exception:
            pass  # headless — signals may not deliver but logic still works

        self._last_action: str = "none"

        # Actuator controllers (connected lazily)
        self._flashlamp = None
        self._filament  = None

    # ------------------------------------------------------------------
    # Signal callbacks
    # ------------------------------------------------------------------

    def _on_action_changed(self, msg: str) -> None:
        self._last_action = msg

    # ------------------------------------------------------------------
    # get_state — streamed by PUB loop
    # ------------------------------------------------------------------

    def get_state(self) -> dict:
        with self._lock:
            status = self._controller.get_status()
            charge_data = dict(self._latest_charge) if self._latest_charge else {}
        return {
            "controller":   status,
            "latest_charge": charge_data,
            "last_action":  self._last_action,
            "flashlamp_connected": (
                self._flashlamp is not None and
                getattr(self._flashlamp, "is_connected", False)
            ),
            "filament_connected": (
                self._filament is not None and
                getattr(self._filament, "is_connected", False)
            ),
        }

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    def handle_command(self, cmd: str, args: dict) -> dict:
        try:
            return self._dispatch_q(cmd, args)
        except Exception as exc:
            log.exception("Command %r raised", cmd)
            return {"status": "error", "message": str(exc)}

    def _dispatch_q(self, cmd: str, args: dict) -> dict:

        # ---- Charge state ----
        if cmd == "get_charge":
            with self._lock:
                data = dict(self._latest_charge) if self._latest_charge else {}
            return {"status": "ok", "data": data}

        if cmd == "update_charge":
            reading = {
                "charge_e":  float(args["charge_e"]),
                "polarity":  args.get("polarity"),
                "timestamp": time.time(),
            }
            if "raw" in args:
                reading["raw"] = args["raw"]
            with self._lock:
                self._latest_charge = reading
                self._controller.on_charge_update(reading)
            return {"status": "ok"}

        # ---- Controller lifecycle ----
        if cmd == "start_control":
            with self._lock:
                self._controller.start()
            return {"status": "ok"}

        if cmd == "stop_control":
            with self._lock:
                self._controller.stop()
            return {"status": "ok"}

        if cmd == "set_target":
            with self._lock:
                self._controller.set_target(
                    charge_e=float(args["charge_e"]),
                    tolerance=float(args.get("tolerance", 0.5)),
                )
            return {"status": "ok"}

        if cmd == "set_timing":
            kwargs = {}
            for k in ("flash_duration_s", "heat_duration_s", "settle_time_s"):
                if k in args:
                    kwargs[k] = float(args[k])
            with self._lock:
                self._controller.set_timing(**kwargs)
            return {"status": "ok"}

        if cmd == "get_config":
            with self._lock:
                cfg = self._controller.get_config()
            return {"status": "ok", "data": cfg}

        if cmd == "get_status":
            with self._lock:
                status = self._controller.get_status()
            return {"status": "ok", "data": status}

        # ---- Threshold rules ----
        if cmd == "add_rule":
            with self._lock:
                rule = self._controller.add_threshold_rule(
                    lower=float(args["lower"]),
                    upper=float(args["upper"]),
                    target_charge=float(args["target_charge"]),
                    tolerance=float(args.get("tolerance", 0.5)),
                    name=args.get("name", ""),
                )
            return {"status": "ok", "data": {"name": rule.name}}

        if cmd == "clear_rules":
            with self._lock:
                self._controller.clear_rules()
            return {"status": "ok"}

        # ---- Manual actuator commands (for testing) ----
        if cmd == "flash":
            duration = float(args.get("duration_s", 2.0))
            result = self._manual_flash(duration)
            return {"status": "ok", "data": result}

        if cmd == "heat":
            duration = float(args.get("duration_s", 3.0))
            result = self._manual_heat(duration)
            return {"status": "ok", "data": result}

        if cmd == "stop_actuators":
            self._stop_actuators()
            return {"status": "ok"}

        # ---- Actuator connections ----
        if cmd == "connect_flashlamp":
            return self._cmd_connect_flashlamp(args)

        if cmd == "connect_filament":
            return self._cmd_connect_filament(args)

        if cmd == "disconnect_actuators":
            self._disconnect_actuators()
            return {"status": "ok"}

        return {"status": "error", "message": f"unknown command: {cmd!r}"}

    # ------------------------------------------------------------------
    # Actuator helpers
    # ------------------------------------------------------------------

    def _cmd_connect_flashlamp(self, args: dict) -> dict:
        try:
            from wg_flashlamp import FlashLampController
            port = args.get("port", "GPIB::2")
            fl = FlashLampController(resource_name=port)
            fl.connect()
            with self._lock:
                self._flashlamp = fl
                self._controller.set_actuators(flashlamp=fl)
            return {"status": "ok", "data": {"message": f"flashlamp connected on {port}"}}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def _cmd_connect_filament(self, args: dict) -> dict:
        try:
            from wg_filament import FilamentController
            port = args.get("port", "GPIB::3")
            fil = FilamentController(resource_name=port)
            fil.connect()
            with self._lock:
                self._filament = fil
                self._controller.set_actuators(filament=fil)
            return {"status": "ok", "data": {"message": f"filament connected on {port}"}}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def _disconnect_actuators(self) -> None:
        with self._lock:
            for act in (self._flashlamp, self._filament):
                if act is not None:
                    try:
                        act.disconnect()
                    except Exception:
                        pass
            self._flashlamp = None
            self._filament  = None

    def _manual_flash(self, duration_s: float) -> dict:
        with self._lock:
            fl = self._flashlamp
        if fl is None or not getattr(fl, "is_connected", False):
            return {"message": "flash lamp not connected"}
        try:
            fl.enable()
            time.sleep(duration_s)
            fl.disable()
            return {"message": f"flashed for {duration_s:.2f}s"}
        except Exception as exc:
            return {"message": f"flash error: {exc}"}

    def _manual_heat(self, duration_s: float) -> dict:
        with self._lock:
            fil = self._filament
        if fil is None or not getattr(fil, "is_connected", False):
            return {"message": "filament not connected"}
        try:
            fil.enable()
            time.sleep(duration_s)
            fil.disable()
            return {"message": f"heated for {duration_s:.2f}s"}
        except Exception as exc:
            return {"message": f"heat error: {exc}"}

    def _stop_actuators(self) -> None:
        with self._lock:
            for act in (self._flashlamp, self._filament):
                if act is not None:
                    try:
                        act.disable()
                    except Exception:
                        pass


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="usphere-Q ZMQ server")
    p.add_argument("--rep",    type=int, default=5554, help="REP port (default 5554)")
    p.add_argument("--pub",    type=int, default=5555, help="PUB port (default 5555)")
    p.add_argument("--no-gui", action="store_true",    help="headless mode (no Qt GUI)")
    return p.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s")
    args = _parse_args()

    qt_app = _ensure_qt_app()

    server = QServer(rep_port=args.rep, pub_port=args.pub)
    server.start()
    log.info("q server listening  REP=tcp://*:%d  PUB=tcp://*:%d", args.rep, args.pub)

    if args.no_gui or qt_app is not None:
        log.info("Running headless — Ctrl-C to stop")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()
            server._disconnect_actuators()
    else:
        try:
            from PyQt5.QtWidgets import QApplication
            import charge_gui as gui_mod
        except ImportError as exc:
            log.error("Cannot import GUI (%s) — rerun with --no-gui", exc)
            sys.exit(1)

        app = QApplication(sys.argv)
        window = gui_mod.ChargeWindow(controller=server._controller,
                                      flashlamp=server._flashlamp,
                                      filament=server._filament)
        window.show()
        try:
            sys.exit(app.exec_())
        finally:
            server.stop()
            server._disconnect_actuators()


if __name__ == "__main__":
    main()
