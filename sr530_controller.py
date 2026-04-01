"""
sr530_controller.py

RS232 driver for the Stanford Research Systems SR530 Lock-In Amplifier.

Requires a USB-to-RS232 adapter (DB-25 male → DB-9 female adapter likely
needed).  Default serial settings: 9600/8/N/2 (configurable via rear DIP
switches on the SR530 — see manual §6).

Usage:
    lia = SR530Controller("COM5")
    lia.connect()
    print(lia.get_frequency())
    print(lia.get_x_output())
    lia.set_sensitivity(12)       # 50 µV
    lia.set_phase(0.0)
    lia.disconnect()

Command reference sourced from SR530m.pdf (docs/).
If a command doesn't work, check the manual for your firmware revision.
"""

from __future__ import annotations

import time
from typing import Optional

import serial


# ---------------------------------------------------------------------------
# Sensitivity table  (index → label).
# Set with G<n>, query with G.
# Verify table against SR530m.pdf §5 "Sensitivity" if readings seem off.
# ---------------------------------------------------------------------------

SENSITIVITY_TABLE = {
    0:  "100 nV",
    1:  "200 nV",
    2:  "500 nV",
    3:  "1 µV",
    4:  "2 µV",
    5:  "5 µV",
    6:  "10 µV",
    7:  "20 µV",
    8:  "50 µV",
    9:  "100 µV",
    10: "200 µV",
    11: "500 µV",
    12: "1 mV",
    13: "2 mV",
    14: "5 mV",
    15: "10 mV",
    16: "20 mV",
    17: "50 mV",
    18: "100 mV",
    19: "200 mV",
    20: "500 mV",
    21: "1 V",
    22: "2 V",
    23: "5 V",
}

# ---------------------------------------------------------------------------
# Pre-filter time constant table  (index → label).
# Set with T1,<n>, query with T1.
# ---------------------------------------------------------------------------

TIME_CONSTANT_TABLE = {
    0:  "1 ms",
    1:  "3 ms",
    2:  "10 ms",
    3:  "30 ms",
    4:  "100 ms",
    5:  "300 ms",
    6:  "1 s",
    7:  "3 s",
    8:  "10 s",
    9:  "30 s",
    10: "100 s",
}


