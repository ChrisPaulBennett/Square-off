#!/usr/bin/env python3
"""Android entry point for the Square Off app.

python-for-android's *webview* bootstrap runs this file and, in parallel, shows a
native Android WebView pointed at http://127.0.0.1:5000. So all we have to do
here is:

  1. Ask for the Bluetooth / location runtime permissions bleak needs.
  2. Start the existing Flask UI (app.py) on port 5000 and keep it running.

The very same code powers the desktop/Raspberry-Pi build, so the on-phone UI is
identical to `python app.py`. On a normal computer this file just launches Flask,
so you can smoke-test it with `python main.py` before building the APK.
"""

from __future__ import annotations

import sys
import traceback

# The port the webview bootstrap loads by default.
PORT = 5000


def _request_android_permissions() -> None:
    """Request the runtime permissions BLE needs (no-op off Android)."""
    try:
        from android.permissions import request_permissions, Permission
    except ImportError:
        return  # not running on Android

    perms = []
    for name in (
        "BLUETOOTH",
        "BLUETOOTH_ADMIN",
        "BLUETOOTH_SCAN",       # Android 12+ (API 31)
        "BLUETOOTH_CONNECT",    # Android 12+ (API 31)
        "ACCESS_FINE_LOCATION",     # required for BLE scan on older Androids
        "ACCESS_COARSE_LOCATION",
    ):
        perm = getattr(Permission, name, None)
        if perm is not None:
            perms.append(perm)

    if perms:
        try:
            request_permissions(perms)
        except Exception:  # noqa: BLE001 - never let permissions crash startup
            traceback.print_exc()


def main() -> None:
    # Any exception here (e.g. an import that touches a read-only path) would
    # otherwise kill the whole app on launch with no trace. Log it to logcat so
    # it can be diagnosed with `buildozer android logcat | grep -i python`.
    try:
        _request_android_permissions()

        # Import here so the permission prompt shows before we touch Bluetooth.
        from app import app as flask_app

        # host=0.0.0.0 so the in-app WebView (127.0.0.1) can reach it; no reloader
        # (it would try to fork a second process, which p4a can't do).
        flask_app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.stderr.flush()
        raise


if __name__ == "__main__":
    main()

