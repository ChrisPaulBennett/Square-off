"""Translate python-chess moves into physical Square Off board actions.

A logical move (e.g. a knight capture, castling, en passant, promotion) can
require several physical motor operations on the board. This module turns a
`chess.Move` into an ordered list of primitive actions that the game layer then
executes against a `squareoff.SquareOff` connection.

Action tuples:
    ("move",    frm, to)   simple slide / knight hop
    ("capture", frm, to)   park the piece on `to`, then slide `frm` -> `to`
    ("park",    square)    carry the piece on `square` to the graveyard
    ("path",    raw_bytes) send a raw motion path (used for castling rooks)
    ("note",    text)      a message for the human (e.g. promotion swap)
"""

from __future__ import annotations

import chess

from squareoff import build_path, plan_park, square_to_xy


def _sq(square_index: int) -> str:
    return chess.square_name(square_index)


def _rook_castle_path(rook_from: str, rook_to: str):
    """Route the castling rook along the gridline next to the back rank so it
    threads past the king (which has already moved to its castled square)."""
    fx, fy = square_to_xy(rook_from)
    tx, ty = square_to_xy(rook_to)
    gy = fy + 0.5 if fy < 7 else fy - 0.5  # half-rank gridline toward the centre
    return [(fx, fy), (fx, gy), (tx, gy), (tx, ty)]


def physical_actions(board: chess.Board, move: chess.Move):
    """Return the ordered physical actions needed to play `move` on `board`.

    `board` must be the position *before* the move is applied.
    """
    actions = []
    frm = _sq(move.from_square)
    to = _sq(move.to_square)
    rank = chess.square_rank(move.from_square)

    if board.is_castling(move):
        # King first (its 2-square slide along the rank is clear), then the rook
        # routed around the king via the adjacent gridline.
        if board.is_kingside_castling(move):
            rook_from = _sq(chess.square(7, rank))
            rook_to = _sq(chess.square(5, rank))
        else:
            rook_from = _sq(chess.square(0, rank))
            rook_to = _sq(chess.square(3, rank))
        actions.append(("move", frm, to))
        actions.append(("path", build_path(_rook_castle_path(rook_from, rook_to))))
        return actions

    if board.is_en_passant(move):
        # The captured pawn sits on the destination file but the mover's rank.
        cap = _sq(chess.square(chess.square_file(move.to_square), rank))
        actions.append(("park", cap))
        actions.append(("move", frm, to))
        return actions

    if board.is_capture(move):
        actions.append(("capture", frm, to))
    else:
        actions.append(("move", frm, to))

    if move.promotion:
        piece = chess.piece_name(move.promotion)
        actions.append(
            ("note", f"Promotion: swap the pawn on {to} for a {piece} by hand.")
        )

    return actions


def describe_actions(actions) -> str:
    """Human-readable one-liner for a list of physical actions."""
    parts = []
    for a in actions:
        if a[0] == "move":
            parts.append(f"move {a[1]}->{a[2]}")
        elif a[0] == "route":
            parts.append(f"route {a[1]}->{a[2]}")
        elif a[0] == "capture":
            parts.append(f"{a[1]}x{a[2]}")
        elif a[0] == "park":
            parts.append(f"park {a[1]}")
        elif a[0] == "retrieve":
            parts.append(f"retrieve[{a[1]}]->{a[2]}")
        elif a[0] == "path":
            parts.append("rook-path")
        elif a[0] == "note":
            parts.append(f"note({a[1]})")
    return ", ".join(parts)


def _chebyshev(a: int, b: int) -> int:
    return max(abs(chess.square_file(a) - chess.square_file(b)),
               abs(chess.square_rank(a) - chess.square_rank(b)))


def plan_reset(board: chess.Board, graveyard=None):
    """Plan the physical actions to restore the standard starting position."""
    return plan_arrange(board, chess.Board(), graveyard)


