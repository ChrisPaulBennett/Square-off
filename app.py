#!/usr/bin/env python3
"""Web UI to play the Square Off board against a chess bot.

    python app.py                 # then open http://localhost:5000

Design
------
* BLE (bleak) is async, Flask is sync, so the board connection runs in a
  dedicated asyncio loop on a background thread. Flask request handlers talk to
  it through `BoardController.run(coro)` (thread-safe).
* `GameManager` holds the python-chess game, picks bot replies, and turns each
  bot move into physical motor actions (see chessmoves.physical_actions).
* Your own moves are made on the physical board; the firmware reports them as
  piece up/down events ('e2u'/'e4d') which we reconstruct into a legal move.
  You can also just make the move in the browser (handy with no board attached).

NOTE: chess.com has no public *play* API, so the opponent is generated locally
(Stockfish if installed, otherwise a small built-in engine). See README.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections import deque

import chess
from flask import Flask, jsonify, request, render_template

from squareoff import SquareOff
from bots import Bot, BOT_LEVELS
from chessmoves import physical_actions, plan_reset, plan_arrange
from lichess import LichessOpponent, LichessError
from chesscom import fetch_daily_puzzle, parse_solution
from graveyard import Graveyard


# --------------------------------------------------------------------------- #
# BLE board controller (background asyncio loop)                              #
# --------------------------------------------------------------------------- #
class BoardController:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self.board: SquareOff | None = None
        self.address = None
        self.last_status = None
        self.last_occupancy = None
        self.flipped = False               # 180° board orientation (colours swapped)
        self.status_listeners = []  # list of callables(text)

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro, timeout=60):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def set_flipped(self, value: bool):
        """Rotate the board mapping 180° (swap black/white sides)."""
        self.flipped = bool(value)
        if self.board is not None:
            self.board.flipped = self.flipped
        return self.flipped

    # --- connection ---------------------------------------------------------
    def scan(self, timeout=8.0):
        async def _scan():
            from bleak import BleakScanner
            devices = await BleakScanner.discover(timeout=timeout)
            out = []
            for d in devices:
                name = d.name or "(unknown)"
                low = name.lower().replace(" ", "")
                out.append({
                    "address": d.address,
                    "name": name,
                    "is_board": "squareoff" in low,
                })
            out.sort(key=lambda x: (not x["is_board"], x["name"]))
            return out
        return self.run(_scan(), timeout + 10)

    def connect(self, address):
        b = SquareOff(address)
        b.status_callback = self._on_status
        b.occupancy_callback = self._on_occupancy
        b.flipped = self.flipped
        self.run(b.connect(), 40)
        try:
            self.run(b.handshake(), 15)
        except Exception as exc:  # noqa: BLE001
            print(f"[app] handshake warning: {exc}")
        self.board = b
        self.address = address

    def disconnect(self):
        if self.board is not None:
            try:
                self.run(self.board.disconnect(), 15)
            finally:
                self.board = None
                self.address = None

    @property
    def connected(self):
        return self.board is not None and self.board.client.is_connected

    # --- events -------------------------------------------------------------
    def _on_status(self, text):
        self.last_status = text
        for fn in list(self.status_listeners):
            try:
                fn(text)
            except Exception as exc:  # noqa: BLE001
                print(f"[app] status listener error: {exc}")

    def _on_occupancy(self, occ):
        self.last_occupancy = occ

    # --- physical execution -------------------------------------------------
    def execute_actions(self, actions):
        """Run a list of chessmoves actions on the motor. Returns note strings."""
        notes = []
        if self.board is None:
            # Not connected: still surface hand-instructions so the UI shows them.
            for a in actions:
                if a[0] == "note":
                    notes.append(a[1])
            return notes
        for a in actions:
            kind = a[0]
            if kind == "move":
                self.run(self.board.move(a[1], a[2]))
            elif kind == "route":
                self.run(self.board.route_move(a[1], a[2]))
            elif kind == "capture":
                slot = a[3] if len(a) > 3 else None
                self.run(self.board.capture(a[1], a[2], slot))
            elif kind == "park":
                slot = a[2] if len(a) > 2 else None
                self.run(self.board.park_piece(a[1], slot))
            elif kind == "retrieve":
                self.run(self.board.retrieve_piece(a[1], a[2]))
            elif kind == "beep":
                self.run(self.board.beep(), timeout=15)
            elif kind == "path":
                self.run(self.board.send_path(a[1]))
            elif kind == "note":
                notes.append(a[1])
        return notes


# --------------------------------------------------------------------------- #
# Over-the-board human move detector (from piece up/down events)              #
# --------------------------------------------------------------------------- #
class MoveDetector:
    """Reconstruct a legal move from 'e2u'/'e4d' events.

    Motor-driven (bot) moves report only 'OK', not up/down, so these events
    correspond to the human physically moving pieces.
    """

    def __init__(self):
        self.lifted = []

    def reset(self):
        self.lifted = []

    def feed(self, text, board: chess.Board):
        if len(text) < 3 or text[-1] not in "ud":
            return None
        sq, action = text[:-1], text[-1]
        if action == "u":
            if sq not in self.lifted:
                self.lifted.append(sq)
            return None
        # a piece was placed on `sq`
        candidates = [
            mv for mv in board.legal_moves
            if chess.square_name(mv.to_square) == sq
            and chess.square_name(mv.from_square) in self.lifted
        ]
        if candidates:
            self.reset()
            # Prefer queen when a promotion is ambiguous.
            for mv in candidates:
                if mv.promotion == chess.QUEEN:
                    return mv
            return candidates[0]
        if sq in self.lifted:  # piece put back down where it came from
            self.lifted.remove(sq)
        return None


# --------------------------------------------------------------------------- #
# Game manager                                                                #
# --------------------------------------------------------------------------- #
class GameManager:
    def __init__(self, controller: BoardController):
        self.ctrl = controller
        self.lock = threading.RLock()
        self.board = chess.Board()
        self.mode = "local"                 # "local" or "lichess"
        self.bot: Bot | None = None
        self.opponent: LichessOpponent | None = None
        self.level = "easy"
        self.human_color = chess.WHITE
        self.active = False
        self.busy = False
        self.last_bot_move = None
        self.messages = deque(maxlen=40)
        self.detector = MoveDetector()
        self.grave = Graveyard()            # captured pieces -> type/colour slots
        # Puzzle mode
        self.puzzle = None                  # dict: title, url, image, fen
        self.solution = []                  # list[chess.Move]
        self.solution_idx = 0
        # Observers (used by the CLI / other front-ends)
        self.message_listeners = []         # fn(msg_str)
        self.update_listeners = []          # fn(state_dict)
        controller.status_listeners.append(self._on_status)

    # --- helpers ------------------------------------------------------------
    def _say(self, msg):
        self.messages.append(msg)
        for fn in list(self.message_listeners):
            try:
                fn(msg)
            except Exception:  # noqa: BLE001
                pass

    def _notify_update(self):
        st = self.state()
        for fn in list(self.update_listeners):
            try:
                fn(st)
            except Exception:  # noqa: BLE001
                pass


    def _result_text(self):
        outcome = self.board.outcome()
        if outcome is None:
            return "in progress"
        if outcome.winner is None:
            return f"draw ({outcome.termination.name.lower()})"
        return f"{'white' if outcome.winner else 'black'} wins ({outcome.termination.name.lower()})"

    def _parse_move(self, text):
        text = text.strip()
        for parser in (self.board.parse_uci, self.board.parse_san):
            try:
                return parser(text)
            except Exception:
                pass
        try:
            return self.board.parse_uci(text + "q")  # coord move needing promo
        except Exception:
            return None

    def _cleanup_opponents(self):
        if self.bot:
            self.bot.close()
            self.bot = None
        if self.opponent:
            self.opponent.close()
            self.opponent = None

    # --- lifecycle ----------------------------------------------------------
    def new_game(self, human_color="white", level="easy", mode="local",
                 lichess_token=None, lichess_level=4):
        with self.lock:
            self._cleanup_opponents()
            self.mode = "lichess" if mode == "lichess" else "local"
            self.human_color = chess.WHITE if human_color == "white" else chess.BLACK
            self.board = chess.Board()
            self.detector.reset()
            self.messages.clear()
            self.last_bot_move = None
            self.grave = Graveyard()
            self.busy = False
            self.puzzle = None
            self.solution = []
            self.solution_idx = 0
            if self.ctrl.board is not None:
                self.ctrl.board._parked_slots = set()

            if self.mode == "lichess":
                try:
                    self.opponent = LichessOpponent(lichess_token, lichess_level)
                    first = self.opponent.start(human_color)
                except LichessError as exc:
                    self.active = False
                    self._say(f"[lichess] {exc}")
                    return self.state(error=str(exc))
                self.active = True
                self._say(f"New Lichess game vs AI level {self.opponent.level} — "
                          f"you are {human_color} (as {self.opponent.username}).")
                if first:
                    try:
                        self._apply_opponent_move(self.board.parse_uci(first))
                    except Exception as exc:  # noqa: BLE001
                        self._say(f"[lichess] bad first move {first}: {exc}")
            else:
                self.level = level if level in BOT_LEVELS else "easy"
                self.bot = Bot(self.level)
                self.active = True
                self._say(f"New game — you are {human_color}, bot '{self.level}' "
                          f"[{self.bot.engine_name}].")
                if self.board.turn != self.human_color:
                    self._opponent_reply()
        self._notify_update()
        return self.state()

    def submit_move(self, text):
        """Entry point for both the UI and over-the-board detection."""
        with self.lock:
            if self.mode == "puzzle":
                st = self._submit_puzzle_move(text)
            else:
                st = self._submit_game_move(text)
            self._notify_update()
            return st

    def _submit_game_move(self, text):
        if not self.active:
            return self.state(error="No active game — start one first.")
        if self.board.turn != self.human_color:
            return self.state(error="It's not your turn.")
        move = self._parse_move(text)
        if move is None or move not in self.board.legal_moves:
            return self.state(error=f"Illegal move: {text}")
        self._say(f"You: {self.board.san(move)}")
        self.board.push(move)
        self.detector.reset()
        if self.board.is_game_over():
            self.active = False
            self._say("Game over — " + self._result_text())
            return self.state()
        self._opponent_reply(move.uci())
        return self.state()

    # --- puzzle mode --------------------------------------------------------
    def load_puzzle(self):
        with self.lock:
            try:
                data = fetch_daily_puzzle()
            except Exception as exc:  # noqa: BLE001
                return self.state(error=f"Could not fetch puzzle: {exc}")
            fen = data.get("fen")
            if not fen:
                return self.state(error="Puzzle response had no FEN.")
            try:
                solution = parse_solution(fen, data.get("pgn", ""))
            except Exception:
                solution = []

            self._cleanup_opponents()
            self.mode = "puzzle"
            self.puzzle = {
                "title": data.get("title"),
                "url": data.get("url"),
                "image": data.get("image"),
                "fen": fen,
            }
            self.board = chess.Board(fen)
            self.solution = solution
            self.solution_idx = 0
            self.human_color = self.board.turn
            self.grave = Graveyard()
            self.detector.reset()
            self.messages.clear()
            self.active = True
            if self.ctrl.board is not None:
                self.ctrl.board._parked_slots = set()

            side = "White" if self.human_color == chess.WHITE else "Black"
            self._say(f"Daily puzzle: {self.puzzle['title']} — {side} to move.")
            # Physically build the position. We assume the board starts from the
            # standard set; the safest workflow is to press Reset first.
            actions, notes = plan_arrange(chess.Board(), self.board, self.grave)
            self.busy = True
            try:
                exec_notes = self.ctrl.execute_actions(actions)
            except Exception as exc:  # noqa: BLE001
                exec_notes = []
                self._say(f"[board error] {exc}")
            finally:
                self.busy = False
            if self.ctrl.board is None:
                self._say("(Not connected — showing the puzzle only.)")
            else:
                self._say("Position set up. Make the best move on the board.")
            for n in notes + exec_notes:
                self._say(n)
            if not solution:
                self._say("(Solution unavailable — moves won't be auto-checked.)")
        self._notify_update()
        return self.state()

    def _submit_puzzle_move(self, text):
        if not self.active:
            return self.state(error="No active puzzle.")
        move = self._parse_move(text)
        if move is None or move not in self.board.legal_moves:
            return self.state(error=f"Illegal move: {text}")

        expected = (self.solution[self.solution_idx]
                    if self.solution_idx < len(self.solution) else None)
        if expected is not None and move != expected:
            # Wrong move: beep and put the piece back.
            self._say(f"✗ {self.board.san(move)} is not the solution — try again.")
            self._beep_and_restore(move)
            self.detector.reset()
            return self.state(error="Wrong move — piece returned.")

        # Correct (or unchecked): play it.
        self._say(f"✓ You: {self.board.san(move)}")
        self.board.push(move)
        self.solution_idx += 1
        self.detector.reset()
        if self.board.is_game_over() or self.solution_idx >= len(self.solution):
            self.active = False
            self._say("Puzzle solved! 🎉")
            return self.state()

        # Play the opponent's reply from the solution line.
        reply = self.solution[self.solution_idx]
        self._apply_opponent_move(reply)
        self.solution_idx += 1
        if self.solution_idx >= len(self.solution) or self.board.is_game_over():
            self.active = False
            self._say("Puzzle solved! 🎉")
        return self.state()

    def _beep_and_restore(self, move):
        """Beep and physically slide the wrongly-moved piece back. Holds lock."""
        frm = chess.square_name(move.from_square)
        to = chess.square_name(move.to_square)
        actions = [("beep",), ("route", to, frm)]
        self.busy = True
        try:
            self.ctrl.execute_actions(actions)
        except Exception as exc:  # noqa: BLE001
            self._say(f"[board error] {exc}")
        finally:
            self.busy = False
        if self.board.is_capture(move):
            self._say(f"(Also put the captured piece back on {to} by hand.)")

    def _opponent_reply(self, last_uci=None):
        """Get the opponent's move (engine or Lichess) and play it. Holds lock."""
        if self.mode == "lichess":
            try:
                uci = self.opponent.reply(last_uci)
            except LichessError as exc:
                self.active = False
                self._say(f"[lichess] {exc}")
                return
            if uci is None:
                self.active = False
                self._say("Game over — " + self._result_text())
                return
            try:
                move = self.board.parse_uci(uci)
            except Exception:
                self.active = False
                self._say(f"[lichess] unexpected move {uci}")
                return
        else:
            move = self.bot.select_move(self.board)
            if move is None:
                self.active = False
                self._say("Game over — " + self._result_text())
                return
        self._apply_opponent_move(move)

    def _apply_opponent_move(self, move):
        """Physically play the opponent's move and update state. Holds lock."""
        raw_actions = physical_actions(self.board, move)  # before pushing
        # Tag captures/en-passant parks with a graveyard slot + remember the piece
        # so the reset button can retrieve them later.
        actions = []
        for a in raw_actions:
            if a[0] == "capture":
                victim = self.board.piece_at(chess.parse_square(a[2]))
                slot = self.grave.alloc(victim) if victim else None
                actions.append(("capture", a[1], a[2], slot))
            elif a[0] == "park":
                victim = self.board.piece_at(chess.parse_square(a[1]))
                slot = self.grave.alloc(victim) if victim else None
                actions.append(("park", a[1], slot))
            else:
                actions.append(a)

        san = self.board.san(move)
        self.busy = True
        try:
            notes = self.ctrl.execute_actions(actions)
        except Exception as exc:  # noqa: BLE001
            notes = []
            self._say(f"[board error] {exc}")
        finally:
            self.busy = False
        self.board.push(move)
        self.last_bot_move = move.uci()
        self._say(f"Bot: {san}")
        for n in notes:
            self._say(n)
        if self.board.is_game_over():
            self.active = False
            self._say("Game over — " + self._result_text())

    def reset_board(self):
        """Physically restore the standard starting position (parking/retrieval)."""
        with self.lock:
            actions, notes = plan_reset(self.board, self.grave)
            self.busy = True
            try:
                exec_notes = self.ctrl.execute_actions(actions)
            except Exception as exc:  # noqa: BLE001
                exec_notes = []
                self._say(f"[board error] {exc}")
            finally:
                self.busy = False
            # Logical + physical bookkeeping back to a clean slate.
            self._cleanup_opponents()
            self.board = chess.Board()
            self.grave = Graveyard()
            self.puzzle = None
            self.solution = []
            self.solution_idx = 0
            self.mode = "local"
            if self.ctrl.board is not None:
                self.ctrl.board._parked_slots = set()
            self.detector.reset()
            self.active = False
            self._say("Board reset to the starting position.")
            for n in notes + exec_notes:
                self._say(n)
        self._notify_update()
        return self.state()

    # --- board event hook ---------------------------------------------------
    def _on_status(self, text):
        # Runs on the BLE thread. Only interpret up/down while it's the human's
        # turn and we're not mid bot-move.
        if not self.active or self.busy:
            return
        if self.board.turn != self.human_color:
            return
        move = self.detector.feed(text, self.board)
        if move is not None:
            # Offload to a worker thread so we don't block the BLE loop while the
            # bot's reply is physically executed.
            threading.Thread(
                target=self.submit_move,
                args=(move.uci(),),
                daemon=True,
            ).start()

    # --- serialisation ------------------------------------------------------
    def state(self, error=None):
        with self.lock:
            return {
                "connected": self.ctrl.connected,
                "address": self.ctrl.address,
                "flipped": self.ctrl.flipped,
                "mode": self.mode,
                "engine": (self.bot.engine_name if self.bot else
                           (f"Lichess AI L{self.opponent.level}" if self.opponent else None)),
                "levels": list(BOT_LEVELS.keys()),
                "active": self.active,
                "busy": self.busy,
                "fen": self.board.fen(),
                "turn": "white" if self.board.turn == chess.WHITE else "black",
                "human_color": "white" if self.human_color == chess.WHITE else "black",
                "your_turn": self.active and self.board.turn == self.human_color,
                "last_bot_move": self.last_bot_move,
                "in_check": self.board.is_check(),
                "game_over": self.board.is_game_over(),
                "result": self._result_text(),
                "captured": len(self.grave),
                "puzzle": self.puzzle,
                "puzzle_progress": (f"{self.solution_idx}/{len(self.solution)}"
                                    if self.mode == "puzzle" and self.solution else None),
                "messages": list(self.messages)[-14:],
                "last_status": self.ctrl.last_status,
                "error": error,
            }


