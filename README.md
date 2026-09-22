# Square Off Pro — Raspberry Pi BLE controller

A small Python script to connect to a **Square Off Pro** chess board over
Bluetooth Low Energy (BLE) and drive it using its text protocol (Nordic UART
Service). Tested target: Raspberry Pi 3 running Raspberry Pi OS.

> ⚠️ This is based on an **unofficial, incomplete** reverse-engineered protocol.
> Use at your own risk.

## What it does

- Scans for the board (advertised name `Squareoff Pro`) and connects.
- Subscribes to notifications so you see piece up/down, battery and board state.
- Gives you an interactive prompt to send commands:
  - start a game, read the board, set a custom position
  - light LEDs, play the king-in-check sound, send a game result
  - send the battery request or any raw `<id>#<data>*` message.

## Play a bot (web UI)

```bash
pip install -r requirements.txt      # bleak + python-chess + flask + requests
python3 app.py                       # open http://localhost:5000
```

**Scan → Connect**, pick your colour + opponent, **Start new game**. You move on
the physical board (auto-detected from the piece up/down events) or by dragging
on-screen; the opponent's reply is played by the motor — captures (piece parked
off-board), castling, en passant and promotions (with a hand-swap reminder) all
handled.

## Android app (APK)

You can install this on an Android phone as a native app (Flask UI in a WebView,
real Bluetooth via `bleak`'s Android backend). See **[ANDROID_BUILD.md](ANDROID_BUILD.md)**
for the full Buildozer build steps. In short:

```bash
pip install "buildozer==1.5.0" "cython<3.0"
buildozer android debug          # produces bin/squareoff-*.apk
```

> Termux / a plain web host won't work for the board itself — normal (non-rooted)
> Android has no BlueZ, so BLE only works through a python-for-android build.

## Command line (same features, no browser)

Every web-UI feature is also available from a text prompt, driving the same
engine:

```bash
python3 cli.py            # or:  python3 app.py --cli
```

```
squareoff> scan                     # list BLE devices
squareoff> connect 0                # connect by scan index (or a MAC address)
squareoff> newgame white easy       # play the local engine as White
squareoff> lichess <token> 4 black  # play the Lichess AI level 4 as Black
squareoff> move e2e4                # your move (or SAN: 'move Nf3')
squareoff> puzzle                   # load & set up the chess.com daily puzzle
squareoff> reset                    # re-home all pieces to the start position
squareoff> beep / battery / led e2e4 / path 4,6:4,5| / raw 4
squareoff> show / status / help / quit
```

Moves you make on the physical board are detected automatically and printed as
they happen (the bot's reply appears without you typing). The lower-level
protocol prompt in `squareoff.py` is still available for raw tinkering.

Two opponent backends:

- **Local engine** — Stockfish if installed (`sudo apt install stockfish`),
  otherwise a small built-in fallback so it works with nothing extra.
- **Lichess AI (online)** — real programmatic play via the Lichess Board API.
  Paste a personal token with the `board:play` scope
  (<https://lichess.org/account/oauth/token/create>) and choose AI level 1–8.

> chess.com has **no public API for playing** its bots (its API is read-only),
> which is why the online option uses Lichess instead.

**Reset board** re-homes every piece to the starting position using the
move/park/retrieve paths (pieces it can't place — e.g. ones you took off by hand
— are listed for you to place manually).

## Daily puzzle (chess.com)

**Load chess.com daily puzzle** fetches the puzzle from the public read-only
endpoint `https://api.chess.com/pub/puzzle`, sets the position up on the board
(collision-safe gridline routing; a few surplus pieces are parked, the rest are
listed to remove by hand), and lets you solve it over the board. Play the best
move — if it's **wrong the board beeps and slides the piece back**; if it's
right, the board plays the reply from the solution line and you continue until
solved.

> Best results: press **Reset board** first so a full set is in the start
> position, then load the puzzle. Positions with very few pieces (e.g. bare
> endgames) exceed the physical capture tray, so those are better set up by hand.

## 1. Set up the Raspberry Pi

The Pi 3 has built-in Bluetooth. Make sure it is enabled and BlueZ is running:

```bash
sudo apt update
sudo apt install -y python3-pip bluetooth bluez libglib2.0-dev
sudo systemctl enable --now bluetooth
```

(Optional but recommended) create a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
```

Install the Python dependency:

```bash
pip install -r requirements.txt
```

`bleak` talks to BlueZ over D-Bus, so you usually do **not** need `sudo` to run
the script. If scanning fails with a permissions error, either run with `sudo`
or grant the Python binary BLE scan capabilities:

```bash
sudo setcap 'cap_net_raw,cap_net_admin+eip' $(readlink -f $(which python3))
```

## 2. Run it

Turn the board on, then:

```bash
# Scan and list nearby BLE devices (find your board's address)
python3 squareoff.py --scan

# Auto-find the board by name and connect
python3 squareoff.py

# Or connect directly by address
python3 squareoff.py --address AA:BB:CC:DD:EE:FF
```

Once connected you get a prompt:

```
squareoff> handshake    # put a SWAP board into "ready" state (do this first)
squareoff> move e2e4    # physically move a piece (SWAP boards)
squareoff> path 6,7:5.5,6.5:5.5,5.5:5,5|   # raw motion path (captures/castling)
squareoff> start        # start a game from the standard position
squareoff> battery      # ask for battery status
squareoff> read         # read the current occupancy (prints an 8x8 grid)
squareoff> led e2e4      # light the LEDs for a move
squareoff> stdpos       # set the standard starting position
squareoff> setpos 1100001111000011...   # 64 chars of 0/1 (a1-a8,b1-b8,...)
squareoff> check        # king-in-check sound
squareoff> result w     # white wins (w/b/d)
squareoff> raw 4         # send an arbitrary 4#* message
squareoff> quit
```

## Moving pieces (Square Off SWAP)

Newer boards advertised as **`Square Off SWAP`** changed the protocol: piece
movement is no longer part of the Nordic UART text protocol (which is why older
scripts could beep/read the board but never move a piece). Movement now lives on
a dedicated service, reverse-engineered from BLE HCI snoop logs of the app:

| Purpose | Characteristic |
|---------|----------------|
| Move motor (write a path) | `f9664d70-93ff-4cfe-9bfe-b5866aa5bef2` |
| Session/state (write) | `c7d64c44-42f0-11ec-81d3-0242ac130003` |
| Config (write) | `c7d64c45-42f0-11ec-81d3-0242ac130003` |
| Status notify (`OK`, `e2u`/`e4d`) | `4496994f-2600-4e7e-81d5-e0f7b67ebd48` |
| Occupancy notify (64 chars) | `777ac5a4-6fa8-474b-841d-091bd57d28c4` |

A move is a **coordinate path** written to the move characteristic:

```
x0,y0:x1,y1:...:xn,yn|
```

- `x` = file, `a=0 … h=7`
- `y` = rank − 1, `rank1=0 … rank8=7`
- integer coords = square centre
- `.5` coords = the gridline **between** squares (used to thread knights and
  captured pieces through the gaps between other pieces)
- the trailing `|` ends the command; the board replies `OK` on the status char.

Examples straight from the logs:

| Move | Path |
|------|------|
| e7–e6 (pawn) | `4,6:4,5\|` |
| d7–d5 (pawn) | `3,6:3,4\|` |
| g8–f6 (knight) | `6,7:5.5,6.5:5.5,5.5:5,5\|` |

Run `handshake` once after connecting, then `move e2e4`. Captures and castling
need multiple segments (route the captured piece to the board edge first, then
move the capturer) — send those with the raw `path` command.

## Captured pieces / parking banks

Captured pieces go into **two mirrored parking banks** just off the board edges
(confirmed on hardware). Files span `a=0 … h=7`; the banks sit just outside:

| Bank | Columns (x) | Holds |
|------|-------------|-------|
| Right | `8` (inner), `9` (outer) | one colour's captures |
| Left  | `-1` (inner), `-2` (outer) | the other colour's captures |

Within a bank the **rank (y) is fixed per piece type** (same on both columns):

| Piece | Rank(s) `y` |
|-------|-------------|
| Pawn | `0, 1, 2, 3` (8 slots) |
| Knight | `4` |
| Bishop | `5` |
| Rook | `6` |
| Queen | `7` (two slots — one spare for a promotion) |

By default **black → right bank, white → left bank** (a captured black knight was
retrieved from `9,4` in the logs); flip `black_bank='left'` in `graveyard.py` if
your board is the other way round.

A piece is routed out along a half-rank gridline to the channel mid-line, then
into its slot, e.g. parking a piece from `e5` into the black knight slot:

```
4,4:4,4.5:8.5,4.5:9,4|
```

The model lives in `graveyard.py` (`Graveyard.alloc/take`) and `squareoff.py`
(`plan_park` / `plan_retrieve`).

> ⚠️ **Two important lessons from hardware testing:**
> 1. Motion commands must be sent in a **single BLE write** — chunking a long
>    path (>20 bytes) makes the board see a truncated command and disconnect.
>    (Fixed via `_write_once`.)
> 2. **Never route to negative-y / off the bottom edge** — it grinds the motor
>    against the frame. Parking only ever uses the side banks.
>
> Use `python park_probe.py --list` to test motion paths one at a time.

## Protocol cheat-sheet

Messages look like `<commandId>#<data>*`.

| Direction | Message | Meaning |
|-----------|---------|---------|
| app → board | `14#1*` | start game (standard position) |
| board → app | `14#GO*` | handshake ack |
| board → app | `0#e2u*` | piece lifted on e2 |
| board → app | `0#e4d*` | piece placed on e4 |
| app → board | `30#R*` | read board occupancy |
| board → app | `30#<64x 0/1>*` | occupancy (a1-a8, b1-b8, ...) |
| app → board | `30#<64x 0/1>*` | set custom position |
| app → board | `25#e2e4*` | light LEDs on squares |
| app → board | `26#ISG*` | in-sync ack |
| app → board | `27#ck*` | king-in-check sound |
| app → board | `27#wt*` / `27#bl*` / `27#dw*` | white / black / draw result |
| app → board | `4#*` | battery request |
| board → app | `22#3.52*` | battery value |

## Notes / troubleshooting

- The board may need to be freshly powered on and not connected to the official
  app for the Pi to see it.
- If the board rejects long writes, the script already chunks writes to 20 bytes.
- Occupancy is **file-major**: index = `file*8 + rank` where files a..h = 0..7
  and ranks 1..8 = 0..7.

