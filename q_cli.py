"""
q_cli.py  —  usphere-Q terminal interface

Connects to a running q_server.py and sends commands.

One-shot usage::

    python q_cli.py ping
    python q_cli.py get_charge
    python q_cli.py set_target 0 0.5
    python q_cli.py start_control
    python q_cli.py stop_control
    python q_cli.py flash 2.0
    python q_cli.py heat 3.0
    python q_cli.py stop_actuators
    python q_cli.py add_rule -5 5 0 0.5
    python q_cli.py clear_rules
    python q_cli.py connect_flashlamp GPIB::2
    python q_cli.py connect_filament  GPIB::3
    python q_cli.py get_status
    python q_cli.py get_config

Interactive REPL::

    python q_cli.py --interactive
    python q_cli.py -i

Global options::

    --host HOST    server hostname (default: localhost)
    --rep PORT     REP port (default: 5554)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from zmq_base import ModuleClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print_reply(reply: dict) -> None:
    status = reply.get("status", "?")
    if status == "error":
        print(f"ERROR: {reply.get('message', reply)}")
    else:
        data = reply.get("data")
        if data is None:
            print("OK")
        elif isinstance(data, dict):
            print(json.dumps(data, indent=2, default=str))
        else:
            print(data)


def _client(host: str, rep_port: int) -> ModuleClient:
    return ModuleClient("q", rep_port=rep_port, pub_port=rep_port + 1,
                        host=host, timeout_ms=5000)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _run_one(client: ModuleClient, tokens: list[str]) -> bool:
    if not tokens:
        return True

    cmd = tokens[0].lower()

    if cmd in ("quit", "exit", "q"):
        return False

    if cmd == "help":
        print(__doc__)
        return True

    if cmd == "ping":
        print("ONLINE" if client.ping() else "OFFLINE")
        return True

    if cmd == "get_state":
        _print_reply(client.send("get_state"))
        return True

    if cmd == "get_status":
        _print_reply(client.send("get_status"))
        return True

    if cmd == "get_config":
        _print_reply(client.send("get_config"))
        return True

    if cmd == "get_charge":
        _print_reply(client.send("get_charge"))
        return True

    if cmd == "update_charge":
        # update_charge <charge_e>
        if len(tokens) < 2:
            print("Usage: update_charge <charge_e>")
            return True
        _print_reply(client.send("update_charge", charge_e=float(tokens[1])))
        return True

    if cmd == "set_target":
        # set_target <charge_e> [tolerance]
        if len(tokens) < 2:
            print("Usage: set_target <charge_e> [tolerance]")
            return True
        kwargs: dict = {"charge_e": float(tokens[1])}
        if len(tokens) >= 3:
            kwargs["tolerance"] = float(tokens[2])
        _print_reply(client.send("set_target", **kwargs))
        return True

    if cmd == "set_timing":
        # set_timing flash=N heat=M settle=K
        kwargs = {}
        for tok in tokens[1:]:
            if tok.startswith("flash="):
                kwargs["flash_duration_s"] = float(tok.split("=")[1])
            elif tok.startswith("heat="):
                kwargs["heat_duration_s"] = float(tok.split("=")[1])
            elif tok.startswith("settle="):
                kwargs["settle_time_s"] = float(tok.split("=")[1])
        if not kwargs:
            print("Usage: set_timing flash=<s> heat=<s> settle=<s>")
            return True
        _print_reply(client.send("set_timing", **kwargs))
        return True

    if cmd == "start_control":
        _print_reply(client.send("start_control"))
        return True

    if cmd == "stop_control":
        _print_reply(client.send("stop_control"))
        return True

    if cmd == "flash":
        duration = float(tokens[1]) if len(tokens) >= 2 else 2.0
        _print_reply(client.send("flash", duration_s=duration))
        return True

    if cmd == "heat":
        duration = float(tokens[1]) if len(tokens) >= 2 else 3.0
        _print_reply(client.send("heat", duration_s=duration))
        return True

    if cmd == "stop_actuators":
        _print_reply(client.send("stop_actuators"))
        return True

    if cmd == "add_rule":
        # add_rule <lower> <upper> <target_charge> [tolerance] [name]
        if len(tokens) < 4:
            print("Usage: add_rule <lower> <upper> <target_charge> [tolerance] [name]")
            return True
        kwargs = {
            "lower":         float(tokens[1]),
            "upper":         float(tokens[2]),
            "target_charge": float(tokens[3]),
        }
        if len(tokens) >= 5:
            kwargs["tolerance"] = float(tokens[4])
        if len(tokens) >= 6:
            kwargs["name"] = tokens[5]
        _print_reply(client.send("add_rule", **kwargs))
        return True

    if cmd == "clear_rules":
        _print_reply(client.send("clear_rules"))
        return True

    if cmd == "connect_flashlamp":
        port = tokens[1] if len(tokens) >= 2 else "GPIB::2"
        _print_reply(client.send("connect_flashlamp", port=port))
        return True

    if cmd == "connect_filament":
        port = tokens[1] if len(tokens) >= 2 else "GPIB::3"
        _print_reply(client.send("connect_filament", port=port))
        return True

    if cmd == "disconnect_actuators":
        _print_reply(client.send("disconnect_actuators"))
        return True

    # Fall through
    print(f"Sending raw command {cmd!r} ...")
    _print_reply(client.send(cmd))
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="usphere-Q terminal interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--rep",  type=int, default=5554)
    parser.add_argument("-i", "--interactive", action="store_true")
    parser.add_argument("command", nargs="*")
    args = parser.parse_args()

    client = _client(args.host, args.rep)

    if args.interactive or not args.command:
        print(f"q-cli  connected to {args.host}:{args.rep}")
        print("Type 'help' for commands, 'quit' to exit.\n")
        while True:
            try:
                line = input("q> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if not _run_one(client, line.split()):
                break
    else:
        _run_one(client, args.command)

    client.close()


if __name__ == "__main__":
    main()
