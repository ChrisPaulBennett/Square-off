[app]

# --- App identity ---------------------------------------------------------
title = Square Off
package.name = squareoff
package.domain = org.squareoff

# --- Source ---------------------------------------------------------------
# Build from this folder. main.py is the entry point (starts the Flask UI).
source.dir = .
source.include_exts = py,html,css,js,png,jpg,jpeg,gif,svg,ico,txt,json
# templates/ (index.html) and any static/ assets are picked up by the globs above.
# Keep build/packaging junk out of the APK.
source.exclude_dirs = __pycache__, bin, .buildozer, .git
source.exclude_patterns = Square-off.zip, *.pyc, park_probe.py

version = 1.0

# --- Python / native requirements -----------------------------------------
# bleak       -> BLE (uses its native Android backend via pyjnius)
# pyjnius     -> Java bridge that bleak's Android backend needs
# android     -> runtime permission helpers (android.permissions)
# chess       -> python-chess game logic
# flask       -> the existing web UI
# requests(+deps)/openssl -> chess.com daily-puzzle fetch over HTTPS
requirements = python3,flask,bleak,pyjnius,android,chess,requests,urllib3,idna,charset-normalizer,certifi,openssl

# --- Bootstrap: run the Flask server, show it in a native WebView ----------
p4a.bootstrap = webview
# The WebView loads http://127.0.0.1:<port>; keep this in step with main.py.
p4a.port = 5000

# bleak ships a python-for-android recipe that compiles its Java classes into
# the APK. Point local_recipes at it (see ANDROID_BUILD.md for how to locate it),
# then uncomment the line below:
# p4a.local_recipes = ./p4a-recipes

# --- Android settings -----------------------------------------------------
orientation = portrait
fullscreen = 0

# INTERNET is needed so the WebView can talk to the local Flask server.
android.permissions = INTERNET, BLUETOOTH, BLUETOOTH_ADMIN, BLUETOOTH_SCAN, BLUETOOTH_CONNECT, ACCESS_FINE_LOCATION, ACCESS_COARSE_LOCATION

android.api = 34
android.minapi = 24
android.archs = arm64-v8a, armeabi-v7a
android.allow_backup = 1

# Keep the screen awake while a game/motor move is running (optional).
android.wakelock = 1


[buildozer]
log_level = 2
warn_on_root = 1

