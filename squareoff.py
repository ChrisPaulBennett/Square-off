#!/usr/bin/env python3
"""
Square Off Pro BLE controller for Raspberry Pi (and other Linux/macOS/Windows hosts).

This script connects to a "Squareoff Pro" chess board over Bluetooth Low Energy
(BLE) using the Nordic UART Service (NUS) and lets you send/receive the board's
text protocol.

Protocol reference (unofficial):
    Two generations of firmware are supported.

    (A) Legacy text protocol over Nordic UART (NUS). Messages look like
        '<commandId>#<data>*':

        app  --> board : 14#1*            start game (standard position)
        board--> app   : 14#GO*          handshake ack
        app  --> board : 4#*             battery status request
        board--> app   : 4#4095.00*      battery value

    (B) "Square Off SWAP" motion protocol (decoded from BLE HCI snoop logs).
        Piece movement is NOT on the NUS char anymore - that is why older
        scripts could beep but never move a piece. Instead:

        * Occupancy is streamed as a 64-char '0/1' string on the OCC notify
          characteristic (777ac5a4-...).
        * Piece up/down and move acks ('e2u', 'e4d', 'OK') arrive on the STATUS
          notify characteristic (4496994f-...).
        * To MOVE a piece, write a coordinate *path* to the MOVE characteristic
          (f9664d70-...):

              x0,y0:x1,y1:...:xn,yn|

          where x = file (a=0..h=7), y = rank-1 (0..7). Integers are square
          centres; '.5' values sit on the gridline between squares so knights
          (and captured pieces) can thread between other pieces. Example:
              e7-e6  ->  4,6:4,5|
              d7-d5  ->  3,6:3,4|
              g8-f6  ->  6,7:5.5,6.5:5.5,5.5:5,5|

        * Session/handshake state is written to STATE (c7d64c44-...):
              R:ISG  = ready / in-sync game     S:po = settle after a move

        * Captured pieces are parked in an off-board channel past the RIGHT edge
          (files only span a=0..h=7, so x>=8 is off the board). The reset in the
          log retrieved a captured knight starting at x=9:
              9,4:8.5,4.5:6.5,4.5:6.5,6.5:5.92,7.08|
          See plan_park() / capture(): the victim is carried out to a slot at
          x~9 before the attacker slides in.

Requires: bleak  (pip install bleak)

Usage:
    python squareoff.py            # scan, connect, then drop into an interactive prompt
    python squareoff.py --address AA:BB:CC:DD:EE:FF
    python squareoff.py --scan     # just scan and list devices, then exit
"""

import argparse
import asyncio
import sys

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # pragma: no cover
    sys.exit(
        "The 'bleak' package is required. Install it with:\n"
        "    pip install bleak"
    )

# --- Nordic UART Service (NUS) UUIDs -------------------------------------------------
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
# Characteristic the board writes notifications to (board --> app)
NUS_TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
# Characteristic the app writes commands to (app --> board)
NUS_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"

# --- Square Off SWAP "motion" service --------------------------------------------
# Newer boards (e.g. "Square Off SWAP") moved piece movement OFF the NUS text
# protocol and onto a dedicated service. This was reverse-engineered from BLE
# HCI snoop logs of the official app.
#
#   MOVE_CHAR   (write)  : physical piece movement, sent as a coordinate *path*
#   STATE_CHAR  (write)  : session/handshake state ('R:ISG', 'S:po', 'S:bl', ...)
#   CONFIG_CHAR (write)  : misc config byte
#   STATUS_CHAR (notify) : 'OK' after a move, plus 'e2u'/'e4d' piece up/down
#   OCC_CHAR    (notify) : 64-char occupancy string streamed continuously
MOVE_CHAR_UUID = "f9664d70-93ff-4cfe-9bfe-b5866aa5bef2"
STATE_CHAR_UUID = "c7d64c44-42f0-11ec-81d3-0242ac130003"
CONFIG_CHAR_UUID = "c7d64c45-42f0-11ec-81d3-0242ac130003"
STATUS_CHAR_UUID = "4496994f-2600-4e7e-81d5-e0f7b67ebd48"
OCC_CHAR_UUID = "777ac5a4-6fa8-474b-841d-091bd57d28c4"

DEVICE_NAME = "Square Off"

# Some NUS implementations cannot receive more than 20 bytes per write, so we chunk.
MAX_WRITE_CHUNK = 20