def plan_arrange(board: chess.Board, target: chess.Board, graveyard=None):
    """Plan the physical actions to turn `board` into the `target` position.

    Uses the *logical* boards to know which piece is where, then:
      1. leaves pieces already on their target square alone,
      2. routes on-board pieces to their target squares (matching type+colour),
         parking a piece off-board to break any cycle and retrieving it after,
      3. retrieves captured pieces from the `graveyard` to fill empty targets,
      4. for any target still empty, emits a "place by hand" note,
      5. parks any leftover/extra pieces (e.g. a spare queen).

    Moves use collision-safe `route` actions (see squareoff.plan_route). Parking
    slots are type/colour-specific (see graveyard.Graveyard), so a captured piece
    always goes to a slot for its own type on its colour's bank.

    `graveyard` : a Graveyard holding pieces already parked during the game.
                  Modified in place. If None, a fresh empty one is used.

    Returns (actions, notes).
    """
    from graveyard import Graveyard

    grave = graveyard if graveyard is not None else Graveyard()
    cur = dict(board.piece_map())     # square -> Piece
    tgt = dict(target.piece_map())    # square -> Piece

    actions = []
    notes = []

    def name(sq):
        return chess.square_name(sq)

    # 1) pieces already on their target square
    assigned_targets = set()
    used_cur = set()
    for sq, pc in cur.items():
        if tgt.get(sq) == pc:
            assigned_targets.add(sq)
            used_cur.add(sq)

    # 2) match remaining on-board pieces to remaining target squares (nearest first)
    remaining_targets = [sq for sq in tgt if sq not in assigned_targets]
    remaining_cur = [sq for sq in cur if sq not in used_cur]
    assignment = {}  # target_sq -> source_sq
    for tsq in sorted(remaining_targets, key=lambda s: -tgt[s].piece_type):
        tp = tgt[tsq]
        best, best_d = None, 10**9
        for csq in remaining_cur:
            if cur[csq] == tp:
                d = _chebyshev(csq, tsq)
                if d < best_d:
                    best_d, best = d, csq
        if best is not None:
            assignment[tsq] = best
            remaining_cur.remove(best)

    # 3) park any genuine "extra" pieces (no matching target, e.g. a spare
    #    queen) up front - this also clears target squares they may sit on.
    occ = set(cur.keys())
    for csq in remaining_cur:
        pc = cur[csq]
        slot = grave.alloc(pc)
        actions.append(("park", name(csq), slot))
        occ.discard(csq)
        notes.append(
            f"Removed extra {chess.COLOR_NAMES[pc.color]} "
            f"{chess.piece_name(pc.piece_type)} from {name(csq)} to the tray."
        )

    # 4) execute assignments, breaking cycles by parking to the graveyard
    pending = dict(assignment)     # target -> source
    temp_parked = []               # (slot, target_sq) to retrieve at the end
    while pending:
        moved = False
        for tsq, csq in list(pending.items()):
            if csq == tsq:                     # already in place
                del pending[tsq]
                moved = True
            elif tsq not in occ:               # target free -> route straight in
                actions.append(("route", name(csq), name(tsq)))
                occ.discard(csq)
                occ.add(tsq)
                del pending[tsq]
                moved = True
        if not moved:
            # deadlock (cycle): park one source out of the way, retrieve later
            tsq, csq = next(iter(pending.items()))
            pc = cur[csq]
            slot = grave.alloc(pc)
            actions.append(("park", name(csq), slot))
            occ.discard(csq)
            temp_parked.append((slot, tsq))
            del pending[tsq]

    filled = set(assigned_targets) | set(assignment.keys())

    # 5) retrieve cycle-broken pieces onto their targets
    for slot, tsq in temp_parked:
        actions.append(("retrieve", slot, name(tsq)))
        grave.free(slot)
        filled.add(tsq)

    # 6) retrieve captured pieces from the graveyard to fill empty targets
    leftover_targets = [sq for sq in tgt if sq not in filled]
    for tsq in list(leftover_targets):
        slot = grave.take(tgt[tsq])
        if slot is not None:
            actions.append(("retrieve", slot, name(tsq)))
            filled.add(tsq)
            leftover_targets.remove(tsq)

    # 7) targets we still can't fill -> ask the human
    for tsq in leftover_targets:
        pc = tgt[tsq]
        notes.append(
            f"Place a {chess.COLOR_NAMES[pc.color]} {chess.piece_name(pc.piece_type)} "
            f"on {name(tsq)} by hand."
        )

    return actions, notes


