#!/usr/bin/env python3
"""Diagnostic probe for the Square Off parking/motion commands.

Sends ONE motion path to the board and reports whether the board stayed
connected, so we can pin down why parking disconnects it.

Examples:
    python park_probe.py --list                 # show the suggested test menu
    python park_probe.py "7,3:7,4|"             # simple move (baseline)
    python park_probe.py "7,3:8,3:9,3|"         # park off the right edge
    python park_probe.py --chunk20 "7,3:8,3:9,3|"   # reproduce the 20-byte-chunk bug
    python park_probe.py --beep                 # just beep
    python park_probe.py --address AA:BB:CC:DD:EE:FF "7,3:7,4|"

Tips:
    * Put a spare piece on the START square of the path before running
      (h4 = coordinate 7,3 for most of the suggested tests).
    * If a test disconnects the board, just run the next command again; if the
      board stops responding entirely, power-cycle it and continue.
"""

from __future__ import annotations

import argparse
import asyncio

from squareoff import SquareOff, find_board, MOVE_CHAR_UUID


SUGGESTED = [
    ("baseline move  (short, must work)", "7,3:7,4|"),
    ("knight-length path (24 bytes)",     "6,7:5.5,6.5:5.5,5.5:5,5|"),
    ("edge x=8, same rank",               "7,3:8,3|"),
    ("into graveyard x=9, y constant",    "7,3:8,3:9,3|"),
    ("corner transition (like the log)",  "7,3:7.5,3.5:8.5,3.5:9,3|"),
    ("current code's park (big y jump)",  "7,3:7,3.5:8.5,3.5:9,0|"),
    ("exact log RETRIEVAL string",        "9,4:8.5,4.5:6.5,4.5:6.5,6.5:5.92,7.08|"),
    ("negative coord (off bottom)",       "7,3:7,-1|"),
]


def print_menu():
    print("Suggested tests (place a piece on h4 = 7,3 first):\n")
    print("  # SINGLE-WRITE (the fix) — these should keep the board connected:")
    for i, (label, path) in enumerate(SUGGESTED):
        print(f'    python park_probe.py "{path}"'.ljust(58) + f"# {label}")
    print("\n  # CHUNKED (reproduces the bug) — long ones should DISCONNECT:")
    print('    python park_probe.py --chunk20 "7,3:8,3:9,3|"')
    print('    python park_probe.py --chunk20 "6,7:5.5,6.5:5.5,5.5:5,5|"')


async def run(args):
    address = args.address or await find_board(10.0)
    if not address:
        print("[!] No board found. Pass --address, or make sure it's on and not "
              "connected to the phone app.")
        return

    board = SquareOff(address)
    lost = {"flag": False}
    orig = board._on_disconnect

    def on_disc(client):
        lost["flag"] = True
        orig(client)

    board.client._disconnected_callback = on_disc  # best-effort hook

    await board.connect()
    try:
        await board.handshake()
    except Exception as exc:  # noqa: BLE001
        print(f"[!] handshake failed: {exc}")

    if args.beep:
        print("[probe] beeping…")
        try:
            await board.beep()
        except Exception as exc:  # noqa: BLE001
            print(f"[!] beep failed: {exc}")

    if args.path:
        payload = args.path.encode("ascii")
        how = f"CHUNKED({args.chunk20} bytes)" if args.chunk20 else "SINGLE write"
        print(f"[probe] sending {len(payload)} bytes as {how}:\n        {args.path}")
        try:
            if args.chunk20:
                size = args.chunk20
                for i in range(0, len(payload), size):
                    await board.client.write_gatt_char(
                        MOVE_CHAR_UUID, payload[i:i + size], response=True)
            else:
                await board.client.write_gatt_char(MOVE_CHAR_UUID, payload, response=True)
            if not args.no_settle and not args.chunk20:
                await board.state("S:po")
        except Exception as exc:  # noqa: BLE001
            print(f"[!] write raised: {exc}")

    print(f"[probe] waiting {args.wait:.0f}s for the board to react…")
    await asyncio.sleep(args.wait)

    connected = board.client.is_connected and not lost["flag"]
    print("\n==================== RESULT ====================")
    print(f"  still connected : {'YES ✅' if connected else 'NO ❌ (board dropped)'}")
    print("  -> tell me this result + whether the piece physically moved/parked.")
    print("===============================================")

    try:
        await board.disconnect()
    except Exception:
        pass


def main():
    p = argparse.ArgumentParser(description="Square Off parking/motion probe")
    p.add_argument("path", nargs="?", help="raw motion path, e.g. '7,3:8,3:9,3|'")
    p.add_argument("--address", help="BLE address (skip scanning)")
    p.add_argument("--beep", action="store_true", help="beep before sending")
    p.add_argument("--chunk20", nargs="?", type=int, const=20, default=0,
                   metavar="N", help="send in N-byte chunks (default 20) to reproduce the bug")
    p.add_argument("--no-settle", action="store_true", help="don't send S:po after the path")
    p.add_argument("--wait", type=float, default=6.0, help="seconds to observe after sending")
    p.add_argument("--list", action="store_true", help="print the suggested test menu and exit")
    args = p.parse_args()

    if args.list:
        print_menu()
        return
    if not args.path and not args.beep:
        p.print_help()
        return
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