def build_message(command_id: int, data: str = "") -> bytes:
    """Build a protocol message of the form '<commandId>#<data>*'."""
    return f"{command_id}#{data}*".encode("ascii")


# --- Square Off SWAP motion-path helpers ----------------------------------------
#
# The SWAP board moves pieces by following a *path* of waypoints written to
# MOVE_CHAR_UUID:
#
#     x0,y0:x1,y1:...:xn,yn|
#
# Coordinate system (decoded from HCI snoop logs):
#   x = file index, a=0 .. h=7   (values <0 or >7 are the off-board channel)
#   y = rank - 1,   rank1=0 .. rank8=7
#   integer coords  -> centre of a square
#   .5 coords       -> on the gridline *between* squares (used to thread a
#                      knight or a captured piece through the gaps)
#   trailing '|'    -> terminates the command; board then replies 'OK'.


def _fmt_coord(v: float) -> str:
    """Format a coordinate, dropping the trailing '.0' for whole numbers."""
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:g}"


def square_to_xy(square: str):
    """'e2' -> (4, 1). File a=0..h=7, rank 1=0..8=7."""
    square = square.strip().lower()
    if len(square) < 2 or square[0] not in "abcdefgh" or not square[1].isdigit():
        raise ValueError(f"bad square: {square!r}")
    file_idx = ord(square[0]) - ord("a")
    rank_idx = int(square[1]) - 1
    if not (0 <= rank_idx <= 7):
        raise ValueError(f"bad rank in square: {square!r}")
    return file_idx, rank_idx


def build_path(points) -> bytes:
    """Turn [(x,y), ...] into the board's 'x,y:x,y:...|' path command."""
    body = ":".join(f"{_fmt_coord(x)},{_fmt_coord(y)}" for x, y in points)
    return (body + "|").encode("ascii")


def plan_move(frm: str, to: str):
    """Return a list of waypoints [(x,y), ...] for a simple (non-capturing) move.

    Sliding pieces (pawn/rook/bishop/queen/king) go straight from source to
    destination. Knights are routed along the gridline between squares so they
    thread between the intervening pieces, mirroring what the official app does.
    Captures / castling need special handling - use `plan_capture` / raw `path`.
    """
    fx, fy = square_to_xy(frm)
    tx, ty = square_to_xy(to)
    dx, dy = tx - fx, ty - fy

    def sign(n):
        return (n > 0) - (n < 0)

    if {abs(dx), abs(dy)} == {1, 2}:
        # Knight: travel along the gridline that runs between the two files
        # (or ranks) on the "long" axis, so we pass between adjacent pieces.
        if abs(dy) == 2:  # long axis is the rank -> vertical gridline at mid-file
            midx = (fx + tx) / 2.0
            s = sign(dy)
            return [
                (fx, fy),
                (midx, fy + s * 0.5),
                (midx, ty - s * 0.5),
                (tx, ty),
            ]
        else:  # long axis is the file -> horizontal gridline at mid-rank
            midy = (fy + ty) / 2.0
            s = sign(dx)
            return [
                (fx, fy),
                (fx + s * 0.5, midy),
                (tx - s * 0.5, midy),
                (tx, ty),
            ]

    # Straight / diagonal slide with a clear path.
    return [(fx, fy), (tx, ty)]


# --- Off-board "graveyard" / parking banks --------------------------------------
#
# Parking channel layout, CONFIRMED on hardware.
#
# Board files are x=0..7. Just off each edge there are two columns of parking
# slots, grouped by piece type. There are two mirrored banks:
#
#   Right bank : x=8 (inner), x=9 (outer)   -- one colour's captures
#   Left  bank : x=-1 (inner), x=-2 (outer) -- the other colour's captures
#
# Rank (y) is assigned by piece type (same on both columns of a bank):
#   pawns  y=0,1,2,3   knight y=4   bishop y=5   rook y=6   queen y=7
#   (the two queen slots include one spare for a promotion)
#
# A piece is routed out along a half-rank gridline to the channel mid-line, then
# into its slot. NEVER route to negative-y / off the bottom edge - that grinds
# the motor against the frame (confirmed: it made a horrible clunk).
PARK_INNER_RIGHT = 8.0
PARK_OUTER_RIGHT = 9.0
PARK_INNER_LEFT = -1.0
PARK_OUTER_LEFT = -2.0