# --------------------------------------------------------------------------- #
# Flask app                                                                   #
# --------------------------------------------------------------------------- #
# Resolve templates/ and static/ relative to THIS file so the server works no
# matter which directory you launch it from (fixes TemplateNotFound: index.html).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)
controller = BoardController()
game = GameManager(controller)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    return jsonify(game.state())


@app.route("/api/scan", methods=["POST"])
def api_scan():
    try:
        return jsonify({"devices": controller.scan()})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc), "devices": []}), 500


@app.route("/api/connect", methods=["POST"])
def api_connect():
    address = (request.json or {}).get("address", "").strip()
    if not address:
        return jsonify({"error": "address required"}), 400
    try:
        controller.connect(address)
        return jsonify(game.state())
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    controller.disconnect()
    return jsonify(game.state())


@app.route("/api/newgame", methods=["POST"])
def api_newgame():
    data = request.json or {}
    return jsonify(game.new_game(
        human_color=data.get("color", "white"),
        level=data.get("level", "easy"),
        mode=data.get("mode", "local"),
        lichess_token=data.get("lichess_token"),
        lichess_level=int(data.get("lichess_level", 4)),
    ))


@app.route("/api/reset", methods=["POST"])
def api_reset():
    return jsonify(game.reset_board())


@app.route("/api/flip", methods=["POST"])
def api_flip():
    data = request.get_json(silent=True) or {}
    value = data.get("flipped")
    controller.set_flipped(not controller.flipped if value is None else bool(value))
    return jsonify(game.state())


@app.route("/api/puzzle", methods=["POST"])
def api_puzzle():
    return jsonify(game.load_puzzle())


@app.route("/api/move", methods=["POST"])
def api_move():
    move = (request.json or {}).get("move", "").strip()
    if not move:
        return jsonify({"error": "move required"}), 400
    return jsonify(game.submit_move(move))


if __name__ == "__main__":
    import sys

    if "--cli" in sys.argv:
        # Text-mode front-end with the same features as the web UI.
        from cli import run_cli
        run_cli(controller, game)
    else:
        # threaded=True so /api/state polls aren't blocked while a move runs.
        app.run(host="0.0.0.0", port=5000, threaded=True)

