#!/usr/bin/env python3
"""Command-line front-end for the Square Off board.

Exposes every feature of the web UI from a text prompt, driving the very same
BoardController + GameManager that `app.py` uses (no duplicated logic):

    python cli.py            # or:  python app.py --cli

Connection : scan, connect, disconnect, status
Play a bot : newgame [white|black] [level], lichess <token> [level] [color]
Moves      : move <uci|SAN>            (also detected automatically over the board)
Board ops  : reset, puzzle
Low level  : handshake, beep, battery, led <sqs>, path <x,y:..|>, raw <id> <data>
Misc       : show, help, quit
"""

from __future__ import annotations

import chess

from bots import BOT_LEVELS


BANNER = r"""
  ____                            ___   __  __
 / ___|  __ _ _   _  __ _ _ __ __/ _ \ / _|/ _|
 \___ \ / _` | | | |/ _` | '__/ _ \ | | |_| |_
  ___) | (_| | |_| | (_| | | |  __/ |_| |  _|  _|
 |____/ \__, |\__,_|\__,_|_|  \___|\___/|_| |_|    command line
           |_|
Type 'help' for commands.
"""

HELP = """
Connection:
  scan                     list nearby BLE devices
  connect [idx|address]    connect (idx from last scan, or a MAC address)
  disconnect               disconnect from the board
  status                   show connection + game status

Play a bot:
  newgame [white|black] [level]   start a local-engine game
                                  levels: {levels}
  lichess <token> [level] [color] play the Lichess AI (level 1-8)
  move <uci|SAN>                  make your move (e.g. 'move e2e4' or 'move Nf3')

Board:
  reset                    physically re-home all pieces to the start position
  puzzle                   load & set up the chess.com daily puzzle
  flip                     swap black/white sides (rotate the board 180°)

Low-level board commands (must be connected):
  handshake                put a SWAP board into the ready state
  beep                     make the board beep
  battery                  request battery status
  led <squares>            light LEDs, e.g. 'led e2e4'
  path <x,y:...|>          send a raw motion path
  raw <id> <data>          send a raw '<id>#<data>*' message

Misc:
  show                     print the board and status
  help                     this help
  quit / exit              leave
""".format(levels=", ".join(BOT_LEVELS.keys()))


def _fmt_board(st):
    board = chess.Board(st["fen"])
    # Orient from the side the human is playing.
    text = str(board)
    if st.get("human_color") == "black":
        text = "\n".join(reversed([" ".join(reversed(r.split()))
                                   for r in text.splitlines()]))
    return text


def _status_line(st):
    bits = [f"[{st['mode']}]"]
    if st["active"]:
        bits.append(f"{st['turn']} to move" + (" (you)" if st["your_turn"] else " (bot)"))
    else:
        bits.append("no active game")
    if st.get("in_check"):
        bits.append("CHECK")
    if st.get("puzzle_progress"):
        bits.append(f"puzzle {st['puzzle_progress']}")
    if st.get("game_over"):
        bits.append(st.get("result", "game over"))
    conn = ("connected " + (st.get("address") or "")) if st["connected"] else "disconnected"
    bits.append(conn)
    return " | ".join(bits)


