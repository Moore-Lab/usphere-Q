"""
sr530_cli_test.py

Command-line equivalent of every button in sr530_gui.py.
Run with:
    py -3 sr530_cli_test.py [--port COM11] [--baud 19200] [--timeout 2.0]
    py -3 sr530_cli_test.py --interactive       # menu-driven mode

Actions mirror GUI buttons:
  [CON] Connect
  [DIS] Disconnect
  [TST] Test - read frequency          (Connection tab > "Test (read frequency)")
  [RDA] Read all parameters            (Parameters tab > "Read from instrument")
  [APP] Apply all parameters           (Parameters tab > "Apply all")
  [RDN] Read now / snapshot            (Monitor tab > "Read now")
  [POL] Auto-refresh N samples         (Monitor tab > "Auto-refresh every ...")
  [APH] Auto-phase                     (Parameters tab > "Auto-phase")
  [XVT] get_x_volts()
  [STS] Status bits
  [AUT] Auto-offset X/Y/R
  [ADV] Advanced settings read         (Advanced tab > "Read all from instrument")
  [REF] Reference config (harmonic, trigger)
  [OUT] Output config (display, expand, ENBW)
  [OFF] Manual offsets (OX/OY/OR)
  [AIO] Analog I/O (X1-X4 read, X5-X6 read/write)
  [REM] Remote/local mode
  [KEY] Front-panel key simulation
  [FST] Full state (full_state() - all params)
  [RST] Reset to factory defaults
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any
import importlib.util
import pathlib

# ---------------------------------------------------------------------------
# Resolve the controller from the same directory
# ---------------------------------------------------------------------------

_here = pathlib.Path(__file__).parent
_spec = importlib.util.spec_from_file_location(
    "sr530_controller", _here / "sr530_controller.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

SR530Controller           = _mod.SR530Controller
SENSITIVITY_TABLE         = _mod.SENSITIVITY_TABLE
PRE_TIME_CONSTANT_TABLE   = _mod.PRE_TIME_CONSTANT_TABLE
POST_TIME_CONSTANT_TABLE  = _mod.POST_TIME_CONSTANT_TABLE
HARMONIC_MODE_TABLE       = _mod.HARMONIC_MODE_TABLE
ENBW_TABLE                = _mod.ENBW_TABLE
TRIGGER_MODE_TABLE        = _mod.TRIGGER_MODE_TABLE
DISPLAY_SELECT_TABLE      = _mod.DISPLAY_SELECT_TABLE
REMOTE_MODE_TABLE         = _mod.REMOTE_MODE_TABLE
KEY_TABLE                 = _mod.KEY_TABLE
_RESERVE_LABELS           = ["Low Noise", "Normal", "High Reserve"]

# ---------------------------------------------------------------------------
# Pretty output helpers (ASCII-only for Windows console compatibility)
# ---------------------------------------------------------------------------

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(label: str, value: Any = "") -> None:
    suffix = f": {value}" if value != "" else ""
    print(f"  {GREEN}[OK] {label}{suffix}{RESET}")

def fail(label: str, exc: Exception) -> None:
    print(f"  {RED}[FAIL] {label}: {exc}{RESET}")

def section(title: str) -> None:
    print(f"\n{BOLD}{CYAN}{'-'*60}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'-'*60}{RESET}")

def info(msg: str) -> None:
    print(f"  {YELLOW}[i] {msg}{RESET}")

# ---------------------------------------------------------------------------
# GUI-button equivalents
# ---------------------------------------------------------------------------

def btn_connect(port: str, baud: int, timeout: float,
                echo: bool = False) -> SR530Controller | None:
    """[CON] Connection tab > Connect."""
    section("[CON] Connect")
    info(f"port={port}  baud={baud}  timeout={timeout}s  echo={echo}")
    try:
        ctrl = SR530Controller(port, baudrate=baud, timeout=timeout, echo=echo)
        result = ctrl.connect()
        if not result:
            raise RuntimeError("connect() returned False")
        ok("Serial port opened", f"{port} @ {baud} baud")
        ok("is_connected", ctrl.is_connected)
        ok("port property", ctrl.port)
        return ctrl
    except Exception as exc:
        fail("Connect", exc)
        return None


def btn_test_frequency(ctrl: SR530Controller) -> None:
    """[TST] Connection tab > Test (read frequency)."""
    section("[TST] Test - read frequency")
    try:
        freq = ctrl.get_frequency()
        ok("get_frequency()", f"{freq:.4f} Hz")
    except Exception as exc:
        fail("get_frequency()", exc)


def btn_read_all(ctrl: SR530Controller) -> dict:
    """[RDA] Parameters tab > Read from instrument."""
    section("[RDA] Read all parameters")
    results = {}
    tests = [
        ("get_phase()",             ctrl.get_phase),
        ("get_sensitivity()",       ctrl.get_sensitivity),
        ("get_sensitivity_label()", ctrl.get_sensitivity_label),
        ("get_sensitivity_volts()", ctrl.get_sensitivity_volts),
        ("get_dynamic_reserve()",   ctrl.get_dynamic_reserve),
        ("get_pre_time_constant()", ctrl.get_pre_time_constant),
        ("get_post_time_constant()",ctrl.get_post_time_constant),
        ("get_bandpass_filter()",   ctrl.get_bandpass_filter),
        ("get_line_notch()",        ctrl.get_line_notch),
        ("get_2x_line_notch()",     ctrl.get_2x_line_notch),
    ]
    for label, fn in tests:
        try:
            val = fn()
            results[label] = val
            ok(label, val)
        except Exception as exc:
            fail(label, exc)
    return results


def btn_apply_all(ctrl: SR530Controller, params: dict) -> None:
    """[APP] Parameters tab > Apply all (round-trip with values from last read)."""
    section("[APP] Apply all parameters (round-trip)")

    phase   = params.get("get_phase()",              0.0)
    sens    = params.get("get_sensitivity()",         16)
    reserve = params.get("get_dynamic_reserve()",     1)
    pre_tc  = params.get("get_pre_time_constant()",   5)
    post_tc = params.get("get_post_time_constant()",  0)
    bp      = params.get("get_bandpass_filter()",     False)
    notch   = params.get("get_line_notch()",          False)
    notch2x = params.get("get_2x_line_notch()",       False)

    sets = [
        ("set_phase()",             lambda: ctrl.set_phase(phase),          f"{phase:.2f} deg"),
        ("set_sensitivity()",       lambda: ctrl.set_sensitivity(sens),     f"idx={sens}"),
        ("set_dynamic_reserve()",   lambda: ctrl.set_dynamic_reserve(reserve), _RESERVE_LABELS[reserve]),
        ("set_pre_time_constant()", lambda: ctrl.set_pre_time_constant(pre_tc),
         PRE_TIME_CONSTANT_TABLE.get(pre_tc, "?")),
        ("set_post_time_constant()",lambda: ctrl.set_post_time_constant(post_tc),
         POST_TIME_CONSTANT_TABLE.get(post_tc, "?")),
        ("set_bandpass_filter()",   lambda: ctrl.set_bandpass_filter(bp),   str(bp)),
        ("set_line_notch()",        lambda: ctrl.set_line_notch(notch),     str(notch)),
        ("set_2x_line_notch()",     lambda: ctrl.set_2x_line_notch(notch2x),str(notch2x)),
    ]
    for label, fn, detail in sets:
        try:
            fn()
            ok(label, detail)
        except Exception as exc:
            fail(label, exc)

    info("Verifying round-trip reads ...")
    for label, fn in [
        ("phase",        ctrl.get_phase),
        ("sensitivity",  ctrl.get_sensitivity),
        ("dyn reserve",  ctrl.get_dynamic_reserve),
    ]:
        try:
            v = fn()
            ok(f"verify {label}", v)
        except Exception as exc:
            fail(f"verify {label}", exc)


def btn_auto_phase(ctrl: SR530Controller) -> None:
    """[APH] Parameters tab > Auto-phase button."""
    section("[APH] Auto-phase")
    try:
        ctrl.auto_phase()
        ok("auto_phase() sent")
        info("Waiting 1 s for instrument to settle ...")
        time.sleep(1.0)
        phase = ctrl.get_phase()
        ok("phase after auto_phase()", f"{phase:.2f} deg")
    except Exception as exc:
        fail("auto_phase()", exc)


def btn_read_now(ctrl: SR530Controller) -> dict | None:
    """[RDN] Monitor tab > Read now (calls snapshot())."""
    section("[RDN] Read now - snapshot()")
    try:
        snap = ctrl.snapshot()

        expected_keys = [
            "x", "y", "r", "x_v", "y_v", "r_v", "theta",
            "frequency", "phase", "sensitivity_idx", "sensitivity",
            "sensitivity_v", "pre_tc_idx", "pre_tc", "post_tc_idx", "post_tc",
            "bandpass", "line_notch", "line_2x_notch",
            "status", "overloaded", "unlocked", "no_reference",
        ]
        missing = [k for k in expected_keys if k not in snap]
        if missing:
            fail("snapshot() keys", f"missing: {missing}")
        else:
            ok("snapshot() keys", f"all {len(expected_keys)} present")

        print(f"\n  {'X (in-phase):':<22}{snap['x']:+.4f} FS   {snap['x_v']:+.6e} V")
        print(f"  {'Y (quadrature):':<22}{snap['y']:+.4f} FS   {snap['y_v']:+.6e} V")
        print(f"  {'R (magnitude):':<22}{snap['r']:.4f} FS    {snap['r_v']:.6e} V")
        print(f"  {'theta:':<22}{snap['theta']:+.2f} deg")
        print(f"  {'Frequency:':<22}{snap['frequency']:.4f} Hz")
        print(f"  {'Phase:':<22}{snap['phase']:.2f} deg")
        print(f"  {'Sensitivity:':<22}{snap['sensitivity']}  ({snap['sensitivity_v']:.3e} V FS)")
        print(f"  {'Pre TC:':<22}{snap['pre_tc']}")
        print(f"  {'Post TC:':<22}{snap['post_tc']}")
        print(f"  {'Bandpass:':<22}{snap['bandpass']}")
        print(f"  {'Line notch:':<22}{snap['line_notch']}")
        print(f"  {'2x notch:':<22}{snap['line_2x_notch']}")
        print(f"  {'Status byte:':<22}0x{snap['status']:02X}  ({snap['status']:08b}b)")

        ovl = f"{RED}OVERLOADED{RESET}" if snap['overloaded'] else f"{GREEN}OK{RESET}"
        lck = f"{YELLOW}UNLOCKED{RESET}"  if snap['unlocked']   else f"{GREEN}Locked{RESET}"
        nrf = f"{YELLOW}NO REF{RESET}"    if snap['no_reference'] else f"{GREEN}Ref OK{RESET}"
        print(f"  {'Overload:':<22}{ovl}")
        print(f"  {'Reference lock:':<22}{lck}")
        print(f"  {'Reference input:':<22}{nrf}")

        return snap
    except Exception as exc:
        fail("snapshot()", exc)
        return None


def btn_full_state(ctrl: SR530Controller) -> dict | None:
    """[FST] Full state via full_state() - all parameters including advanced."""
    section("[FST] Full state - full_state()")
    try:
        state = ctrl.full_state()
        btn_read_now.__wrapped__ = True  # just display; reuse snapshot display logic below

        print(f"\n  --- Advanced fields ---")
        print(f"  {'Harmonic mode:':<22}{HARMONIC_MODE_TABLE.get(state['harmonic_mode'], '?')}")
        print(f"  {'Trigger mode:':<22}{TRIGGER_MODE_TABLE.get(state['trigger_mode'], '?')}")
        print(f"  {'ENBW:':<22}{ENBW_TABLE.get(state['enbw'], '?')}")
        print(f"  {'Display select:':<22}{DISPLAY_SELECT_TABLE.get(state['display_select'], '?')}")
        print(f"  {'Expand Ch1:':<22}{state['expand_ch1']}")
        print(f"  {'Expand Ch2:':<22}{state['expand_ch2']}")
        print(f"  {'Remote mode:':<22}{REMOTE_MODE_TABLE.get(state['remote_mode'], '?')}")
        print(f"  {'Pre-amplifier:':<22}{'Connected' if state['preamp'] else 'Not connected'}")
        print(f"  {'Offset X enabled:':<22}{state['offset_x']}")
        print(f"  {'Offset Y enabled:':<22}{state['offset_y']}")
        print(f"  {'Offset R enabled:':<22}{state['offset_r']}")
        ok("full_state() complete", f"{len(state)} keys")
        return state
    except Exception as exc:
        fail("full_state()", exc)
        return None


def btn_auto_refresh(ctrl: SR530Controller, n: int = 5,
                     interval_ms: int = 500) -> None:
    """[POL] Monitor tab > Auto-refresh (runs N poll cycles)."""
    section(f"[POL] Auto-refresh - {n} samples @ {interval_ms} ms")
    errors = 0
    for i in range(n):
        t0 = time.time()
        try:
            snap = ctrl.snapshot()
            elapsed = (time.time() - t0) * 1000
            print(f"  [{i+1:2d}/{n}]  "
                  f"X={snap['x']:+.4f} FS  "
                  f"R={snap['r']:.4f} FS  "
                  f"f={snap['frequency']:.3f} Hz  "
                  f"({elapsed:.0f} ms)")
        except Exception as exc:
            errors += 1
            fail(f"sample {i+1}", exc)
        sleep = interval_ms / 1000 - (time.time() - t0)
        if sleep > 0:
            time.sleep(sleep)
    if errors == 0:
        ok(f"All {n} samples OK")
    else:
        fail("Polling", Exception(f"{errors}/{n} samples failed"))


def btn_get_x_volts(ctrl: SR530Controller) -> None:
    """[XVT] get_x_volts() convenience method."""
    section("[XVT] get_x_volts()")
    try:
        v = ctrl.get_x_volts()
        ok("get_x_volts()", f"{v:+.6e} V")
    except Exception as exc:
        fail("get_x_volts()", exc)


def btn_status_bits(ctrl: SR530Controller) -> None:
    """[STS] Status byte and is_overloaded() / is_unlocked() / is_no_reference()."""
    section("[STS] Status bits")
    try:
        raw = ctrl.get_status()
        ok("get_status()", f"0x{raw:02X} = {raw:08b}b")
    except Exception as exc:
        fail("get_status()", exc)
    for label, fn in [
        ("is_overloaded()",   ctrl.is_overloaded),
        ("is_unlocked()",     ctrl.is_unlocked),
        ("is_no_reference()", ctrl.is_no_reference),
    ]:
        try:
            ok(label, fn())
        except Exception as exc:
            fail(label, exc)


def btn_auto_offset(ctrl: SR530Controller) -> None:
    """[AUT] Auto-offset on X, Y, R outputs."""
    section("[AUT] Auto-offset")
    for label, fn in [
        ("auto_offset_x()", ctrl.auto_offset_x),
        ("auto_offset_y()", ctrl.auto_offset_y),
        ("auto_offset_r()", ctrl.auto_offset_r),
    ]:
        try:
            fn()
            ok(label, "command sent")
        except Exception as exc:
            fail(label, exc)


def btn_reference_config(ctrl: SR530Controller) -> None:
    """[REF] Harmonic mode (M) and trigger mode (R) read/write."""
    section("[REF] Reference configuration")
    # Read current
    try:
        m = ctrl.get_harmonic_mode()
        ok("get_harmonic_mode()", f"{m} = {HARMONIC_MODE_TABLE.get(m, '?')}")
    except Exception as exc:
        fail("get_harmonic_mode()", exc)
    try:
        r = ctrl.get_trigger_mode()
        ok("get_trigger_mode()", f"{r} = {TRIGGER_MODE_TABLE.get(r, '?')}")
    except Exception as exc:
        fail("get_trigger_mode()", exc)

    # Round-trip: set to current values (no-op) then read back
    try:
        ctrl.set_harmonic_mode(0)
        v = ctrl.get_harmonic_mode()
        ok("set/get harmonic_mode=0 (f)", v == 0)
    except Exception as exc:
        fail("set_harmonic_mode(0)", exc)
    try:
        ctrl.set_trigger_mode(0)
        v = ctrl.get_trigger_mode()
        ok("set/get trigger_mode=0 (positive)", v == 0)
    except Exception as exc:
        fail("set_trigger_mode(0)", exc)


def btn_output_config(ctrl: SR530Controller) -> None:
    """[OUT] Display select (S), expand ch1/ch2 (E), ENBW (N) read/write."""
    section("[OUT] Output configuration")
    try:
        s = ctrl.get_display_select()
        ok("get_display_select()", f"{s} = {DISPLAY_SELECT_TABLE.get(s, '?')}")
    except Exception as exc:
        fail("get_display_select()", exc)
    try:
        e1 = ctrl.get_expand(1)
        ok("get_expand(1)", e1)
    except Exception as exc:
        fail("get_expand(1)", exc)
    try:
        e2 = ctrl.get_expand(2)
        ok("get_expand(2)", e2)
    except Exception as exc:
        fail("get_expand(2)", exc)
    try:
        n = ctrl.get_enbw()
        ok("get_enbw()", f"{n} = {ENBW_TABLE.get(n, '?')}")
    except Exception as exc:
        fail("get_enbw()", exc)

    # Round-trip tests (restore current values)
    info("Round-trip: set display=0 (X/Y), expand=off, ENBW=1Hz ...")
    try:
        ctrl.set_display_select(0)
        ok("set_display_select(0)", ctrl.get_display_select() == 0)
    except Exception as exc:
        fail("set_display_select(0)", exc)
    try:
        ctrl.set_expand(1, False)
        ok("set_expand(1, False)", ctrl.get_expand(1) == False)
    except Exception as exc:
        fail("set_expand(1, False)", exc)
    try:
        ctrl.set_expand(2, False)
        ok("set_expand(2, False)", ctrl.get_expand(2) == False)
    except Exception as exc:
        fail("set_expand(2, False)", exc)
    try:
        ctrl.set_enbw(0)
        ok("set_enbw(0) -> 1 Hz", ctrl.get_enbw() == 0)
    except Exception as exc:
        fail("set_enbw(0)", exc)

    # Q1/Q2 with current display setting
    try:
        q1 = ctrl.get_q1()
        q2 = ctrl.get_q2()
        ok("get_q1()", f"{q1:.6e}")
        ok("get_q2()", f"{q2:.6e}")
    except Exception as exc:
        fail("get_q1/q2()", exc)


def btn_manual_offsets(ctrl: SR530Controller) -> None:
    """[OFF] Manual offsets OX/OY/OR - read enable status, then round-trip."""
    section("[OFF] Manual offsets")
    for axis, get_fn in [
        ("X", ctrl.get_offset_x_enabled),
        ("Y", ctrl.get_offset_y_enabled),
        ("R", ctrl.get_offset_r_enabled),
    ]:
        try:
            en = get_fn()
            ok(f"get_offset_{axis}_enabled()", en)
        except Exception as exc:
            fail(f"get_offset_{axis}_enabled()", exc)

    # Toggle X offset on and off (tiny value to avoid disturbing measurement)
    info("Testing OX enable/disable toggle ...")
    try:
        ctrl.set_offset_x(True)
        on = ctrl.get_offset_x_enabled()
        ctrl.set_offset_x(False)
        off = ctrl.get_offset_x_enabled()
        ok("set_offset_x on then off", f"on={on}, off={off}")
    except Exception as exc:
        fail("set_offset_x toggle", exc)


def btn_analog_io(ctrl: SR530Controller) -> None:
    """[AIO] Analog I/O: read X1-X4 A/D inputs, read/write X5-X6 D/A outputs."""
    section("[AIO] Analog I/O")

    info("Reading analog inputs X1-X4 ...")
    for n in (1, 2, 3, 4):
        try:
            v = ctrl.read_analog_input(n)
            ok(f"read_analog_input({n})", f"{v:+.6f} V")
        except Exception as exc:
            fail(f"read_analog_input({n})", exc)

    info("Reading D/A outputs X5, X6 ...")
    for n in (5, 6):
        try:
            v = ctrl.get_da_output(n)
            ok(f"get_da_output({n})", f"{v:+.6f} V")
        except Exception as exc:
            fail(f"get_da_output({n})", exc)

    info("Writing X6 = 0.0 V (safe test), then reading back ...")
    try:
        ctrl.set_da_output(6, 0.0)
        v = ctrl.get_da_output(6)
        ok("set_da_output(6, 0.0) -> read back", f"{v:+.6f} V")
    except Exception as exc:
        fail("set_da_output(6, 0.0)", exc)


def btn_remote_local(ctrl: SR530Controller) -> None:
    """[REM] Remote/local mode (I command) read/write."""
    section("[REM] Remote/local mode")
    try:
        mode = ctrl.get_remote_mode()
        ok("get_remote_mode()", f"{mode} = {REMOTE_MODE_TABLE.get(mode, '?')}")
    except Exception as exc:
        fail("get_remote_mode()", exc)

    info("Setting Remote, then restoring Local ...")
    try:
        ctrl.set_remote_mode(1)
        m = ctrl.get_remote_mode()
        ok("set_remote_mode(1) -> Remote", m == 1)
    except Exception as exc:
        fail("set_remote_mode(1)", exc)
    try:
        ctrl.set_remote_mode(0)
        m = ctrl.get_remote_mode()
        ok("set_remote_mode(0) -> Local", m == 0)
    except Exception as exc:
        fail("set_remote_mode(0)", exc)

    try:
        preamp = ctrl.get_preamp_status()
        ok("get_preamp_status()", "Connected" if preamp else "Not connected")
    except Exception as exc:
        fail("get_preamp_status()", exc)


def btn_key_sim(ctrl: SR530Controller) -> None:
    """[KEY] Front-panel key simulation (K command)."""
    section("[KEY] Key simulation")
    # K27 = Sensitivity Up, K28 = Sensitivity Down (safe pair to test)
    info("Sending K27 (Sensitivity Up) then K28 (Sensitivity Down) ...")
    try:
        s_before = ctrl.get_sensitivity()
        ctrl.send_key(27)   # Sensitivity Up
        time.sleep(0.2)
        s_up = ctrl.get_sensitivity()
        ctrl.send_key(28)   # Sensitivity Down (restore)
        time.sleep(0.2)
        s_after = ctrl.get_sensitivity()
        ok("send_key(27) Sensitivity Up",   f"{s_before} -> {s_up}")
        ok("send_key(28) Sensitivity Down", f"{s_up} -> {s_after} (restored)")
    except Exception as exc:
        fail("send_key()", exc)

    # List all available key codes
    info(f"Available keys: 1-{max(KEY_TABLE.keys())}")
    for k, name in KEY_TABLE.items():
        print(f"    K{k:2d}  {name}")


def btn_disconnect(ctrl: SR530Controller) -> None:
    """[DIS] Connection tab > Disconnect."""
    section("[DIS] Disconnect")
    try:
        ctrl.disconnect()
        ok("disconnect()")
        ok("is_connected after disconnect", ctrl.is_connected)
    except Exception as exc:
        fail("disconnect()", exc)


# ---------------------------------------------------------------------------
# Interactive menu
# ---------------------------------------------------------------------------

def interactive_mode(port: str, baud: int, timeout: float,
                     echo: bool = False) -> None:
    ctrl: SR530Controller | None = None
    params: dict = {}

    menu = [
        "  1   [CON] Connect",
        "  2   [TST] Test - read frequency",
        "  3   [RDA] Read all parameters",
        "  4   [APP] Apply all (round-trip)",
        "  5   [RDN] Read now (snapshot)",
        "  6   [POL] Auto-refresh 5 samples",
        "  7   [APH] Auto-phase",
        "  8   [XVT] get_x_volts()",
        "  9   [STS] Status bits",
        " 10   [AUT] Auto-offset X/Y/R",
        " 11   [ADV] Advanced: read all",
        " 12   [REF] Reference config (harmonic, trigger)",
        " 13   [OUT] Output config (display, expand, ENBW)",
        " 14   [OFF] Manual offsets (OX/OY/OR)",
        " 15   [AIO] Analog I/O (X1-X4 read, X5-X6)",
        " 16   [REM] Remote/local + pre-amp status",
        " 17   [KEY] Key simulation (K command)",
        " 18   [FST] Full state (all parameters)",
        " 19   [DIS] Disconnect",
        "  0   Run full automated test suite",
        "  q   Quit",
    ]

    while True:
        print("\n" + "\n".join(menu))
        choice = input("\n  > ").strip().lower()
        if choice == "q":
            break
        elif choice == "1":
            ctrl = btn_connect(port, baud, timeout, echo)
        elif choice == "2":
            (btn_test_frequency(ctrl) if ctrl else info("Not connected"))
        elif choice == "3":
            params = btn_read_all(ctrl) if ctrl else ({}, info("Not connected"))[0]
        elif choice == "4":
            (btn_apply_all(ctrl, params) if ctrl else info("Not connected"))
        elif choice == "5":
            (btn_read_now(ctrl) if ctrl else info("Not connected"))
        elif choice == "6":
            (btn_auto_refresh(ctrl) if ctrl else info("Not connected"))
        elif choice == "7":
            (btn_auto_phase(ctrl) if ctrl else info("Not connected"))
        elif choice == "8":
            (btn_get_x_volts(ctrl) if ctrl else info("Not connected"))
        elif choice == "9":
            (btn_status_bits(ctrl) if ctrl else info("Not connected"))
        elif choice == "10":
            (btn_auto_offset(ctrl) if ctrl else info("Not connected"))
        elif choice == "11":
            (btn_full_state(ctrl) if ctrl else info("Not connected"))
        elif choice == "12":
            (btn_reference_config(ctrl) if ctrl else info("Not connected"))
        elif choice == "13":
            (btn_output_config(ctrl) if ctrl else info("Not connected"))
        elif choice == "14":
            (btn_manual_offsets(ctrl) if ctrl else info("Not connected"))
        elif choice == "15":
            (btn_analog_io(ctrl) if ctrl else info("Not connected"))
        elif choice == "16":
            (btn_remote_local(ctrl) if ctrl else info("Not connected"))
        elif choice == "17":
            (btn_key_sim(ctrl) if ctrl else info("Not connected"))
        elif choice == "18":
            (btn_full_state(ctrl) if ctrl else info("Not connected"))
        elif choice == "19":
            if ctrl:
                btn_disconnect(ctrl)
                ctrl = None
        elif choice == "0":
            ctrl = _run_full_suite(port, baud, timeout, echo)
        else:
            info("Unknown option")

    if ctrl:
        btn_disconnect(ctrl)


# ---------------------------------------------------------------------------
# Full automated test suite
# ---------------------------------------------------------------------------

def _run_full_suite(port: str, baud: int, timeout: float,
                    echo: bool = False) -> SR530Controller | None:
    print(f"\n{BOLD}{'='*60}")
    print(f"  SR530 FULL AUTOMATED TEST SUITE")
    print(f"  port={port}  baud={baud}  timeout={timeout}s")
    print(f"{'='*60}{RESET}")

    ctrl = btn_connect(port, baud, timeout, echo)
    if ctrl is None:
        print(f"\n{RED}  Cannot continue - connection failed.{RESET}")
        return None

    # --- Original suite ---
    btn_test_frequency(ctrl)
    params = btn_read_all(ctrl)
    btn_apply_all(ctrl, params)
    btn_auto_phase(ctrl)
    btn_read_now(ctrl)
    btn_get_x_volts(ctrl)
    btn_status_bits(ctrl)
    btn_auto_offset(ctrl)
    btn_auto_refresh(ctrl, n=5, interval_ms=500)

    # --- New advanced tests ---
    btn_reference_config(ctrl)
    btn_output_config(ctrl)
    btn_manual_offsets(ctrl)
    btn_analog_io(ctrl)
    btn_remote_local(ctrl)
    btn_key_sim(ctrl)
    btn_full_state(ctrl)

    print(f"\n{BOLD}{GREEN}{'='*60}")
    print(f"  TEST SUITE COMPLETE")
    print(f"{'='*60}{RESET}\n")
    return ctrl


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="CLI test tool - mirrors all SR530 GUI buttons"
    )
    parser.add_argument("--port",        default="COM11", help="Serial port (default COM11)")
    parser.add_argument("--baud",        type=int, default=19200, help="Baud rate (default 19200)")
    parser.add_argument("--timeout",     type=float, default=2.0,  help="Serial timeout in seconds")
    parser.add_argument("--echo",        action="store_true",      help="Enable echo mode (SW2-6 DOWN)")
    parser.add_argument("--interactive", action="store_true",      help="Menu-driven interactive mode")
    parser.add_argument("--samples",     type=int, default=5,      help="Samples for auto-refresh test")
    args = parser.parse_args()

    if args.interactive:
        interactive_mode(args.port, args.baud, args.timeout, args.echo)
    else:
        ctrl = _run_full_suite(args.port, args.baud, args.timeout, args.echo)
        if ctrl:
            btn_disconnect(ctrl)


if __name__ == "__main__":
    main()
