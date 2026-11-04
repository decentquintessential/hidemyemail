#!/bin/bash
# Installs a downloaded Hide My Email update. Run by the app, which then quits:
#   update.sh <app pid> <downloaded .dmg> <path of the installed .app>
# Waits for the app to exit, swaps in the new version and reopens it. On any
# failure the old version stays in place and is reopened. Logs to update.log.
set -u
PID="$1" DMG="$2" DEST="$3"
LOG="$HOME/Library/Application Support/hme/update.log"
mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1
echo "=== $(date): updating $DEST from $DMG"

for _ in $(seq 1 120); do kill -0 "$PID" 2>/dev/null || break; sleep 0.5; done

MNT="$(mktemp -d /tmp/hme-update.XXXXXX)"
NEW="$(dirname "$DEST")/.Hide My Email.updating.app"
OLD="$(dirname "$DEST")/.Hide My Email.previous.app"
cleanup() { hdiutil detach "$MNT" -quiet -force 2>/dev/null; rmdir "$MNT" 2>/dev/null; rm -rf "$NEW"; }
fail() { echo "FAILED: $1"; cleanup; open "$DEST"; exit 1; }

hdiutil attach "$DMG" -nobrowse -noautoopen -readonly -mountpoint "$MNT" -quiet || fail "could not open the disk image"
[ -d "$MNT/Hide My Email.app" ] || fail "no app in the disk image"
rm -rf "$NEW" "$OLD"
ditto "$MNT/Hide My Email.app" "$NEW" || fail "could not copy the new version"
codesign --verify "$NEW" || fail "the new version's signature is invalid"
xattr -dr com.apple.quarantine "$NEW" 2>/dev/null

# Swap: keep the old copy until the new one is in place.
mv "$DEST" "$OLD" || fail "could not move the old version aside"
if ! mv "$NEW" "$DEST"; then mv "$OLD" "$DEST"; fail "could not move the new version in place"; fi
rm -rf "$OLD"
cleanup
echo "updated to $(/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' "$DEST/Contents/Info.plist")"
open "$DEST"
