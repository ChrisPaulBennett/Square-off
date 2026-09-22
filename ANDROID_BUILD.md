# Building Square Off as an Android APK

This packages the existing Flask web UI (`app.py`) into a native Android app.
Flask runs *inside* the app and the UI is shown in a native WebView, while
`bleak` drives the board over Bluetooth using Android's own Bluetooth stack.

> **Why not Termux / a plain web server?** Those can run the Python code, but
> they can't reach Bluetooth Low Energy on a normal (non-rooted) Android phone,
> so the board would never move. A real APK built with python-for-android is the
> only route that gives working BLE, because `bleak` has a native Android
> backend that goes through Android's Java Bluetooth APIs.

## What you get

- One tappable **Square Off** app icon.
- On launch it asks for Bluetooth + location permissions, starts the Flask
  server locally, and opens the normal web UI full-screen.
- **Scan → Connect → New game** works exactly like the desktop version. The
  built-in fallback engine is used for offline bots (Stockfish is desktop-only);
  Lichess online play and the chess.com daily puzzle work over the phone's
  internet connection.

## Prerequisites (build on Linux, or WSL2 / a Linux VM on Windows/Mac)

Buildozer only builds on Linux. On a Debian/Ubuntu machine:

```bash
sudo apt update
sudo apt install -y git zip unzip openjdk-17-jdk python3-pip \
    autoconf libtool pkg-config zlib1g-dev libncurses5-dev \
    libncursesw5-dev libtinfo5 cmake libffi-dev libssl-dev build-essential ccache

python3 -m pip install --user --upgrade pip
python3 -m pip install --user "buildozer==1.5.0" "cython<3.0"
```

Make sure `~/.local/bin` is on your `PATH` (so `buildozer` is found).

## One extra step: the bleak Android recipe

`bleak`'s Android backend ships a small amount of Java that must be compiled
into the APK. `bleak` bundles a python-for-android recipe for exactly this.
Point Buildozer at it:

1. Find the recipe folder in your installed `bleak`:

   ```bash
   python3 -c "import bleak, os; print(os.path.join(os.path.dirname(bleak.__file__), 'backends', 'p4android', 'recipes'))"
   ```

   That prints something like
   `.../site-packages/bleak/backends/p4android/recipes` — it contains a `bleak/`
   sub-folder (the recipe).

2. Copy it next to this project and enable it in `buildozer.spec`:

   ```bash
   mkdir -p p4a-recipes
   cp -r "$(python3 -c 'import bleak, os; print(os.path.join(os.path.dirname(bleak.__file__), "backends", "p4android", "recipes"))')/bleak" p4a-recipes/
   ```

   Then uncomment this line in `buildozer.spec`:

   ```ini
   p4a.local_recipes = ./p4a-recipes
   ```

## Build the APK

From this project folder:

```bash
buildozer android debug
```

The first run downloads the Android SDK/NDK and compiles everything — it can
take 20–40+ minutes and a few GB of disk. When it finishes, the APK is in:

```
bin/squareoff-1.0-arm64-v8a_armeabi-v7a-debug.apk
```

## Install on your phone

Either:

```bash
# Phone connected by USB with "USB debugging" enabled:
buildozer android deploy run logcat
```

or copy the `.apk` from `bin/` to the phone and tap it (you'll need to allow
"install from unknown sources").

## Using it

1. Turn the board on (and make sure the official Square Off app is **not**
   connected to it).
2. Open **Square Off**, accept the Bluetooth/Location prompts.
3. **Scan**, pick your board, **Connect**, then **New game** — same flow as the
   README's web UI.

## Notes & limitations

- **Permissions:** Android 12+ needs *Nearby devices* (Bluetooth) granted;
  older Android needs *Location* enabled for BLE scanning. The app requests
  these on launch — grant them, or scanning returns nothing.
- **No Stockfish:** the desktop build can use a Stockfish binary; on Android the
  app automatically falls back to the built-in engine (`bots.py`). Lichess AI
  levels still give strong online play.
- **First build is slow / needs internet.** Rebuilds are fast.
- **iPhone:** this route is Android-only. iOS can't be built this way; a browser
  can't reach BLE either. iOS would need a native Swift/CoreBluetooth rewrite.

## Troubleshooting

- `buildozer` not found → add `~/.local/bin` to `PATH`.
- Java errors → ensure `openjdk-17-jdk` is the active JDK.
- Cython errors → use `cython<3.0` as pinned above.
- BLE scan finds nothing on the phone → confirm the *Nearby devices* /
  *Location* permission was granted and Bluetooth + Location are switched on.
- Watch runtime logs with `buildozer android logcat` (filter with
  `logcat | grep -i python`).

