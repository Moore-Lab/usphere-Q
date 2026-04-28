"""
sr530_controller.py

RS232 driver for the Stanford Research Systems SR530 Lock-In Amplifier.

Command reference: SR530m.pdf §5-6 (docs/).

Serial settings (set via rear DIP switches SW2):
    Default: 9600 baud / 8 data bits / no parity / 2 stop bits
    Echo mode must be OFF when controlled by a computer (SW2 switch 6 UP).

Usage:
    lia = SR530Controller("COM5")
    lia.connect()
    print(lia.get_frequency())      # reference frequency in Hz
    print(lia.get_x_volts())        # X output in real volts
    lia.set_sensitivity(19)         # G19 = 10 mV full-scale
    lia.set_phase(0.0)
    lia.auto_phase()
    lia.disconnect()

Key SR530-specific notes
------------------------
- Sensitivity indices are 1-based (G4 = 100 nV ... G24 = 500 mV).
  G1-G3 require the optional SRS pre-amplifier.
- QX / QY return the X / Y BNC output voltage (+-10 V = +-full scale).
  Divide by 10 to get fraction of FS; multiply by sensitivity_V to get
  the actual input signal in volts.  Q3 / Q4 do not exist on the SR530 -
  R and theta are computed from QX and QY.
- Post-filter time constant has only three settings: Off / 100 ms / 1 s.
  The pre-filter TC (T1) is the primary time constant (1 ms - 100 s).
- Status byte overload flag is bit 4 (0x10), not bit 2.
- Input channel (A / A-B / I) and oscillator freq/amplitude are set by
  hardware switches/knobs only; they are NOT controllable via serial.
"""

from __future__ import annotations

import math
import time
from typing import Optional

import serial


# ---------------------------------------------------------------------------
# Sensitivity table  (SR530 command index -> label).
# G4 = 100 nV (minimum without SRS pre-amplifier) ... G24 = 500 mV (maximum).
# G1-G3 are valid only when the SRS pre-amplifier is installed.
# ---------------------------------------------------------------------------

SENSITIVITY_TABLE: dict[int, str] = {
    1:  "10 nV  (preamp)",
    2:  "20 nV  (preamp)",
    3:  "50 nV  (preamp)",
    4:  "100 nV",
    5:  "200 nV",
    6:  "500 nV",
    7:  "1 uV",
    8:  "2 uV",
    9:  "5 uV",
    10: "10 uV",
    11: "20 uV",
    12: "50 uV",
    13: "100 uV",
    14: "200 uV",
    15: "500 uV",
    16: "1 mV",
    17: "2 mV",
    18: "5 mV",
    19: "10 mV",
    20: "20 mV",
    21: "50 mV",
    22: "100 mV",
    23: "200 mV",
    24: "500 mV",
}

# ---------------------------------------------------------------------------
# Pre-filter time constant table  (command index -> label).
# Set / query with  T1,n  /  T1.  Index range: 1-11.
# ---------------------------------------------------------------------------

PRE_TIME_CONSTANT_TABLE: dict[int, str] = {
    1:  "1 ms",
    2:  "3 ms",
    3:  "10 ms",
    4:  "30 ms",
    5:  "100 ms",
    6:  "300 ms",
    7:  "1 s",
    8:  "3 s",
    9:  "10 s",
    10: "30 s",
    11: "100 s",
}

# ---------------------------------------------------------------------------
# Post-filter time constant table  (command index -> label).
# Set / query with  T2,n  /  T2.  The SR530 has ONLY three settings.
# ---------------------------------------------------------------------------

POST_TIME_CONSTANT_TABLE: dict[int, str] = {
    0: "Off",
    1: "100 ms",
    2: "1 s",
}

# Backwards-compatible alias (pre-filter table)
TIME_CONSTANT_TABLE = PRE_TIME_CONSTANT_TABLE

# ---------------------------------------------------------------------------
# Harmonic mode table  (M command)
# ---------------------------------------------------------------------------

HARMONIC_MODE_TABLE: dict[int, str] = {
    0: "f  (fundamental)",
    1: "2f (second harmonic)",
}

# ---------------------------------------------------------------------------
# ENBW table  (N command)
# ---------------------------------------------------------------------------

ENBW_TABLE: dict[int, str] = {
    0: "1 Hz",
    1: "10 Hz",
}