class CLI:
    def __init__(self, controller, game):
        self.controller = controller
        self.game = game
        self.devices = []
        game.message_listeners.append(self._on_message)
        game.update_listeners.append(self._on_update)

    # --- observers (also fire for over-the-board moves) ---------------------
    def _on_message(self, msg):
        print(f"  {msg}")

    def _on_update(self, st):
        print("\n" + _fmt_board(st))
        print(_status_line(st))
        print("squareoff> ", end="", flush=True)

    # --- helpers ------------------------------------------------------------
    def _need_board(self):
        if self.controller.board is None:
            print("  ! not connected — use 'scan' then 'connect'.")
            return False
        return True

    def _run(self, coro, timeout=60):
        return self.controller.run(coro, timeout)

    def _print_err(self, st):
        if st and st.get("error"):
            print(f"  ! {st['error']}")

    # --- command handlers ---------------------------------------------------
    def cmd_scan(self, args):
        print("  scanning…")
        self.devices = self.controller.scan()
        if not self.devices:
            print("  (no devices found)")
        for i, d in enumerate(self.devices):
            mark = " <- Square Off" if d["is_board"] else ""
            print(f"  [{i}] {d['address']}  {d['name']}{mark}")

    def cmd_connect(self, args):
        if not args:
            board = next((d for d in self.devices if d["is_board"]), None)
            if not board:
                print("  ! give an index or address (run 'scan' first).")
                return
            address = board["address"]
        elif args[0].isdigit() and int(args[0]) < len(self.devices):
            address = self.devices[int(args[0])]["address"]
        else:
            address = args[0]
        print(f"  connecting to {address} …")
        self.controller.connect(address)
        print(f"  connected: {self.controller.connected}")

    def cmd_disconnect(self, args):
        self.controller.disconnect()
        print("  disconnected.")

    def cmd_status(self, args):
        self._on_update(self.game.state())

    def cmd_show(self, args):
        self._on_update(self.game.state())

    def cmd_newgame(self, args):
        color = "white"
        level = "easy"
        for a in args:
            if a.lower() in ("white", "black"):
                color = a.lower()
            elif a.lower() in BOT_LEVELS:
                level = a.lower()
        self._print_err(self.game.new_game(color, level, mode="local"))

    def cmd_lichess(self, args):
        if not args:
            print("  usage: lichess <token> [level 1-8] [white|black]")
            return
        token = args[0]
        level = 4
        color = "white"
        for a in args[1:]:
            if a.lower() in ("white", "black"):
                color = a.lower()
            elif a.isdigit():
                level = int(a)
        self._print_err(self.game.new_game(
            color, "easy", mode="lichess", lichess_token=token, lichess_level=level))

    def cmd_move(self, args):
        if not args:
            print("  usage: move e2e4  (or SAN like Nf3)")
            return
        self._print_err(self.game.submit_move(" ".join(args)))

    def cmd_reset(self, args):
        self._print_err(self.game.reset_board())

    def cmd_puzzle(self, args):
        self._print_err(self.game.load_puzzle())

    def cmd_flip(self, args):
        flipped = self.controller.set_flipped(not self.controller.flipped)
        print(f"  board sides {'SWAPPED (rotated 180°)' if flipped else 'normal'}.")

    # low-level -------------------------------------------------------------
    def cmd_handshake(self, args):
        if self._need_board():
            self._run(self.controller.board.handshake(), 20)
            print("  handshake sent.")

    def cmd_beep(self, args):
        if self._need_board():
            self._run(self.controller.board.beep(), 15)
            print("  beep!")

    def cmd_battery(self, args):
        if self._need_board():
            self._run(self.controller.board.battery())

    def cmd_led(self, args):
        if not args:
            print("  usage: led e2e4")
            return
        if self._need_board():
            self._run(self.controller.board.leds("".join(args)))

    def cmd_path(self, args):
        if not args:
            print("  usage: path 4,6:4,5|")
            return
        raw = "".join(args)
        if not raw.endswith("|"):
            raw += "|"
        if self._need_board():
            self._run(self.controller.board.send_path(raw.encode("ascii")))

    def cmd_raw(self, args):
        if not args:
            print("  usage: raw <id> [data]")
            return
        if self._need_board():
            cid = int(args[0])
            data = args[1] if len(args) > 1 else ""
            self._run(self.controller.board.send(cid, data))

    # --- REPL ---------------------------------------------------------------
    def run(self):
        print(BANNER)
        handlers = {
            "scan": self.cmd_scan, "connect": self.cmd_connect,
            "disconnect": self.cmd_disconnect, "status": self.cmd_status,
            "show": self.cmd_show, "newgame": self.cmd_newgame,
            "lichess": self.cmd_lichess, "move": self.cmd_move,
            "reset": self.cmd_reset, "puzzle": self.cmd_puzzle,
            "flip": self.cmd_flip,
            "handshake": self.cmd_handshake, "beep": self.cmd_beep,
            "battery": self.cmd_battery, "led": self.cmd_led,
            "path": self.cmd_path, "raw": self.cmd_raw,
        }
        while True:
            try:
                line = input("squareoff> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            parts = line.split()
            cmd, args = parts[0].lower(), parts[1:]
            if cmd in ("quit", "exit"):
                break
            if cmd == "help":
                print(HELP)
                continue
            handler = handlers.get(cmd)
            if handler is None:
                print("  ? unknown command — type 'help'.")
                continue
            try:
                handler(args)
            except Exception as exc:  # noqa: BLE001 - keep the REPL alive
                print(f"  [error] {exc}")

        print("  disconnecting…")
        try:
            self.controller.disconnect()
        except Exception:
            pass


def run_cli(controller, game):
    CLI(controller, game).run()


if __name__ == "__main__":
    import app  # module import (does not start the Flask server)
    run_cli(app.controller, app.game)