# Right-bank slots in a sensible fill order, used only as a type-agnostic
# fallback when no proper (type/colour) slot is supplied.
_FALLBACK_SLOTS = [
    (8, 0), (8, 1), (8, 2), (8, 3), (9, 0), (9, 1), (9, 2), (9, 3),  # pawns
    (8, 4), (9, 4),                                                  # knights
    (8, 5), (9, 5),                                                  # bishops
    (8, 6), (9, 6),                                                  # rooks
    (8, 7), (9, 7),                                                  # queens
]


def _park_transition(sx: float) -> float:
    """Channel mid-line used to enter/leave a slot column on the correct side."""
    return 8.5 if sx > 0 else -1.5


def plan_park(square: str, slot):
    """Route the piece on `square` out to graveyard slot `slot` = (sx, sy).

    Hop onto a half-rank gridline, drive to the channel mid-line on the correct
    side, then into the slot. Matches the shapes verified on hardware.
    """
    sx, sy = slot
    fx, fy = square_to_xy(square)
    gy = fy + 0.5 if fy < 7 else fy - 0.5   # a half-rank gridline to travel along
    return [
        (fx, fy),
        (fx, gy),
        (_park_transition(sx), gy),
        (sx, sy),
    ]


def plan_retrieve(slot, square: str):
    """Bring a parked piece from graveyard `slot` = (sx, sy) back onto `square`.

    The reverse of plan_park; matches the retrieval seen in the log
    (`9,4:8.5,4.5:6.5,4.5:...`).
    """
    sx, sy = slot
    tx, ty = square_to_xy(square)
    gy = ty + 0.5 if ty < 7 else ty - 0.5
    return [
        (sx, sy),
        (_park_transition(sx), gy),
        (tx, gy),
        (tx, ty),
    ]


def plan_route(frm: str, to: str):
    """A collision-safe path from `frm` to `to` that stays on the gridlines.

    Pieces sit at integer square centres, so a path that travels only along the
    half-integer gridlines (the gaps between squares) never bumps another piece.
    Used when setting up an arbitrary position, where straight moves would plough
    through other pieces. The target square must be empty on arrival.
    """
    fx, fy = square_to_xy(frm)
    tx, ty = square_to_xy(to)
    if (fx, fy) == (tx, ty):
        return [(fx, fy)]
    if fx == tx:                                   # same file: hug one side gap
        gx = fx + 0.5 if fx < 7 else fx - 0.5
        return [(fx, fy), (gx, fy), (gx, ty), (tx, ty)]
    if fy == ty:                                   # same rank: hug one side gap
        gy = fy + 0.5 if fy < 7 else fy - 0.5
        return [(fx, fy), (fx, gy), (tx, gy), (tx, ty)]
    sx = 0.5 if tx > fx else -0.5
    sy = 0.5 if ty > fy else -0.5
    return [
        (fx, fy),                # centre
        (fx + sx, fy + sy),      # exit to a gap corner
        (tx - sx, fy + sy),      # travel along a rank gridline
        (tx - sx, ty - sy),      # travel along a file gridline
        (tx, ty),                # enter the (empty) target
    ]


def plan_capture(frm: str, to: str, slot):
    """Return (park_path, move_path) for a capture.

    First the captured piece on `to` is carried off to graveyard `slot`=(sx,sy),
    then the attacker slides from `frm` to the now-empty `to`.
    """
    park = build_path(plan_park(to, slot))
    move = build_path(plan_move(frm, to))
    return park, move


def parse_occupancy(data: str) -> str:
    """Pretty-print a 64-char occupancy string as an 8x8 grid.

    The board order is a1-a8, b1-b8, c1-c8, ... i.e. file-major.
    We print it rank 8 (top) down to rank 1 (bottom).
    """
    data = data.strip()
    if len(data) < 64:
        return f"(occupancy string too short: {len(data)} chars)\n{data}"

    files = "abcdefgh"
    rows = []
    for rank in range(8, 0, -1):  # 8 down to 1
        cells = []
        for file_idx in range(8):
            # index = file_idx * 8 + (rank - 1)
            idx = file_idx * 8 + (rank - 1)
            cells.append("#" if data[idx] == "1" else ".")
        rows.append(f"{rank}  " + " ".join(cells))
    rows.append("   " + " ".join(files))
    return "\n".join(rows)