# ---------------------------------------------------------------------------
# Reference trigger mode table  (R command)
# ---------------------------------------------------------------------------

TRIGGER_MODE_TABLE: dict[int, str] = {
    0: "Positive edge",
    1: "Symmetric (zero crossing)",
    2: "Negative edge",
}

# ---------------------------------------------------------------------------
# Display select table  (S command)
# Controls what Q1/Q2 return and what the analog meters/LCDs show.
# ---------------------------------------------------------------------------

DISPLAY_SELECT_TABLE: dict[int, str] = {
    0: "X / Y",
    1: "X Offset / Y Offset",
    2: "R / Theta",
    3: "R Offset / Theta",
    4: "X Noise / Y Noise",
    5: "X5 (D/A) / X6 (D/A)",
}

# ---------------------------------------------------------------------------
# Remote mode table  (I command)
# ---------------------------------------------------------------------------

REMOTE_MODE_TABLE: dict[int, str] = {
    0: "Local  (front panel active)",
    1: "Remote (front panel locked)",
    2: "Lockout (key required to restore)",
}

# ---------------------------------------------------------------------------
# Front-panel key numbers for the K command (1-32)
# ---------------------------------------------------------------------------

KEY_TABLE: dict[int, str] = {
    1:  "Post TC Up",
    2:  "Post TC Down",
    3:  "Pre TC Up",
    4:  "Pre TC Down",
    5:  "Select Display (f/phase)",
    6:  "90 deg Up",
    7:  "90 deg Down",
    8:  "Zero Phase",
    9:  "Reference Trigger Mode",
    10: "Reference Mode (f/2f)",
    11: "Degrees Up",
    12: "Degrees Down",
    13: "Channel 2 Rel",
    14: "Channel 2 Offset On/Off",
    15: "Channel 2 Offset Up",
    16: "Channel 2 Offset Down",
    17: "Channel 2 Expand",
    18: "Output Display Up",
    19: "Output Display Down",
    20: "Channel 1 Expand",
    21: "Channel 1 Rel",
    22: "Channel 1 Offset On/Off",
    23: "Channel 1 Offset Up",
    24: "Channel 1 Offset Down",
    25: "Dyn Res Up",
    26: "Dyn Res Down",
    27: "Sensitivity Up",
    28: "Sensitivity Down",
    29: "Local",
    30: "Line x2 Notch Filter",
    31: "Line Notch Filter",
    32: "Bandpass Filter",
}

# ---------------------------------------------------------------------------
# Sensitivity index -> full-scale volts
# ---------------------------------------------------------------------------

_SENS_VOLTS: dict[int, float] = {
    1:  10e-9,
    2:  20e-9,
    3:  50e-9,
    4:  100e-9,
    5:  200e-9,
    6:  500e-9,
    7:  1e-6,
    8:  2e-6,
    9:  5e-6,
    10: 10e-6,
    11: 20e-6,
    12: 50e-6,
    13: 100e-6,
    14: 200e-6,
    15: 500e-6,
    16: 1e-3,
    17: 2e-3,
    18: 5e-3,
    19: 10e-3,
    20: 20e-3,
    21: 50e-3,
    22: 100e-3,
    23: 200e-3,
    24: 500e-3,
}

# Full-scale output voltage of the X / Y BNC connectors (volts)
_BNC_FULL_SCALE = 10.0


def sensitivity_index_to_volts(idx: int) -> float:
    """Convert a sensitivity index (1-24) to its full-scale input voltage."""
    return _SENS_VOLTS.get(idx, 500e-3)  # safe fallback: 500 mV


