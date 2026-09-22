"""Type- and colour-aware parking bank for captured pieces.

Confirmed on hardware: there are two mirrored parking banks just off the board
edges, with slots labelled by piece type.

  Right bank : x=8 (inner), x=9 (outer)
  Left  bank : x=-1 (inner), x=-2 (outer)

Rank (y) is fixed per piece type on both columns of a bank:
  pawns  y=0,1,2,3   knight y=4   bishop y=5   rook y=6   queen y=7
  (two queen slots per bank -> one is the spare for a promotion).

Which colour uses which bank is set by `black_bank`. Evidence from the HCI snoop
(a captured black knight was retrieved from slot (9,4), i.e. the RIGHT bank) puts
black on the right by default; flip `black_bank='left'` if your board differs.
"""

from __future__ import annotations

import chess

# ranks used by each piece type (kings are never captured)
_TYPE_RANKS = {
    chess.PAWN:   (0, 1, 2, 3),
    chess.KNIGHT: (4,),
    chess.BISHOP: (5,),
    chess.ROOK:   (6,),
    chess.QUEEN:  (7,),
}
_RIGHT_COLS = (8, 9)     # inner, outer
_LEFT_COLS = (-1, -2)


def bank_columns(color: bool, black_bank: str = "right"):
    """Return the two slot columns (inner, outer) for a piece of `color`."""
    black_cols = _RIGHT_COLS if black_bank == "right" else _LEFT_COLS
    white_cols = _LEFT_COLS if black_bank == "right" else _RIGHT_COLS
    return black_cols if color == chess.BLACK else white_cols


def slots_for(piece: chess.Piece, black_bank: str = "right"):
    """All slots a given piece could occupy, inner column first."""
    cols = bank_columns(piece.color, black_bank)
    ranks = _TYPE_RANKS.get(piece.piece_type, (7,))
    return [(c, r) for c in cols for r in ranks]


class Graveyard:
    """Tracks which parking slots are occupied by which captured piece."""

    def __init__(self, black_bank: str = "right"):
        self.black_bank = black_bank
        self.occupied = {}            # (sx, sy) -> chess.Piece

    def alloc(self, piece: chess.Piece):
        """Reserve and return a free slot (sx, sy) for `piece`."""
        for slot in slots_for(piece, self.black_bank):
            if slot not in self.occupied:
                self.occupied[slot] = piece
                return slot
        # Overflow (e.g. a third same-type piece) - reuse the last valid slot.
        slot = slots_for(piece, self.black_bank)[-1]
        self.occupied[slot] = piece
        return slot

    def free(self, slot):
        self.occupied.pop(slot, None)

    def take(self, piece: chess.Piece):
        """Find and release a slot holding a matching piece; return it or None."""
        for slot, pc in list(self.occupied.items()):
            if pc == piece:
                del self.occupied[slot]
                return slot
        return None

    def __len__(self):
        return len(self.occupied)