class SquareOff:
    """Small wrapper around a BLE connection to a Square Off Pro board."""

    def __init__(self, address: str):
        self.address = address
        self.client = BleakClient(address, disconnected_callback=self._on_disconnect)
        self._rx_buffer = ""
        self._notifying = []
        self._last_occupancy = None
        # Graveyard slots that are already occupied (by rank index 0..7).
        self._parked_slots = set()
        # Optional hooks so a UI/game layer can receive board events.
        #   status_callback(text)      -> 'OK', 'e2u', 'e4d', ...
        #   occupancy_callback(str64)  -> 64-char occupancy string (on change)
        self.status_callback = None
        self.occupancy_callback = None
        # Board orientation. When True, every coordinate is rotated 180°
        # ((x,y) -> (7-x, 7-y)) so you can play with the board turned around
        # (colours swapped). This also maps the right parking bank onto the
        # left, keeping captures/parking consistent.
        self.flipped = False

    # --- orientation helpers -------------------------------------------------
    def _orient(self, points):
        """Apply the 180° flip to a list of (x, y) waypoints when flipped."""
        if not self.flipped:
            return points
        return [(7 - x, 7 - y) for (x, y) in points]

    def _flip_square_name(self, name: str) -> str:
        """Translate a board square name to/from the flipped orientation."""
        if not self.flipped or len(name) < 2 or name[0] not in "abcdefgh":
            return name
        file_idx = ord(name[0]) - ord("a")
        rank_idx = int(name[1]) - 1
        return "abcdefgh"[7 - file_idx] + str((7 - rank_idx) + 1)

    def _flip_occupancy(self, data: str) -> str:
        """Remap a 64-char (file-major) occupancy string to the flipped view."""
        if not self.flipped or len(data) != 64:
            return data
        # index of (x, y) is x*8 + y; flipped square (x,y) reads (7-x, 7-y).
        return "".join(data[(7 - (i // 8)) * 8 + (7 - (i % 8))] for i in range(64))

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.disconnect()

    def _on_disconnect(self, _client):
        print("\n[!] Board disconnected.")

    async def connect(self):
        print(f"[*] Connecting to {self.address} ...")
        await self.client.connect()
        print("[+] Connected. Subscribing to notifications ...")
        # Old NUS text channel (battery, some acks).
        await self._try_notify(NUS_TX_CHAR_UUID, self._handle_notification)
        # New SWAP motion service: status ('OK', piece up/down) + occupancy.
        await self._try_notify(STATUS_CHAR_UUID, self._handle_status)
        await self._try_notify(OCC_CHAR_UUID, self._handle_occupancy)
        print("[+] Ready.")

    async def _try_notify(self, uuid, cb):
        """Subscribe to a characteristic, ignoring it if the board lacks it."""
        try:
            await self.client.start_notify(uuid, cb)
            self._notifying.append(uuid)
        except Exception as exc:  # noqa: BLE001
            print(f"[!] Could not subscribe to {uuid}: {exc}")

    async def disconnect(self):
        try:
            if self.client.is_connected:
                for uuid in self._notifying:
                    try:
                        await self.client.stop_notify(uuid)
                    except Exception:  # noqa: BLE001
                        pass
                await self.client.disconnect()
        except Exception:  # noqa: BLE001 - best effort on shutdown
            pass

    # --- Incoming data -------------------------------------------------------------
    def _handle_notification(self, _sender, data: bytearray):
        """Accumulate bytes and dispatch complete '<id>#<data>*' messages."""
        self._rx_buffer += data.decode("ascii", errors="replace")
        while "*" in self._rx_buffer:
            message, self._rx_buffer = self._rx_buffer.split("*", 1)
            message = message.strip()
            if message:
                self._dispatch(message)

    def _dispatch(self, message: str):
        if "#" not in message:
            print(f"[board] (raw) {message}")
            return
        command_id, _, payload = message.partition("#")
        command_id = command_id.strip()

        if command_id == "14":
            print(f"[board] handshake -> {payload}")
        elif command_id == "0":
            # piece up/down: <square><u|d>
            square, action = payload[:-1], payload[-1:]
            verb = {"u": "lifted", "d": "placed"}.get(action, action)
            print(f"[board] piece {verb} on {square}")
        elif command_id == "22":
            print(f"[board] battery: {payload}")
        elif command_id == "30":
            print(f"[board] occupancy:\n{parse_occupancy(payload)}")
        else:
            print(f"[board] {command_id}#{payload}")

    def _handle_status(self, _sender, data: bytearray):
        """SWAP status channel: 'OK' after a move, 'e2u'/'e4d' piece up/down."""
        text = data.decode("ascii", errors="replace").strip()
        if not text:
            return
        # Translate the reported square into the logical (flipped) view so the
        # rest of the app always works in standard orientation.
        if len(text) >= 3 and text[-1] in "ud":
            text = self._flip_square_name(text[:-1]) + text[-1]
        if text == "OK":
            print("[board] move OK")
        elif len(text) >= 3 and text[-1] in "ud":
            square, action = text[:-1], text[-1]
            verb = {"u": "lifted", "d": "placed"}[action]
            print(f"[board] piece {verb} on {square}")
        else:
            print(f"[board] status: {text}")
        if self.status_callback:
            try:
                self.status_callback(text)
            except Exception as exc:  # noqa: BLE001
                print(f"[!] status_callback error: {exc}")

    def _handle_occupancy(self, _sender, data: bytearray):
        """SWAP occupancy channel: continuous 64-char '0/1' board state."""
        text = data.decode("ascii", errors="replace").strip()
        text = self._flip_occupancy(text)   # into the logical (flipped) view
        if len(text) == 64 and text != self._last_occupancy:
            self._last_occupancy = text
            # Streamed very frequently; only print when it changes.
            print(f"[board] occupancy changed:\n{parse_occupancy(text)}")
            if self.occupancy_callback:
                try:
                    self.occupancy_callback(text)
                except Exception as exc:  # noqa: BLE001
                    print(f"[!] occupancy_callback error: {exc}")

    # --- Outgoing commands ---------------------------------------------------------
    async def _write(self, uuid, payload: bytes, response: bool = True):
        """Write raw bytes to a characteristic, chunked for compatibility."""
        for i in range(0, len(payload), MAX_WRITE_CHUNK):
            chunk = payload[i:i + MAX_WRITE_CHUNK]
            await self.client.write_gatt_char(uuid, chunk, response=response)

    async def _write_once(self, uuid, payload: bytes, response: bool = True):
        """Write the WHOLE payload in a single GATT write (no chunking).

        The SWAP motion/state characteristics treat each write as one complete
        command. Splitting a long command (e.g. a capture/parking path, which is
        >20 bytes) across 20-byte chunks makes the board see a truncated,
        malformed command and drop the BLE connection. The board negotiates a
        517-byte MTU, so the whole command fits in one write.
        """
        await self.client.write_gatt_char(uuid, payload, response=response)

    async def send(self, command_id: int, data: str = ""):
        payload = build_message(command_id, data)
        await self._write(NUS_RX_CHAR_UUID, payload, response=False)
        print(f"[app  ] sent {payload.decode('ascii')}")

    # --- SWAP motion service --------------------------------------------------------
    async def state(self, text: str):
        """Send a session/state command to the SWAP state characteristic."""
        await self._write_once(STATE_CHAR_UUID, text.encode("ascii"))
        print(f"[app  ] state -> {text}")

    async def handshake(self):
        """Bring the SWAP board into the 'ready / in-sync game' state.

        Mirrors the init the official app performs before it can drive pieces.
        """
        await self.state("S:po")
        await self._write_once(CONFIG_CHAR_UUID, b"0")
        await self.state("R:ISG")

    async def send_path(self, path: bytes, settle: bool = True):
        """Write a raw motion path (e.g. b'4,6:4,5|') and let the motor run.

        Sent as a single write - see _write_once for why chunking breaks this.
        """
        await self._write_once(MOVE_CHAR_UUID, path)
        print(f"[app  ] path -> {path.decode('ascii')}")
        if settle:
            # The app sends 'S:po' after each move to settle the board state.
            await self.state("S:po")

    async def move(self, frm: str, to: str):
        """Physically move a piece from one square to another (simple moves).

        For captures use `capture()`; for castling send the two `path` segments
        yourself (move the king, then the rook).
        """
        path = build_path(self._orient(plan_move(frm, to)))
        print(f"[app  ] move {frm}{to}")
        await self.send_path(path)

    def _next_park_slot(self):
        """Pick the next free fallback graveyard slot (right bank) as (sx, sy).

        Used only when a caller doesn't provide a type-correct slot (e.g. the raw
        CLI 'capture'). The game layer passes proper slots via a Graveyard.
        """
        for slot in _FALLBACK_SLOTS:
            if slot not in self._parked_slots:
                return slot
        return _FALLBACK_SLOTS[0]

    async def park_piece(self, square: str, slot=None):
        """Carry the piece on `square` out to graveyard `slot` = (sx, sy)."""
        if slot is None:
            slot = self._next_park_slot()
        self._parked_slots.add(slot)
        print(f"[app  ] park {square} -> slot {slot}")
        await self.send_path(build_path(self._orient(plan_park(square, slot))))
        return slot

    async def retrieve_piece(self, slot, square: str):
        """Bring a parked piece from graveyard `slot` = (sx, sy) onto `square`."""
        self._parked_slots.discard(slot)
        print(f"[app  ] retrieve slot {slot} -> {square}")
        await self.send_path(build_path(self._orient(plan_retrieve(slot, square))))

    async def route_move(self, frm: str, to: str):
        """Collision-safe move along the gridlines (for arbitrary setups)."""
        print(f"[app  ] route {frm} -> {to}")
        await self.send_path(build_path(self._orient(plan_route(frm, to))))

    async def beep(self):
        """Make the board beep (uses the king-in-check sound on the NUS channel)."""
        await self.send(27, "ck")

    async def capture(self, frm: str, to: str, slot=None):
        """Capture: park the piece on `to`, then slide the attacker in.

        The captured piece is carried to graveyard `slot` = (sx, sy). Pass a slot
        to choose a specific one, otherwise the next free fallback is used.
        Returns the slot.
        """
        if slot is None:
            slot = self._next_park_slot()
        self._parked_slots.add(slot)
        park_path = build_path(self._orient(plan_park(to, slot)))
        move_path = build_path(self._orient(plan_move(frm, to)))
        print(f"[app  ] capture {frm}x{to} (park victim in slot {slot})")
        # 1) carry the captured piece off the board...
        await self.send_path(park_path, settle=False)
        # 2) ...then move the attacker onto the now-empty square.
        await self.send_path(move_path)
        return slot

    # Convenience helpers ------------------------------------------------------------
    async def start_game(self):
        """Start a game from the standard position (triggers the handshake)."""
        await self.send(14, "1")

    async def battery(self):
        await self.send(4, "")

    async def read_board(self):
        await self.send(30, "R")

    async def set_position(self, occupancy: str):
        """Set a custom position. `occupancy` is 64 chars of 0/1 (a1-a8,b1-b8,...)."""
        occupancy = occupancy.strip()
        if len(occupancy) != 64 or any(c not in "01" for c in occupancy):
            raise ValueError("occupancy must be exactly 64 characters of 0/1")
        await self.send(30, occupancy)

    async def leds(self, squares: str):
        """Light LEDs, e.g. squares='c8d7e6f5' or 'e2e4'."""
        await self.send(25, squares.replace(" ", ""))

    async def in_sync(self):
        await self.send(26, "ISG")

    async def check_sound(self):
        await self.send(27, "ck")

    async def result(self, who: str):
        codes = {"white": "wt", "black": "bl", "draw": "dw"}
        if who not in codes:
            raise ValueError("result must be one of: white, black, draw")
        await self.send(27, codes[who])


STANDARD_OCCUPANCY = (
    # a1-a8, b1-b8, ... each file: ranks 1,2 occupied, 3-6 empty, 7,8 occupied
    "11000011" * 8
)


async def scan(timeout: float = 8.0):
    print(f"[*] Scanning for BLE devices ({timeout:.0f}s) ...")
    devices = await BleakScanner.discover(timeout=timeout)
    found = []
    for d in devices:
        name = d.name or "(unknown)"
        marker = " <-- Square Off" if _is_square_off(d.name) else ""
        print(f"    {d.address}  {name}{marker}")
        found.append(d)
    return found


def _is_square_off(name) -> bool:
    """Match 'Squareoff Pro', 'Square Off SWAP - C80', etc."""
    if not name:
        return False
    low = name.lower().replace(" ", "")
    return "squareoff" in low


async def find_board(timeout: float = 10.0):
    """Return the address of the first Square Off board found, or None."""
    print(f"[*] Looking for '{DEVICE_NAME}' ({timeout:.0f}s) ...")
    device = await BleakScanner.find_device_by_filter(
        lambda d, _ad: _is_square_off(d.name),
        timeout=timeout,
    )
    return device.address if device else None


HELP_TEXT = """
Commands:
    move <from><to>  physically move a piece, e.g. 'move e2e4' (simple moves)
    capture <a><t>   capture: park piece on <t> off-board, move attacker <a>, e.g.
                     'capture e4d5' (a.k.a. 'take')
    path <x,y:...|>  send a raw motion path to the motor (castling/odd cases)
    handshake        put the SWAP board into 'ready' state (do this first)
    start            start a game from the standard position (handshake)
    battery          request battery status
    read             read the board occupancy
    stdpos           set the standard starting position
    setpos <64x01>   set a custom occupancy (a1-a8,b1-b8,...)
    led <squares>    light LEDs, e.g. 'led e2e4' or 'led c8d7e6f5'
    sync             send the in-sync ack (26#ISG)
    check            play the king-in-check sound
    result <w|b|d>   send game result: w=white, b=black, d=draw
    state <text>     send a raw SWAP state command (e.g. 'state S:po')
    raw <id> <data>  send an arbitrary message '<id>#<data>*'
    help             show this help
    quit / exit      disconnect and exit

Note: 'move'/'path' drive the physical pieces on Square Off SWAP boards.
      Run 'handshake' once after connecting so the board accepts moves.
"""


async def interactive(board: "SquareOff"):
    print(HELP_TEXT)
    loop = asyncio.get_event_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, input, "squareoff> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]
        try:
            if cmd in ("quit", "exit"):
                break
            elif cmd == "help":
                print(HELP_TEXT)
            elif cmd == "move" and args:
                token = "".join(args).replace("-", "").replace(",", "").lower()
                if len(token) < 4:
                    print("usage: move e2e4")
                else:
                    await board.move(token[:2], token[2:4])
            elif cmd in ("capture", "take") and args:
                token = "".join(args).replace("-", "").replace("x", "").replace(",", "").lower()
                if len(token) < 4:
                    print("usage: capture e4d5   (attacker square, then victim square)")
                else:
                    await board.capture(token[:2], token[2:4])
            elif cmd == "path" and args:
                raw = "".join(args)
                if not raw.endswith("|"):
                    raw += "|"
                await board.send_path(raw.encode("ascii"))
            elif cmd == "handshake":
                await board.handshake()
            elif cmd == "state" and args:
                await board.state(" ".join(args))
            elif cmd == "start":
                await board.start_game()
            elif cmd == "battery":
                await board.battery()
            elif cmd == "read":
                await board.read_board()
            elif cmd == "stdpos":
                await board.set_position(STANDARD_OCCUPANCY)
            elif cmd == "setpos" and args:
                await board.set_position(args[0])
            elif cmd == "led" and args:
                await board.leds("".join(args))
            elif cmd == "sync":
                await board.in_sync()
            elif cmd == "check":
                await board.check_sound()
            elif cmd == "result" and args:
                mapping = {"w": "white", "b": "black", "d": "draw"}
                await board.result(mapping.get(args[0].lower(), args[0].lower()))
            elif cmd == "raw" and len(args) >= 1:
                cid = int(args[0])
                data = args[1] if len(args) > 1 else ""
                await board.send(cid, data)
            else:
                print("Unknown or incomplete command. Type 'help'.")
        except Exception as exc:  # noqa: BLE001 - keep the REPL alive
            print(f"[error] {exc}")


async def main():
    parser = argparse.ArgumentParser(description="Square Off Pro BLE controller")
    parser.add_argument("--address", help="BLE address of the board (skip scanning)")
    parser.add_argument("--scan", action="store_true", help="scan and list devices, then exit")
    parser.add_argument("--timeout", type=float, default=10.0, help="scan timeout in seconds")
    args = parser.parse_args()

    if args.scan:
        await scan(args.timeout)
        return

    address = args.address
    if not address:
        address = await find_board(args.timeout)
        if not address:
            print("[!] No Square Off board found. Try '--scan' to list devices, "
                  "or pass '--address'.")
            return

    async with SquareOff(address) as board:
        await interactive(board)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

