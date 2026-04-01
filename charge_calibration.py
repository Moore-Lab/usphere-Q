"""
charge_calibration.py

Calibration data model and workflow for usphere charge measurement.

Two calibration modes:
    File-based  — uses checkQ.run_calibration() on known-charge H5 data
    Lock-in     — simple volts-per-electron from known charge states

The CalibrationStore class manages the JSON file and provides lookup.
All logic is GUI-independent.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Default calibration file
# ---------------------------------------------------------------------------

DEFAULT_CAL_FILE = str(
    Path(__file__).parent / "resources"
    / "Microsphere-Utility-Scripts" / "checkQ_calibration.json"
)


# ---------------------------------------------------------------------------
# CalibrationStore — manages the JSON calibration file
# ---------------------------------------------------------------------------

class CalibrationStore:
    """
    Reads / writes calibration entries from a JSON file.

    Each entry is keyed by (sphere_diameter_um, drive_frequency_hz) and
    optionally by source_type ('file' or 'lockin').

    File format:
        {
          "calibrations": [ { ... }, { ... } ],
          "lockin_calibrations": [ { ... }, { ... } ]
        }
    """

    def __init__(self, filepath: str = DEFAULT_CAL_FILE):
        self._filepath = filepath
        self._data: dict = {"calibrations": [], "lockin_calibrations": []}
        if os.path.exists(filepath):
            with open(filepath, "r") as f:
                self._data = json.load(f)
            # Ensure both keys exist
            self._data.setdefault("calibrations", [])
            self._data.setdefault("lockin_calibrations", [])

    # ------------------------------------------------------------------
    # File-based (checkQ) calibrations
    # ------------------------------------------------------------------

    def lookup_file_cal(
        self,
        sphere_diameter_um: float,
        drive_frequency_hz: float,
        freq_tolerance: float = 0.5,
    ) -> Optional[dict]:
        """Return the most recent matching file-based calibration, or None."""
        matches = [
            e for e in self._data["calibrations"]
            if e["sphere_diameter_um"] == sphere_diameter_um
            and abs(e["drive_frequency_hz"] - drive_frequency_hz) < freq_tolerance
        ]
        if not matches:
            return None
        matches.sort(key=lambda x: x.get("calibration_date", ""), reverse=True)
        return matches[0]

    def save_file_cal(self, cal: dict, freq_tolerance: float = 0.5) -> None:
        """Write or overwrite a file-based calibration entry."""
        cals = self._data["calibrations"]
        for i, e in enumerate(cals):
            if (e["sphere_diameter_um"] == cal["sphere_diameter_um"]
                    and abs(e["drive_frequency_hz"] - cal["drive_frequency_hz"])
                    < freq_tolerance):
                cals[i] = cal
                self._write()
                return
        cals.append(cal)
        self._write()

    # ------------------------------------------------------------------
    # Lock-in calibrations
    # ------------------------------------------------------------------

    def lookup_lockin_cal(
        self,
        sphere_diameter_um: float,
        drive_frequency_hz: float,
        freq_tolerance: float = 0.5,
    ) -> Optional[dict]:
        """Return the most recent matching lock-in calibration, or None."""
        matches = [
            e for e in self._data["lockin_calibrations"]
            if e["sphere_diameter_um"] == sphere_diameter_um
            and abs(e["drive_frequency_hz"] - drive_frequency_hz) < freq_tolerance
        ]
        if not matches:
            return None
        matches.sort(key=lambda x: x.get("calibration_date", ""), reverse=True)
        return matches[0]

    def save_lockin_cal(self, cal: dict, freq_tolerance: float = 0.5) -> None:
        """Write or overwrite a lock-in calibration entry."""
        cals = self._data["lockin_calibrations"]
        for i, e in enumerate(cals):
            if (e["sphere_diameter_um"] == cal["sphere_diameter_um"]
                    and abs(e["drive_frequency_hz"] - cal["drive_frequency_hz"])
                    < freq_tolerance):
                cals[i] = cal
                self._write()
                return
        cals.append(cal)
        self._write()

    def make_lockin_cal(
        self,
        sphere_diameter_um: float,
        drive_frequency_hz: float,
        volts_per_electron: float,
        sr530_sensitivity_idx: int = -1,
        sr530_phase: float = 0.0,
        notes: str = "",
    ) -> dict:
        """Create a lock-in calibration dict."""
        return {
            "sphere_diameter_um": sphere_diameter_um,
            "drive_frequency_hz": drive_frequency_hz,
            "volts_per_electron": volts_per_electron,
            "sr530_sensitivity_idx": sr530_sensitivity_idx,
            "sr530_phase": sr530_phase,
            "calibration_date": str(date.today()),
            "notes": notes,
        }

    # ------------------------------------------------------------------
    # List all entries
    # ------------------------------------------------------------------

    def list_file_cals(self) -> list[dict]:
        return list(self._data["calibrations"])

    def list_lockin_cals(self) -> list[dict]:
        return list(self._data["lockin_calibrations"])

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _write(self) -> None:
        with open(self._filepath, "w") as f:
            json.dump(self._data, f, indent=2)


# ---------------------------------------------------------------------------
# Convenience: run file-based calibration via checkQ
# ---------------------------------------------------------------------------

def run_file_calibration(
    data_dir: str,
    sphere_diameter_um: float,
    calibration_file: str = DEFAULT_CAL_FILE,
    position_channel: int = 0,
    drive_channel: int = 10,
    polarity: str = "positive",
    n_charges: int = 1,
) -> dict:
    """
    Run a file-based calibration and save the result.

    Parameters
    ----------
    data_dir : str
        Directory containing H5 files from a known-charge-state run.
    sphere_diameter_um : float
        Microsphere diameter in microns.
    calibration_file : str
        Path to the JSON calibration file.
    position_channel, drive_channel : int
        DAQ column indices.
    polarity : str
        'positive' or 'negative' — the known polarity during calibration.
    n_charges : int
        Number of charges on the sphere during calibration data.

    Returns
    -------
    dict
        The calibration entry that was saved.
    """
    import sys
    _SCRIPTS_PATH = str(
        Path(__file__).parent / "resources" / "Microsphere-Utility-Scripts"
    )
    if _SCRIPTS_PATH not in sys.path:
        sys.path.insert(0, _SCRIPTS_PATH)

    import checkQ as cq

    cols = [position_channel, drive_channel]
    cal = cq.run_calibration(
        data_dir, cols, sphere_diameter_um,
        polarity=polarity, n_charges=n_charges,
    )

    store = CalibrationStore(calibration_file)
    store.save_file_cal(cal)
    return cal


# ---------------------------------------------------------------------------
# Convenience: compute volts-per-electron from known charge state
# ---------------------------------------------------------------------------

def calibrate_lockin_from_voltage(
    measured_voltage: float,
    known_charge: int,
    sphere_diameter_um: float,
    drive_frequency_hz: float,
    calibration_file: str = DEFAULT_CAL_FILE,
    sr530_sensitivity_idx: int = -1,
    sr530_phase: float = 0.0,
) -> dict:
    """
    Calibrate the lock-in by providing a known voltage at a known charge state.

    Parameters
    ----------
    measured_voltage : float
        SR530 X output voltage at the known charge state.
    known_charge : int
        Number of charges (signed) on the sphere.
    sphere_diameter_um : float
        Microsphere diameter in microns.
    drive_frequency_hz : float
        Drive frequency in Hz.

    Returns
    -------
    dict
        The calibration entry that was saved.
    """
    if known_charge == 0:
        raise ValueError("Cannot calibrate with zero charge")

    volts_per_electron = measured_voltage / known_charge

    store = CalibrationStore(calibration_file)
    cal = store.make_lockin_cal(
        sphere_diameter_um=sphere_diameter_um,
        drive_frequency_hz=drive_frequency_hz,
        volts_per_electron=volts_per_electron,
        sr530_sensitivity_idx=sr530_sensitivity_idx,
        sr530_phase=sr530_phase,
        notes=f"Calibrated at {known_charge}e, V={measured_voltage:.6f}V",
    )
    store.save_lockin_cal(cal)
    return cal
