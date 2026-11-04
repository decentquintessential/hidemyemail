#!/bin/zsh
# Builds "Hide My Email.app" next to this script, using the Swift compiler
# from Xcode or the Command Line Tools.
set -e
HERE="${0:A:h}"
APP="$HERE/Hide My Email.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

swiftc -O -swift-version 5 -parse-as-library \
  -target "$(uname -m)-apple-macos14.0" \
  -o "$APP/Contents/MacOS/HideMyEmail" \
  "$HERE/HideMyEmail.swift"

cp "$HERE/../hme_helper.py" "$APP/Contents/Resources/hme_helper.py"

cat > "$APP/Contents/Info.plist" <<'EOF'
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
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
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

codesign --force --sign - "$APP" >/dev/null
echo "Built: $APP"