class SR530Controller:
    """
    RS232 interface to the SR530 Lock-In Amplifier.

    All commands are CR-terminated.  Responses are CR-LF terminated.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        timeout: float = 2.0,
    ):
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._ser: Optional[serial.Serial] = None

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
        """Read one response line (strips CR/LF)."""
        if not self.is_connected:
            raise RuntimeError("SR530 not connected")
        raw = self._ser.readline()
        return raw.decode("ascii", errors="replace").strip()

    def _query(self, cmd: str) -> str:
        """Send command, return response string."""
        self._write(cmd)
        return self._read_line()

    def _query_float(self, cmd: str) -> float:
        """Send command, return response as float."""
        return float(self._query(cmd))

    def _query_int(self, cmd: str) -> int:
        """Send command, return response as int."""
        return int(self._query(cmd))

    # ------------------------------------------------------------------
    # Output readings
    # ------------------------------------------------------------------

    def get_x_output(self) -> float:
        """Read the X (in-phase) output as a fraction of full-scale (±1.000).
        Multiply by the current sensitivity to get volts."""
        return self._query_float("Q1")

    def get_y_output(self) -> float:
        """Read the Y (quadrature) output as a fraction of full-scale."""
        return self._query_float("Q2")

    def get_r_output(self) -> float:
        """Read the R (magnitude) output as a fraction of full-scale."""
        return self._query_float("Q3")

    def get_theta_output(self) -> float:
        """Read the θ (phase) output in degrees."""
        return self._query_float("Q4")

    # ------------------------------------------------------------------
    # Reference / frequency
    # ------------------------------------------------------------------

    def get_frequency(self) -> float:
        """Read the current reference frequency in Hz."""
        return self._query_float("F")

    def set_phase(self, degrees: float) -> None:
        """Set the reference phase (−180.00 to +180.00°)."""
        self._write(f"P {degrees:.2f}")

    def get_phase(self) -> float:
        """Query the reference phase in degrees."""
        return self._query_float("P")

    # ------------------------------------------------------------------
    # Sensitivity
    # ------------------------------------------------------------------

    def set_sensitivity(self, index: int) -> None:
        """Set sensitivity by index (0–23).  See SENSITIVITY_TABLE."""
        if index not in SENSITIVITY_TABLE:
            raise ValueError(f"Invalid sensitivity index {index}")
        self._write(f"G {index}")

    def get_sensitivity(self) -> int:
        """Query current sensitivity index."""
        return self._query_int("G")

    def get_sensitivity_label(self) -> str:
        idx = self.get_sensitivity()
        return SENSITIVITY_TABLE.get(idx, f"unknown ({idx})")

    # ------------------------------------------------------------------
    # Time constant
    # ------------------------------------------------------------------

    def set_pre_time_constant(self, index: int) -> None:
        """Set pre-filter time constant by index (0–10).  See TIME_CONSTANT_TABLE."""
        if index not in TIME_CONSTANT_TABLE:
            raise ValueError(f"Invalid time constant index {index}")
        self._write(f"T1,{index}")

    def get_pre_time_constant(self) -> int:
        """Query pre-filter time constant index."""
        return self._query_int("T1")

    def set_post_time_constant(self, index: int) -> None:
        """Set post-filter time constant by index (0–10)."""
        if index not in TIME_CONSTANT_TABLE:
            raise ValueError(f"Invalid time constant index {index}")
        self._write(f"T2,{index}")

    def get_post_time_constant(self) -> int:
        return self._query_int("T2")

    # ------------------------------------------------------------------
    # Dynamic reserve
    # ------------------------------------------------------------------

    def set_dynamic_reserve(self, mode: int) -> None:
        """0 = Low Noise, 1 = Normal, 2 = High Reserve."""
        if mode not in (0, 1, 2):
            raise ValueError("Dynamic reserve mode must be 0, 1, or 2")
        self._write(f"D {mode}")

    def get_dynamic_reserve(self) -> int:
        return self._query_int("D")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> int:
        """Read status byte.  Bit meanings — see SR530m.pdf §5."""
        return self._query_int("Y")

    def is_overloaded(self) -> bool:
        """Check if the input is overloaded (bit 2 of status byte)."""
        return bool(self.get_status() & 0x04)

    def is_unlocked(self) -> bool:
        """Check if the reference is unlocked (bit 3 of status byte)."""
        return bool(self.get_status() & 0x08)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        """Read all key parameters at once."""
        return {
            "x": self.get_x_output(),
            "y": self.get_y_output(),
            "r": self.get_r_output(),
            "theta": self.get_theta_output(),
            "frequency": self.get_frequency(),
            "phase": self.get_phase(),
            "sensitivity": self.get_sensitivity_label(),
            "pre_tc": TIME_CONSTANT_TABLE.get(self.get_pre_time_constant(), "?"),
            "status": self.get_status(),
        }

    def get_x_volts(self) -> float:
        """Return the X output in real volts (fraction × sensitivity)."""
        sens_idx = self.get_sensitivity()
        # Convert sensitivity label to volts
        sens_volts = _sensitivity_index_to_volts(sens_idx)
        fraction = self.get_x_output()
        return fraction * sens_volts


# ---------------------------------------------------------------------------
# Helper: convert sensitivity index → volts
# ---------------------------------------------------------------------------

_SENS_VOLTS = [
    100e-9, 200e-9, 500e-9,        # 0-2:  nV
    1e-6, 2e-6, 5e-6,              # 3-5:  µV
    10e-6, 20e-6, 50e-6,           # 6-8:  µV
    100e-6, 200e-6, 500e-6,        # 9-11: µV
    1e-3, 2e-3, 5e-3,              # 12-14: mV
    10e-3, 20e-3, 50e-3,           # 15-17: mV
    100e-3, 200e-3, 500e-3,        # 18-20: mV
    1.0, 2.0, 5.0,                 # 21-23: V
]

def _sensitivity_index_to_volts(idx: int) -> float:
    if 0 <= idx < len(_SENS_VOLTS):
        return _SENS_VOLTS[idx]
    return 1.0  # safe fallback
