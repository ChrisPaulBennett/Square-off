#!/usr/bin/env bash
# Convenience wrapper: copy bleak's python-for-android recipe into ./p4a-recipes,
# make sure buildozer.spec points at it, then build the debug APK.
# Run this on Linux (or WSL2) after: pip install "buildozer==1.5.0" "cython<3.0"
set -euo pipefail

cd "$(dirname "$0")"

echo "==> Locating bleak's Android recipe…"
RECIPE_SRC="$(python3 -c 'import bleak, os; print(os.path.join(os.path.dirname(bleak.__file__), "backends", "p4android", "recipes"))')"

if [ ! -d "$RECIPE_SRC/bleak" ]; then
    echo "!! Could not find bleak's p4android recipe at: $RECIPE_SRC" >&2
    echo "   Is bleak installed?  pip install bleak" >&2
    exit 1
fi

echo "==> Copying recipe into ./p4a-recipes"
mkdir -p p4a-recipes
cp -r "$RECIPE_SRC/bleak" p4a-recipes/

# Enable the local_recipes line in buildozer.spec if it's still commented out.
if grep -q '^# *p4a.local_recipes' buildozer.spec; then
    echo "==> Enabling p4a.local_recipes in buildozer.spec"
    sed -i 's|^# *p4a.local_recipes.*|p4a.local_recipes = ./p4a-recipes|' buildozer.spec
fi

echo "==> Building debug APK (first run downloads the Android SDK/NDK; be patient)…"
buildozer android debug

echo
echo "==> Done. Look in ./bin for the .apk"

