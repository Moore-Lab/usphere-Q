"""
nge_supply.py

Module for the Rohde & Schwarz NGE100-series DC power supply (an NGE103B on
this rig) used for the charge-control DC lines:

  CH1 — flash-lamp control voltage   (was an AFG-2225 DC channel; now the PSU)
  CH2 — filament power supply        (previously a static, uncontrolled line)
  CH3 — spare

Channel→role assignment is not hard-coded here — it is chosen in the GUI's
NGE channel map (mirroring the electrode channel map), so this module stays a
plain channel-addressed wrapper.

Module-level metadata (MODULE_NAME/DEVICE_NAME/CONFIG_FIELDS/DEFAULTS/test)
follows the same plugin protocol as the wg_* modules and usphere-DAQ device
plugins.  NGESupplyController is the connect/apply/measure object used by the
GUI connection panel and by experiment scripts.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# NGE100 driver import
# ---------------------------------------------------------------------------

_NGE_PATH = Path(__file__).parent / "resources" / "NGE100_controller"
if str(_NGE_PATH) not in sys.path:
    sys.path.insert(0, str(_NGE_PATH))

try:
    from nge100_controller import NGE100Controller
    NGE_AVAILABLE = True
except ImportError:
    NGE_AVAILABLE = False


# ---------------------------------------------------------------------------
# Module identity  (GUI protocol)
# ---------------------------------------------------------------------------

MODULE_NAME = "NGE100"
DEVICE_NAME = "DC Power Supply (R&S NGE100)"

# Hardware limits (NGE100 series: 0–32 V, 0–3 A per channel).
V_MIN, V_MAX = 0.0, 32.0
I_MIN, I_MAX = 0.0, 3.0

CONFIG_FIELDS: list[dict] = [
    {
        "key":     "com_port",
        "label":   "COM port",
        "type":    "text",
        "default": "",
    },
]

DEFAULTS: dict = {
    "com_port": "",
}


# ---------------------------------------------------------------------------
# Module-level test  (GUI protocol — safe to call from a worker thread)
# ---------------------------------------------------------------------------

def test(config: dict) -> tuple[bool, str]:
    """Attempt a connection and return (success, message).  Read-only: opens,
    reads *IDN?, and releases the instrument to local control."""
    if not NGE_AVAILABLE:
        return False, "NGE100 driver not found"
    port = config.get("com_port", "").strip()
    nge = NGE100Controller()
    try:
        ok = nge.connect(port) if port else nge.auto_connect()
        if ok:
            idn = nge.idn or "unknown"
            nge.disconnect()
            return True, f"OK — {idn}"
        return False, "connect() returned False"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class NGESupplyController:
    """
    Thin channel-addressed wrapper around NGE100Controller for the charge rig.

    One physical instrument, up to three channels.  Role→channel assignment is
    the GUI's responsibility; every method here takes an explicit channel.

    Typical usage
    -------------
    psu = NGESupplyController(config)
    psu.connect()                      # GUI Connect button
    psu.set_channel(1, volts=5.0, current=0.1)
    psu.output_on(1)                   # apply the flash-lamp control voltage
    psu.measure(1)                     # -> {"voltage":..., "current":..., "power":...}
    psu.output_off(1)
    psu.disconnect()
    """

    def __init__(self, config: dict | None = None):
        self._config: dict = dict(config or {})
        self._nge: NGE100Controller | None = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        if not NGE_AVAILABLE:
            raise RuntimeError("NGE100 driver not found")
        port = self._config.get("com_port", "").strip()
        self._nge = NGE100Controller()
        ok = self._nge.connect(port) if port else self._nge.auto_connect()
        if not ok:
            self._nge = None
        return ok

    def disconnect(self) -> None:
        if self._nge:
            # Leave every channel output off before releasing the instrument.
            try:
                for ch in range(1, (self._nge.num_channels or 3) + 1):
                    self._nge.output_off(ch)
            except Exception:
                pass
            try:
                self._nge.disconnect()
            except Exception:
                pass
            self._nge = None

    @property
    def is_connected(self) -> bool:
        return self._nge is not None and self._nge.is_connected

    @property
    def idn(self):
        return self._nge.idn if self._nge else None

    @property
    def num_channels(self) -> int:
        return self._nge.num_channels if self._nge else 0

    # ------------------------------------------------------------------
    # Per-channel control
    # ------------------------------------------------------------------

    @staticmethod
    def _clamp(value: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, float(value)))

    def set_voltage(self, channel: int, volts: float) -> bool:
        if not self.is_connected:
            return False
        return self._nge.set_voltage(channel, self._clamp(volts, V_MIN, V_MAX))

    def set_current(self, channel: int, amps: float) -> bool:
        if not self.is_connected:
            return False
        return self._nge.set_current(channel, self._clamp(amps, I_MIN, I_MAX))

    def set_channel(self, channel: int, volts: float, current: float) -> bool:
        """Program voltage and current limit together (does not toggle output)."""
        if not self.is_connected:
            return False
        return self._nge.apply(
            channel,
            self._clamp(volts, V_MIN, V_MAX),
            self._clamp(current, I_MIN, I_MAX),
        )

    def set_easyramp(self, channel: int, duration_ms: float,
                     enabled: bool = True) -> bool:
        """Configure the EasyRamp soft-start (10–10000 ms).  When enabled the
        output voltage ramps to a new setpoint over this duration."""
        if not self.is_connected:
            return False
        fn = getattr(self._nge, "set_easyramp", None)
        if fn is None:
            return False
        return fn(channel, self._clamp(duration_ms, 10.0, 10000.0), enabled)

    def output_on(self, channel: int) -> bool:
        if not self.is_connected:
            return False
        return self._nge.output_on(channel)

    def output_off(self, channel: int) -> bool:
        if not self.is_connected:
            return False
        return self._nge.output_off(channel)

    def is_output_on(self, channel: int) -> bool:
        return self._nge.is_output_on(channel) if self.is_connected else False

    def get_setpoints(self, channel: int) -> dict:
        if not self.is_connected:
            return {"voltage_set": None, "current_set": None, "output_on": False}
        return {
            "voltage_set": self._nge.get_voltage_setpoint(channel),
            "current_set": self._nge.get_current_setpoint(channel),
            "output_on":   self._nge.is_output_on(channel),
        }

    def measure(self, channel: int) -> dict:
        """Return measured {voltage, current, power} for a channel."""
        if not self.is_connected:
            return {"voltage": None, "current": None, "power": None}
        return self._nge.measure_all(channel)

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def configure(self, config: dict) -> None:
        self._config = dict(config)

    @property
    def raw(self) -> "NGE100Controller | None":
        """The underlying NGE100Controller (for advanced/experiment use)."""
        return self._nge


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cfg = {"com_port": sys.argv[1] if len(sys.argv) > 1 else ""}
    print(f"Testing {DEVICE_NAME}  ({cfg['com_port'] or 'auto-discover'})…")
    ok, msg = test(cfg)
    print(f"{'OK' if ok else 'FAILED'}: {msg}")
