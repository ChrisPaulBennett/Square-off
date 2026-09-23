"""Colour-aware parking bank for captured pieces.

Confirmed on hardware: there are two parking banks just off the board edges.

  Right bank : x=8 (inner), x=9 (outer)
  Left  bank : x=-1 (inner), x=-2 (outer)

Each player parks the pieces they capture on the bank to THEIR right, so captures
group by colour (black -> right bank, white -> left bank; set by `black_bank`).
Within a bank pieces are stacked sequentially in capture order: inner column
first, filling low-to-high from that player's own side (ranks ascend on the right
bank near the white player, descend on the left bank near the black player). The
motor (alloc) and hand captures (alloc_human) use this same layout so they agree.

Retrieval matches by piece type+colour (see Graveyard.take), so the exact slot a
piece sits in only needs to be consistent, which it is.

`slots_for()` / `_TYPE_RANKS` below describe an older type-segregated pocket
layout; they're kept for reference but are no longer used by allocation.
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
    """All slots a given piece could occupy, inner column first.

    The two banks are point-symmetric (a 180° mirror of each other): the right
    bank sits at x=8,9 with pawns near y=0..3, while the left bank at x=-1,-2 is
    its mirror image, so its ranks run the other way (y -> 7-y). Without this
    flip, white pieces (which use the left bank by default) get routed to the
    wrong slot even though black parks correctly on the right.
    """
    cols = bank_columns(piece.color, black_bank)
    ranks = _TYPE_RANKS.get(piece.piece_type, (7,))
    if cols[0] < 0:  # left bank -> mirror the rank layout
        ranks = tuple(7 - r for r in ranks)
    return [(c, r) for c in cols for r in ranks]


class Graveyard:
    """Tracks which parking slots are occupied by which captured piece."""

    def __init__(self, black_bank: str = "right"):
        self.black_bank = black_bank
        self.occupied = {}            # (sx, sy) -> chess.Piece

    def alloc(self, piece: chess.Piece):
        """Reserve and return a free slot (sx, sy) for `piece`.

        Uses the same sequential stacking layout as hand captures (bank chosen by
        colour, filled leftmost/low-to-high) so the motor and the human park
        pieces identically on the same side.
        """
        for slot in self._human_fill_order(piece.color):
            if slot not in self.occupied:
                self.occupied[slot] = piece
                return slot
        # Overflow (e.g. a third same-type piece) - reuse the last valid slot.
        slot = self._human_fill_order(piece.color)[-1]
        self.occupied[slot] = piece
        return slot

    def alloc_human(self, piece: chess.Piece):
        """Slot we ASSUME a human used when taking `piece` off by hand.

        Identical to alloc(): each player stacks captures on the bank to their
        right (black -> right bank, white -> left bank), filling leftmost/low-to-
        high. Kept as a named method so call sites read clearly.
        """
        return self.alloc(piece)

    def _human_fill_order(self, color: bool):
        """Slots for `color`'s captures in the order a human would stack them."""
        cols = bank_columns(color, self.black_bank)   # black->right, white->left
        # Ranks ascend on the right bank (near the white player at rank 1) and
        # descend on the left bank (near the black player at rank 8) so each
        # player fills "low to high" from where they sit.
        ranks = range(8) if cols[0] > 0 else range(7, -1, -1)
        return [(c, y) for c in cols for y in ranks]

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

