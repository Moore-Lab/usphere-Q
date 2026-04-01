"""
wg_drive.py

Module for WG1: GW Instek AFG-2225 waveform generator outputting the
monitor drive tone.

  CH1 — continuous sine wave to drive electrodes (charge monitoring)

Module-level metadata is consumed by the GUI to build the config panel
automatically (same protocol as usphere-DAQ device plugins).

Controller class is used by the control loop and the GUI connect/disconnect
buttons.  It also proxies the full AFG2225Controller API so WaveformGenTab
can use it for manual control.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# AFG2225 driver import
# ---------------------------------------------------------------------------

_AFG_PATH = Path(__file__).parent / "resources" / "GWINSTEKAFG2225_controller"
if str(_AFG_PATH) not in sys.path:
    sys.path.insert(0, str(_AFG_PATH))

try:
    from afg2225_controller import AFG2225Controller
    AFG_AVAILABLE = True
except ImportError:
    AFG_AVAILABLE = False


# ---------------------------------------------------------------------------
# Module identity  (GUI protocol)
# ---------------------------------------------------------------------------

MODULE_NAME = "Drive"
DEVICE_NAME = "Drive Tone (INSTEK AFG-2225 — WG1)"

CONFIG_FIELDS: list[dict] = [
    {
        "key":     "com_port",
        "label":   "COM port",
        "type":    "text",
        "default": "",
    },
    {
        "key":     "frequency_hz",
        "label":   "Drive frequency (Hz)",
        "type":    "text",
        "default": "100.0",
    },
    {
        "key":     "amplitude_v",
        "label":   "Amplitude (Vpp)",
        "type":    "text",
        "default": "1.0",
    },
]

DEFAULTS: dict = {
    "frequency_hz": 100.0,
    "amplitude_v":    1.0,
}


# ---------------------------------------------------------------------------
# Module-level test  (GUI protocol)
# ---------------------------------------------------------------------------

def test(config: dict) -> tuple[bool, str]:
    if not AFG_AVAILABLE:
        return False, "AFG2225 driver not found"
    port = config.get("com_port", "").strip()
    if not port:
        return False, "No COM port specified"
    afg = AFG2225Controller()
    try:
        if afg.connect(port):
            idn = afg.idn or "unknown"
            afg.disconnect()
            return True, f"OK — {idn}"
        return False, "connect() returned False"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class DriveController:
    """
    Manages WG1 (INSTEK AFG-2225) for drive tone output.

    Typical usage
    -------------
    ctrl = DriveController(config)
    ctrl.connect()          # called when user clicks Connect in GUI
    ctrl.enable()           # called by control loop — starts sine tone
    ctrl.disable()          # called by control loop — stops sine tone
    ctrl.configure(config)  # called when user edits params in GUI
    ctrl.disconnect()       # called when user clicks Disconnect

    Also proxies AFG2225Controller's setup_* / output_* / get_* methods
    so WaveformGenTab can control it manually.
    """

    def __init__(self, config: dict):
        self._config: dict = dict(config)
        self._afg: AFG2225Controller | None = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        if not AFG_AVAILABLE:
            raise RuntimeError("AFG2225 driver not found")
        port = self._config.get("com_port", "").strip()
        if not port:
            raise ValueError("No COM port specified")
        self._afg = AFG2225Controller()
        return self._afg.connect(port)

    def disconnect(self) -> None:
        if self._afg:
            try:
                self._afg.output_off(1)
            except Exception:
                pass
            try:
                self._afg.disconnect()
            except Exception:
                pass
            self._afg = None

    @property
    def is_connected(self) -> bool:
        return self._afg is not None and self._afg.is_connected

    @property
    def idn(self):
        return self._afg.idn if self._afg else None

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def configure(self, config: dict) -> None:
        self._config = dict(config)
        if self.is_connected:
            self._apply_waveform()

    def _parse(self, key: str) -> float:
        try:
            return float(self._config.get(key, DEFAULTS[key]))
        except (ValueError, TypeError):
            return float(DEFAULTS[key])

    def _apply_waveform(self) -> None:
        freq = self._parse("frequency_hz")
        amp = self._parse("amplitude_v")
        self._afg.setup_sine(1, frequency=freq, amplitude=amp, offset=0.0)

    # ------------------------------------------------------------------
    # Actuation  (called by control loop)
    # ------------------------------------------------------------------

    def enable(self) -> bool:
        if not self.is_connected:
            raise RuntimeError("Drive not connected")
        self._apply_waveform()
        return self._afg.output_on(1)

    def disable(self) -> bool:
        if not self.is_connected:
            raise RuntimeError("Drive not connected")
        return self._afg.output_off(1)

    # ------------------------------------------------------------------
    # Proxy methods for WaveformGenTab
    # ------------------------------------------------------------------

    def setup_sine(self, channel=1, frequency=1000.0, amplitude=5.0,
                   offset=0.0, **kw) -> bool:
        if self._afg:
            return self._afg.setup_sine(channel, frequency, amplitude, offset)
        return False

    def setup_square(self, channel=1, frequency=1000.0, amplitude=5.0,
                     offset=0.0, duty_cycle=None, **kw) -> bool:
        if self._afg:
            ok = self._afg.setup_square(channel, frequency, amplitude, offset)
            if ok and duty_cycle is not None:
                self._afg.waveform.set_square_duty_cycle(channel, duty_cycle)
            return ok
        return False

    def setup_pulse(self, channel=1, frequency=1000.0, amplitude=5.0,
                    offset=0.0, duty_cycle=None, **kw) -> bool:
        if self._afg:
            ok = self._afg.setup_pulse(channel, frequency, amplitude, offset)
            if ok and duty_cycle is not None and frequency > 0:
                width = (duty_cycle / 100.0) / frequency
                self._afg.waveform.set_pulse_width(channel, width)
            return ok
        return False

    def setup_ramp(self, channel=1, frequency=1000.0, amplitude=5.0,
                   offset=0.0, **kw) -> bool:
        if self._afg:
            return self._afg.setup_ramp(channel, frequency, amplitude, offset)
        return False

    def setup_dc(self, channel=1, voltage=0.0, **kw) -> bool:
        if self._afg:
            return self._afg.setup_sine(channel, 1.0, 0.001, voltage)
        return False

    def setup_noise(self, channel=1, amplitude=5.0, offset=0.0, **kw) -> bool:
        if self._afg:
            return self._afg.setup_noise(channel, amplitude, offset)
        return False

    def output_on(self, channel=1) -> bool:
        if self._afg:
            return self._afg.output_on(channel)
        return False

    def output_off(self, channel=1) -> bool:
        if self._afg:
            return self._afg.output_off(channel)
        return False

    def set_frequency(self, channel, frequency) -> bool:
        if self._afg:
            return self._afg.set_frequency(channel, frequency)
        return False

    def set_amplitude(self, channel, amplitude) -> bool:
        if self._afg:
            return self._afg.set_amplitude(channel, amplitude)
        return False

    def get_status(self) -> dict:
        if self._afg:
            return self._afg.get_status()
        return {"connected": False}
