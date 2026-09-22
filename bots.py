"""Chess "bots" for the Square Off board to play against.

There is **no public chess.com API for playing games or their bots** - their
public API is read-only (profiles, archives, ratings). So instead of pretending
to talk to chess.com, we generate opponent moves locally:

  * If a UCI engine (Stockfish by default) is available, we use it and cap its
    strength to emulate different bot difficulties.
  * Otherwise we fall back to a small built-in engine (material + light search)
    so the feature still works with nothing else installed.

The same interface could later be pointed at an online backend (e.g. the Lichess
Board/Bot API, which *does* allow programmatic play) - see README.
"""

from __future__ import annotations

import random
import shutil

import chess

try:
    import chess.engine  # noqa: F401
    _HAVE_ENGINE_MODULE = True
except Exception:  # pragma: no cover
    _HAVE_ENGINE_MODULE = False


# Named difficulty presets. `elo` targets Stockfish's UCI_LimitStrength; `depth`
# drives the built-in fallback. Ordered easiest -> hardest.
BOT_LEVELS = {
    "beginner":     {"elo": 800,  "skill": 0,  "movetime": 0.10, "depth": 1},
    "easy":         {"elo": 1100, "skill": 3,  "movetime": 0.10, "depth": 1},
    "intermediate": {"elo": 1400, "skill": 6,  "movetime": 0.20, "depth": 2},
    "advanced":     {"elo": 1700, "skill": 10, "movetime": 0.30, "depth": 2},
    "hard":         {"elo": 2000, "skill": 15, "movetime": 0.50, "depth": 3},
    "max":          {"elo": None, "skill": 20, "movetime": 1.00, "depth": 3},
}

PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}


def find_engine(explicit_path: str = None) -> str | None:
    """Return a path to a UCI engine binary, or None if not found."""
    if explicit_path:
        return explicit_path
    for name in ("stockfish", "lc0"):
        found = shutil.which(name)
        if found:
            return found
    return None


class Bot:
    """A chess opponent at a given difficulty level."""

    def __init__(self, level: str = "easy", engine_path: str = None):
        if level not in BOT_LEVELS:
            level = "easy"
        self.level = level
        self.cfg = BOT_LEVELS[level]
        self._engine = None
        self.engine_name = "built-in (fallback)"

        path = find_engine(engine_path) if _HAVE_ENGINE_MODULE else None
        if path:
            try:
                self._engine = chess.engine.SimpleEngine.popen_uci(path)
                self._configure_engine()
                self.engine_name = path
            except Exception as exc:  # pragma: no cover
                print(f"[bot] could not start engine {path}: {exc}; using fallback")
                self._engine = None

    def _configure_engine(self):
        opts = {}
        try:
            if self.cfg["elo"] is not None:
                opts["UCI_LimitStrength"] = True
                opts["UCI_Elo"] = self.cfg["elo"]
            else:
                opts["Skill Level"] = self.cfg["skill"]
            self._engine.configure(opts)
        except Exception:  # pragma: no cover - option names vary by engine
            try:
                self._engine.configure({"Skill Level": self.cfg["skill"]})
            except Exception:
                pass

    # --- move selection -----------------------------------------------------
    def select_move(self, board: chess.Board) -> chess.Move:
        if self._engine is not None:
            try:
                limit = chess.engine.Limit(time=self.cfg["movetime"])
                result = self._engine.play(board, limit)
                if result.move is not None:
                    return result.move
            except Exception as exc:  # pragma: no cover
                print(f"[bot] engine error: {exc}; using fallback move")
        return self._fallback_move(board)

    def _fallback_move(self, board: chess.Board) -> chess.Move:
        """Tiny negamax with material eval; randomised for lower levels."""
        depth = self.cfg["depth"]
        legal = list(board.legal_moves)
        if not legal:
            return None
        best_score = None
        best_moves = []
        for mv in legal:
            board.push(mv)
            score = -self._negamax(board, depth - 1)
            board.pop()
            if best_score is None or score > best_score:
                best_score = score
                best_moves = [mv]
            elif score == best_score:
                best_moves.append(mv)
        # Add randomness so weak bots feel human and don't always repeat lines.
        if self.level in ("beginner", "easy") and random.random() < 0.35:
            return random.choice(legal)
        return random.choice(best_moves)

    def _negamax(self, board: chess.Board, depth: int) -> int:
        if board.is_checkmate():
            return -100000
        if board.is_stalemate() or board.is_insufficient_material():
            return 0
        if depth <= 0:
            return self._evaluate(board)
        best = -10**9
        for mv in board.legal_moves:
            board.push(mv)
            best = max(best, -self._negamax(board, depth - 1))
            board.pop()
        return best

    def _evaluate(self, board: chess.Board) -> int:
        score = 0
        for piece_type, value in PIECE_VALUES.items():
            score += value * len(board.pieces(piece_type, chess.WHITE))
            score -= value * len(board.pieces(piece_type, chess.BLACK))
        return score if board.turn == chess.WHITE else -score

    def close(self):
        if self._engine is not None:
            try:
                self._engine.quit()
            except Exception:
                pass
            self._engine = None