class SR530Controller:
    """
    RS232 interface to the SR530 Lock-In Amplifier.

    All commands are CR-terminated; responses are CR (echo off) or CR-LF
    (echo on) terminated.  Echo mode must be OFF for computer control.

    This class is NOT thread-safe.  If a background thread polls via
    snapshot() while the main thread sends commands, protect with a lock.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        timeout: float = 2.0,
        write_timeout: float = 2.0,
        echo: bool = False,
    ):
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._write_timeout = write_timeout
        self._echo = echo          # True if SW2-6 is DOWN (echo mode on)
        self._ser: Optional[serial.Serial] = None

    @property
    def port(self) -> str:
        return self._port

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Open the serial port.  Returns True on success."""
        self._ser = serial.Serial(
            port=self._port,
            baudrate=self._baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_TWO,
            timeout=self._timeout,
            write_timeout=self._write_timeout,
        )
        self._ser.reset_input_buffer()
        return self._ser.is_open

    def disconnect(self) -> None:
        if self._ser and self._ser.is_open:
            self._ser.close()
        self._ser = None

    @property
    def is_connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    # ------------------------------------------------------------------
    # Low-level I/O
    # ------------------------------------------------------------------

    def _write(self, cmd: str) -> None:
        """Send a command (auto-appends CR)."""
        if not self.is_connected:
            raise RuntimeError("SR530 not connected")
        self._ser.write((cmd + "\r").encode("ascii"))

    def _read_line(self) -> str:
        """Read one response line (strips CR/LF).

        The SR530 terminates responses with CR only when echo is off (SW2-6 UP,
        the default for computer control).  pyserial's readline() waits for LF
        and would always time out, so we use read_until(b'\\r') instead.
        """
        if not self.is_connected:
            raise RuntimeError("SR530 not connected")
        raw = self._ser.read_until(b"\r")
        return raw.decode("ascii", errors="replace").strip()

    def _query(self, cmd: str) -> str:
        """Send command, return response string.

        With echo on (SW2-6 DOWN) the SR530 sends back the command before the
        value, so we discard that first line.
        """
        self._write(cmd)
        if self._echo:
            self._read_line()   # discard echoed command
        return self._read_line()

    def _query_float(self, cmd: str) -> float:
        return float(self._query(cmd))

    def _query_int(self, cmd: str) -> int:
        # Route through float() first: the SR530 occasionally returns
        # decimal-formatted integers (e.g. "12.0" instead of "12").
        return int(float(self._query(cmd)))

    # ------------------------------------------------------------------
    # Output readings
    # ------------------------------------------------------------------

    def get_x_output(self) -> float:
        """X output as a fraction of full-scale (+-1.0).

        Uses the QX command which reads the X BNC output (+-10 V = +-FS).
        Always returns X regardless of front-panel DISPLAY setting.
        Multiply by sensitivity_v to get real input signal volts.
        """
        return self._query_float("QX") / _BNC_FULL_SCALE

    def get_y_output(self) -> float:
        """Y output as a fraction of full-scale (+-1.0).  See get_x_output()."""
        return self._query_float("QY") / _BNC_FULL_SCALE

    def get_r_output(self) -> float:
        """R = sqrt(X^2+Y^2) as a fraction of full-scale.

        Computed from two QX/QY queries; costs two serial round-trips.
        Use snapshot() to get X, Y, R, theta in one efficient burst.
        """
        x = self.get_x_output()
        y = self.get_y_output()
        return math.sqrt(x * x + y * y)

    def get_theta_output(self) -> float:
        """theta = atan2(Y, X) in degrees.

        Computed from two QX/QY queries.  Use snapshot() for efficiency.
        """
        x = self.get_x_output()
        y = self.get_y_output()
        return math.degrees(math.atan2(y, x))

    def get_q1(self) -> float:
        """Read the Channel 1 display value (units depend on display select S)."""
        return self._query_float("Q1")

    def get_q2(self) -> float:
        """Read the Channel 2 display value (units depend on display select S)."""
        return self._query_float("Q2")

    # ------------------------------------------------------------------
    # Reference / frequency / phase
    # ------------------------------------------------------------------

    def get_frequency(self) -> float:
        """Read the current reference frequency in Hz."""
        return self._query_float("F")

    def set_phase(self, degrees: float) -> None:
        """Set the reference phase shift (-999 to +999 deg, typically -180 to +180)."""
        self._write(f"P {degrees:.2f}")

    def get_phase(self) -> float:
        """Query the reference phase shift in degrees (-180 to +180)."""
        return self._query_float("P")

    def auto_phase(self) -> None:
        """Execute auto-phase: sets phase so theta -> 0, X is maximised, Y minimised.

        Equivalent to pressing the front-panel REL key while displaying theta.
        The SR530 executes this immediately; wait >= 5x post-TC before reading.
        """
        self._write("AP")

    def set_harmonic_mode(self, mode: int) -> None:
        """Set harmonic detection mode: 0 = f (fundamental), 1 = 2f.

        In 2f mode the PLL locks to 2x the reference input frequency.
        """
        if mode not in HARMONIC_MODE_TABLE:
            raise ValueError("mode must be 0 (f) or 1 (2f)")
        self._write(f"M {mode}")

    def get_harmonic_mode(self) -> int:
        """Query harmonic mode: 0 = f, 1 = 2f."""
        return self._query_int("M")

    def set_trigger_mode(self, mode: int) -> None:
        """Set reference input trigger mode.

        0 = Positive edge, 1 = Symmetric (zero crossing), 2 = Negative edge.
        """
        if mode not in TRIGGER_MODE_TABLE:
            raise ValueError("mode must be 0 (positive), 1 (symmetric), or 2 (negative)")
        self._write(f"R {mode}")

    def get_trigger_mode(self) -> int:
        """Query reference trigger mode: 0=positive, 1=symmetric, 2=negative."""
        return self._query_int("R")

    # ------------------------------------------------------------------
    # Sensitivity  (G command, indices 1-24; 1-3 require SRS pre-amp)
    # ------------------------------------------------------------------

    def set_sensitivity(self, index: int) -> None:
        """Set sensitivity by index (4-24 without pre-amp, 1-24 with).

        See SENSITIVITY_TABLE for the index -> full-scale voltage mapping.
        """
        if index not in SENSITIVITY_TABLE:
            raise ValueError(f"Invalid sensitivity index {index} (valid: 1-24)")
        self._write(f"G {index}")

    def get_sensitivity(self) -> int:
        """Query current sensitivity index (1-24)."""
        return self._query_int("G")

    def get_sensitivity_label(self) -> str:
        return SENSITIVITY_TABLE.get(self.get_sensitivity(), "?")

    def get_sensitivity_volts(self) -> float:
        """Return current full-scale sensitivity in volts."""
        return sensitivity_index_to_volts(self.get_sensitivity())

    # ------------------------------------------------------------------
    # Time constants
    # ------------------------------------------------------------------

    def set_pre_time_constant(self, index: int) -> None:
        """Set pre-filter time constant by index (1-11).

        See PRE_TIME_CONSTANT_TABLE for the mapping (1=1 ms ... 11=100 s).
        """
        if index not in PRE_TIME_CONSTANT_TABLE:
            raise ValueError(f"Invalid pre-TC index {index} (valid: 1-11)")
        self._write(f"T1,{index}")

    def get_pre_time_constant(self) -> int:
        """Query pre-filter time constant index (1-11)."""
        return self._query_int("T1")

    def set_post_time_constant(self, index: int) -> None:
        """Set post-filter time constant by index (0=Off, 1=100 ms, 2=1 s).

        The SR530 has only three post-filter settings.
        """
        if index not in POST_TIME_CONSTANT_TABLE:
            raise ValueError(f"Invalid post-TC index {index} (valid: 0, 1, 2)")
        self._write(f"T2,{index}")

    def get_post_time_constant(self) -> int:
        """Query post-filter time constant index (0, 1, or 2)."""
        return self._query_int("T2")

    # ------------------------------------------------------------------
    # Dynamic reserve  (D command: 0=Low Noise, 1=Normal, 2=High Reserve)
    # ------------------------------------------------------------------

    def set_dynamic_reserve(self, mode: int) -> None:
        """Set dynamic reserve: 0=Low Noise, 1=Normal, 2=High Reserve."""
        if mode not in (0, 1, 2):
            raise ValueError("Dynamic reserve mode must be 0, 1, or 2")
        self._write(f"D {mode}")

    def get_dynamic_reserve(self) -> int:
        return self._query_int("D")

    # ------------------------------------------------------------------
    # Filters  (B = bandpass, L1 = line notch, L2 = 2x line notch)
    # ------------------------------------------------------------------

    def set_bandpass_filter(self, enable: bool) -> None:
        """Insert (True) or remove (False) the auto-tracking bandpass filter."""
        self._write("B1" if enable else "B0")

    def get_bandpass_filter(self) -> bool:
        return bool(self._query_int("B"))

    def set_line_notch(self, enable: bool) -> None:
        """Insert (True) or remove (False) the line-frequency notch filter."""
        self._write("L1,1" if enable else "L1,0")

    def get_line_notch(self) -> bool:
        return bool(self._query_int("L1"))

    def set_2x_line_notch(self, enable: bool) -> None:
        """Insert (True) or remove (False) the 2x line-frequency notch filter."""
        self._write("L2,1" if enable else "L2,0")

    def get_2x_line_notch(self) -> bool:
        return bool(self._query_int("L2"))

    # ------------------------------------------------------------------
    # ENBW  (N command: 0 = 1 Hz, 1 = 10 Hz)
    # ------------------------------------------------------------------

    def set_enbw(self, mode: int) -> None:
        """Set equivalent noise bandwidth: 0 = 1 Hz, 1 = 10 Hz."""
        if mode not in ENBW_TABLE:
            raise ValueError("mode must be 0 (1 Hz) or 1 (10 Hz)")
        self._write(f"N {mode}")

    def get_enbw(self) -> int:
        """Query ENBW setting: 0 = 1 Hz, 1 = 10 Hz."""
        return self._query_int("N")

    # ------------------------------------------------------------------
    # Output display select  (S command)
    # Controls Q1/Q2 readings and the front-panel meters/LCDs.
    # ------------------------------------------------------------------

    def set_display_select(self, n: int) -> None:
        """Select what Q1/Q2 and the analog meters display.

        0 = X/Y,  1 = X Offset/Y Offset,  2 = R/Theta,
        3 = R Offset/Theta,  4 = X Noise/Y Noise,  5 = X5/X6 (D/A).
        See DISPLAY_SELECT_TABLE.
        """
        if n not in DISPLAY_SELECT_TABLE:
            raise ValueError(f"display index must be 0-5 (got {n})")
        self._write(f"S {n}")

    def get_display_select(self) -> int:
        """Query current display select index (0-5)."""
        return self._query_int("S")

    # ------------------------------------------------------------------
    # Expand  (E command: channel 1 or 2, on/off)
    # Expand multiplies the channel 1/2 meter and output voltage by x10.
    # Note: does NOT affect QX/QY BNC outputs, only Q1/Q2 and analog meters.
    # ------------------------------------------------------------------

    def set_expand(self, channel: int, enable: bool) -> None:
        """Enable (True) or disable (False) x10 expand for channel 1 or 2."""
        if channel not in (1, 2):
            raise ValueError("channel must be 1 or 2")
        self._write(f"E{channel},{1 if enable else 0}")

    def get_expand(self, channel: int) -> bool:
        """Query expand status for channel 1 or 2."""
        if channel not in (1, 2):
            raise ValueError("channel must be 1 or 2")
        return bool(self._query_int(f"E{channel}"))

    # ------------------------------------------------------------------
    # Manual offset  (OX, OY, OR commands)
    # value is in absolute volts, up to +- full-scale.
    # ------------------------------------------------------------------

    def set_offset_x(self, enable: bool, value: Optional[float] = None) -> None:
        """Enable/disable X offset.  If value is given, sets it simultaneously.

        value must be in volts, within +- current full-scale sensitivity.
        The SR530 stores offset as a fraction of FS; it survives sensitivity changes.
        """
        if value is not None:
            self._write(f"OX {1 if enable else 0},{value:.6E}")
        else:
            self._write(f"OX {1 if enable else 0}")

    def get_offset_x_enabled(self) -> bool:
        """Return True if the X offset is currently enabled."""
        return bool(self._query_int("OX"))

    def set_offset_y(self, enable: bool, value: Optional[float] = None) -> None:
        """Enable/disable Y offset.  Optional value in volts."""
        if value is not None:
            self._write(f"OY {1 if enable else 0},{value:.6E}")
        else:
            self._write(f"OY {1 if enable else 0}")

    def get_offset_y_enabled(self) -> bool:
        return bool(self._query_int("OY"))

    def set_offset_r(self, enable: bool, value: Optional[float] = None) -> None:
        """Enable/disable R offset.  Optional value in volts.

        R offset does not affect X or Y outputs.
        """
        if value is not None:
            self._write(f"OR {1 if enable else 0},{value:.6E}")
        else:
            self._write(f"OR {1 if enable else 0}")

    def get_offset_r_enabled(self) -> bool:
        return bool(self._query_int("OR"))

    # ------------------------------------------------------------------
    # Analog I/O  (X command)
    # X1-X4: rear-panel A/D inputs (read-only, +-10.24 V range).
    # X5-X6: rear-panel D/A outputs (read/write, +-10.238 V range).
    #        On power-up X5 is the ratio output; writing overrides it
    #        until the next reset/power-cycle.
    # ------------------------------------------------------------------

    def read_analog_input(self, n: int) -> float:
        """Read rear-panel analog input Xn (n = 1-4) in volts."""
        if n not in (1, 2, 3, 4):
            raise ValueError(f"analog input n must be 1-4 (got {n})")
        return self._query_float(f"X {n}")

    def get_da_output(self, n: int) -> float:
        """Read current value of D/A output Xn (n = 5 or 6) in volts."""
        if n not in (5, 6):
            raise ValueError(f"D/A output n must be 5 or 6 (got {n})")
        return self._query_float(f"X {n}")

    def set_da_output(self, n: int, voltage: float) -> None:
        """Set D/A output Xn (n = 5 or 6) to voltage in range +-10.238 V."""
        if n not in (5, 6):
            raise ValueError(f"D/A output n must be 5 or 6 (got {n})")
        if not (-10.238 <= voltage <= 10.238):
            raise ValueError(f"voltage {voltage} V outside +-10.238 V range")
        self._write(f"X {n},{voltage:.6E}")

    # ------------------------------------------------------------------
    # Status byte  (Y command)
    #
    # Bit definitions (SR530m.pdf §5):
    #   0  Not used
    #   1  Command parameter out of range
    #   2  No reference input detected
    #   3  PLL not locked to reference (UNLK)
    #   4  Signal overload (OVLD)
    #   5  Auto-offset out of range
    #   6  SRQ generated (GPIB only, always 0 on RS-232)
    #   7  Unrecognised command
    # ------------------------------------------------------------------

    def get_status(self) -> int:
        """Read and clear the status byte.  See bit definitions above."""
        return self._query_int("Y")

    def is_overloaded(self) -> bool:
        """True if the OVLD (signal overload) flag is set - status bit 4."""
        return bool(self.get_status() & 0x10)

    def is_unlocked(self) -> bool:
        """True if the UNLK (PLL not locked) flag is set - status bit 3."""
        return bool(self.get_status() & 0x08)

    def is_no_reference(self) -> bool:
        """True if no reference input is detected - status bit 2."""
        return bool(self.get_status() & 0x04)

    # ------------------------------------------------------------------
    # Pre-amplifier status  (H command, read-only)
    # ------------------------------------------------------------------

    def get_preamp_status(self) -> bool:
        """Return True if an SRS pre-amplifier is connected."""
        return bool(self._query_int("H"))

    # ------------------------------------------------------------------
    # Remote / local mode  (I command)
    # ------------------------------------------------------------------

    def set_remote_mode(self, mode: int) -> None:
        """Set remote/local mode.

        0 = Local (front panel active),
        1 = Remote (front panel locked; LOCAL key restores),
        2 = Lockout (front panel locked; only I,0 command restores).
        """
        if mode not in REMOTE_MODE_TABLE:
            raise ValueError("mode must be 0, 1, or 2")
        self._write(f"I {mode}")

    def get_remote_mode(self) -> int:
        """Query remote/local mode: 0=local, 1=remote, 2=lockout."""
        return self._query_int("I")

    # ------------------------------------------------------------------
    # Front-panel key simulation  (K command)
    # ------------------------------------------------------------------

    def send_key(self, n: int) -> None:
        """Simulate a front-panel key press.

        n must be 1-32.  See KEY_TABLE for the mapping.
        The effect is exactly the same as pressing the physical key once.
        """
        if not (1 <= n <= 32):
            raise ValueError(f"key number must be 1-32 (got {n})")
        self._write(f"K {n}")

    # ------------------------------------------------------------------
    # Auto-offset  (AX, AY, AR)
    # ------------------------------------------------------------------

    def auto_offset_x(self) -> None:
        """Execute auto-offset on the X output (sets X offset to null X)."""
        self._write("AX")

    def auto_offset_y(self) -> None:
        """Execute auto-offset on the Y output."""
        self._write("AY")

    def auto_offset_r(self) -> None:
        """Execute auto-offset on the R output."""
        self._write("AR")

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Reset all settings to factory defaults (Z command)."""
        self._write("Z")

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def get_x_volts(self) -> float:
        """Return the X input signal in real volts (fraction x sensitivity)."""
        return self.get_x_output() * self.get_sensitivity_volts()

    def snapshot(self) -> dict:
        """
        Read all key parameters in a single burst of serial queries.

        Returns
        -------
        dict with keys:
            x, y, r        - fraction of full-scale (+-1.0 / 0-1.0)
            x_v, y_v, r_v  - actual input signal in volts
            theta          - phase in degrees (computed from X and Y)
            frequency      - reference frequency in Hz
            phase          - reference phase shift setting in degrees
            sensitivity_idx - sensitivity index (1-24)
            sensitivity    - sensitivity label string
            sensitivity_v  - full-scale sensitivity in volts
            pre_tc_idx     - pre-filter TC index (1-11)
            pre_tc         - pre-filter TC label string
            post_tc_idx    - post-filter TC index (0-2)
            post_tc        - post-filter TC label string
            bandpass       - bandpass filter enabled (bool)
            line_notch     - line notch filter enabled (bool)
            line_2x_notch  - 2x line notch filter enabled (bool)
            status         - raw status byte
            overloaded     - bool, OVLD flag (bit 4)
            unlocked       - bool, UNLK flag (bit 3)
            no_reference   - bool, no reference flag (bit 2)
        """
        qx = self._query_float("QX")
        qy = self._query_float("QY")
        x = qx / _BNC_FULL_SCALE
        y = qy / _BNC_FULL_SCALE
        r = math.sqrt(x * x + y * y)
        theta = math.degrees(math.atan2(y, x))

        sens_idx = self.get_sensitivity()
        sens_v = sensitivity_index_to_volts(sens_idx)
        x_v = x * sens_v
        y_v = y * sens_v
        r_v = r * sens_v

        pre_tc_idx = self.get_pre_time_constant()
        post_tc_idx = self.get_post_time_constant()
        status = self.get_status()

        return {
            "x":              x,
            "y":              y,
            "r":              r,
            "x_v":            x_v,
            "y_v":            y_v,
            "r_v":            r_v,
            "theta":          theta,
            "frequency":      self.get_frequency(),
            "phase":          self.get_phase(),
            "sensitivity_idx": sens_idx,
            "sensitivity":    SENSITIVITY_TABLE.get(sens_idx, f"unknown ({sens_idx})"),
            "sensitivity_v":  sens_v,
            "pre_tc_idx":     pre_tc_idx,
            "pre_tc":         PRE_TIME_CONSTANT_TABLE.get(pre_tc_idx, "?"),
            "post_tc_idx":    post_tc_idx,
            "post_tc":        POST_TIME_CONSTANT_TABLE.get(post_tc_idx, "?"),
            "bandpass":       self.get_bandpass_filter(),
            "line_notch":     self.get_line_notch(),
            "line_2x_notch":  self.get_2x_line_notch(),
            "status":         status,
            "overloaded":     bool(status & 0x10),
            "unlocked":       bool(status & 0x08),
            "no_reference":   bool(status & 0x04),
        }

    def full_state(self) -> dict:
        """Read all instrument state including advanced settings.

        Superset of snapshot(): adds harmonic_mode, trigger_mode, enbw,
        display_select, expand_ch1, expand_ch2, remote_mode, preamp,
        offset_x, offset_y, offset_r.

        Takes roughly 2x as many serial queries as snapshot().
        Not suitable for fast polling; use snapshot() for the monitor loop.
        """
        base = self.snapshot()
        base.update({
            "harmonic_mode":    self.get_harmonic_mode(),
            "trigger_mode":     self.get_trigger_mode(),
            "enbw":             self.get_enbw(),
            "display_select":   self.get_display_select(),
            "expand_ch1":       self.get_expand(1),
            "expand_ch2":       self.get_expand(2),
            "remote_mode":      self.get_remote_mode(),
            "preamp":           self.get_preamp_status(),
            "offset_x":         self.get_offset_x_enabled(),
            "offset_y":         self.get_offset_y_enabled(),
            "offset_r":         self.get_offset_r_enabled(),
        })
        return base
