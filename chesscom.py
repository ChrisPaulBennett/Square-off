"""chess.com daily puzzle client (read-only public API).

chess.com's public Published-Data API is read-only, but it *does* expose the
daily puzzle, so this endpoint is fair game:

    GET https://api.chess.com/pub/puzzle
    -> { title, url, publish_time, fen, pgn, image }

`fen` is the puzzle's starting position; `pgn` is the solution line (your move,
the reply, your move, ...). We parse the PGN against the FEN into a list of
`chess.Move` so the app can check the solver's moves.
"""

from __future__ import annotations

import io

import chess
import chess.pgn
import requests

PUZZLE_URL = "https://api.chess.com/pub/puzzle"
# chess.com blocks requests without a User-Agent.
HEADERS = {"User-Agent": "SquareOff-Pi/1.0 (BLE chessboard controller)"}


def fetch_daily_puzzle(timeout: float = 15.0) -> dict:
    """Return the daily puzzle dict from chess.com."""
    r = requests.get(PUZZLE_URL, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()


def parse_solution(fen: str, pgn: str):
    """Parse the solution `pgn` (SAN) against `fen` into a list of chess.Move."""
    if not pgn:
        return []
    game = chess.pgn.read_game(io.StringIO(
        f'[FEN "{fen}"]\n[SetUp "1"]\n\n{pgn}\n'
    ))
    if game is None:
        return []
    return list(game.mainline_moves())

