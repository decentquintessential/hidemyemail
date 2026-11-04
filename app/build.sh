#!/bin/zsh
# Builds "Hide My Email.app" next to this script, using the Swift compiler
# from Xcode or the Command Line Tools.
#
#   app/build.sh                   developer build: runs hme_helper.py with ~/.local/share/hme/venv
#   app/build.sh --standalone      self-contained build: bundles the helper (needs pyinstaller)
#   app/build.sh --standalone --dmg   ...and packages it as "Hide My Email.dmg"
#
# APP_VERSION (e.g. 1.0.20) sets the version shown in Finder; default 1.0.
set -e
HERE="${0:A:h}"
APP="$HERE/Hide My Email.app"
VERSION="${APP_VERSION:-1.0}"
STANDALONE=0
DMG=0
for arg in "$@"; do
  case "$arg" in
    --standalone) STANDALONE=1 ;;
    --dmg) DMG=1 ;;
    *) echo "unknown option $arg" >&2; exit 2 ;;
  esac
done

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

swiftc -O -swift-version 5 -parse-as-library \
  -target "$(uname -m)-apple-macos14.0" \
  -o "$APP/Contents/MacOS/HideMyEmail" \
  "$HERE/HideMyEmail.swift"

if [[ $STANDALONE == 1 ]]; then
  # Freeze the helper with its own Python, so the app needs nothing installed.
  WORK="$(mktemp -d)"
  ${PYTHON:-python3} -m PyInstaller --noconfirm --onedir --console --name hme-helper \
    --distpath "$WORK/dist" --workpath "$WORK/build" --specpath "$WORK" \
    --collect-submodules pyicloud --collect-submodules keyring.backends \
    --collect-data pyicloud --collect-data fido2 \
    --copy-metadata pyicloud --copy-metadata keyring \
    "$HERE/../hme_helper.py" >"$WORK/pyinstaller.log" 2>&1 \
    || { tail -30 "$WORK/pyinstaller.log"; exit 1; }
  cp -R "$WORK/dist/hme-helper" "$APP/Contents/Resources/hme-helper"
else
  cp "$HERE/../hme_helper.py" "$APP/Contents/Resources/hme_helper.py"
fi

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Hide My Email</string>
  <key>CFBundleDisplayName</key><string>Hide My Email</string>
  <key>CFBundleIdentifier</key><string>local.hme.HideMyEmail</string>
  <key>CFBundleExecutable</key><string>HideMyEmail</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF

# App icon: an SF Symbol drawn onto a rounded tile.
ICONSET="$(mktemp -d)/AppIcon.iconset"
mkdir -p "$ICONSET"
swift "$HERE/make_icon.swift" "$ICONSET" >/dev/null 2>&1 \
  && iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns" \
  || echo "(skipped icon)"

# Ad-hoc signatures (no Apple developer certificate): sign the helper's
# binaries first, then the app, so macOS accepts every piece of code.
if [[ $STANDALONE == 1 ]]; then
  find "$APP/Contents/Resources/hme-helper" -type f \( -perm -u+x -o -name '*.dylib' -o -name '*.so' \) \
    -exec codesign --force --sign - {} \; 2>/dev/null
fi
codesign --force --sign - "$APP" >/dev/null
codesign --verify "$APP"
echo "Built: $APP"

if [[ $DMG == 1 ]]; then
  STAGE="$(mktemp -d)/Hide My Email"
  mkdir -p "$STAGE"
  cp -R "$APP" "$STAGE/"
  ln -s /Applications "$STAGE/Applications"
  rm -f "$HERE/Hide My Email.dmg"
  hdiutil create -volname "Hide My Email" -srcfolder "$STAGE" -ov -format UDZO "$HERE/Hide My Email.dmg" >/dev/null
  echo "Packaged: $HERE/Hide My Email.dmg"
fi
