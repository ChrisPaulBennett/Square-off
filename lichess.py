"""Minimal Lichess Board API client so the board can play a *real* online bot.

Unlike chess.com, Lichess exposes an API for playing games. This module
challenges the Lichess AI (Stockfish, levels 1-8) and relays moves both ways.

Requires a personal API token with the `board:play` scope:
    https://lichess.org/account/oauth/token/create

Docs: https://lichess.org/api#tag/Board
"""

from __future__ import annotations

import json

import requests

LICHESS = "https://lichess.org"


class LichessError(Exception):
    pass


class LichessOpponent:
    """Play a single game against the Lichess AI over the Board API.

    Flow:
        opp = LichessOpponent(token, level=4)
        first = opp.start("white")   # our colour; returns AI's move if it moved first
        ...on each of our moves...
        reply = opp.reply("e2e4")    # sends our move, returns the AI's reply (uci) or None
        opp.close()
    """

    def __init__(self, token: str, level: int = 4, timeout: float = 30.0):
        if not token:
            raise LichessError("A Lichess API token is required.")
        self.level = max(1, min(8, int(level)))
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token.strip()}"
        self.game_id = None
        self.username = None
        self._stream = None
        self._lines = None
        self._moves_seen = []

    # --- helpers ------------------------------------------------------------
    def _account(self):
        r = self.session.get(f"{LICHESS}/api/account", timeout=self.timeout)
        if r.status_code == 401:
            raise LichessError("Lichess rejected the token (needs board:play scope).")
        if r.status_code != 200:
            raise LichessError(f"Lichess /account failed: HTTP {r.status_code}")
        return r.json()

    def _next_event(self):
        """Return the next JSON object from the game stream (blocking)."""
        if self._lines is None:
            return None
        for raw in self._lines:
            if not raw:
                continue  # keep-alive newline
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                continue
        return None

    @staticmethod
    def _moves_of(event):
        if not event:
            return None
        if event.get("type") == "gameFull":
            return (event.get("state", {}) or {}).get("moves", "")
        if event.get("type") == "gameState":
            return event.get("moves", "")
        if "moves" in event:  # gameFull's embedded state, defensive
            return event.get("moves", "")
        return None

    @staticmethod
    def _status_of(event):
        if not event:
            return None
        if event.get("type") == "gameFull":
            return (event.get("state", {}) or {}).get("status")
        return event.get("status")

    # --- lifecycle ----------------------------------------------------------
    def start(self, human_color: str = "white"):
        acct = self._account()
        self.username = acct.get("username")
        color = "white" if human_color == "white" else "black"
        r = self.session.post(
            f"{LICHESS}/api/challenge/ai",
            data={"level": self.level, "color": color},
            timeout=self.timeout,
        )
        if r.status_code not in (200, 201):
            raise LichessError(f"Could not start AI game: HTTP {r.status_code} {r.text}")
        self.game_id = r.json()["id"]

        self._stream = self.session.get(
            f"{LICHESS}/api/board/game/stream/{self.game_id}",
            stream=True,
            timeout=(self.timeout, None),  # no read timeout on the stream
        )
        if self._stream.status_code != 200:
            raise LichessError(f"Could not open game stream: HTTP {self._stream.status_code}")
        self._lines = self._stream.iter_lines(decode_unicode=True)

        first = self._next_event()  # gameFull
        moves = (self._moves_of(first) or "").split()
        self._moves_seen = moves

        # If the AI is White and we're Black, wait for its first move.
        if human_color == "black" and not moves:
            while True:
                ev = self._next_event()
                if ev is None:
                    return None
                mv = (self._moves_of(ev) or "").split()
                if mv:
                    self._moves_seen = mv
                    return mv[-1]
        return moves[-1] if moves else None

    def reply(self, move_uci: str):
        """Send our move; return the AI's reply (uci) or None if the game ended."""
        if not self.game_id:
            raise LichessError("No active Lichess game.")
        r = self.session.post(
            f"{LICHESS}/api/board/game/{self.game_id}/move/{move_uci}",
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise LichessError(f"Lichess rejected move {move_uci}: {r.text}")

        while True:
            ev = self._next_event()
            if ev is None:
                return None
            status = self._status_of(ev)
            moves = self._moves_of(ev)
            if moves is not None:
                moves = moves.split()
                if len(moves) > len(self._moves_seen):
                    self._moves_seen = moves
                    if moves[-1] != move_uci:
                        return moves[-1]      # AI has replied
                    # only our move echoed so far; keep reading for the AI's
            if status and status not in ("started", "created"):
                return None                    # game finished

    def close(self):
        try:
            if self._stream is not None:
                self._stream.close()
        except Exception:
            pass
        self._stream = None
        self._lines = None

