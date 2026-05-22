#!/bin/bash
# Install MyCalFix.app into ~/Applications and register the mycalfix:// URL scheme.
#
# Idempotent: re-running upgrades the existing app in-place. Run after every
# change to app/MyCalFix/main.applescript or scripts/launch_fix.sh.

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)

SRC_APPLESCRIPT="$ROOT/app/MyCalFix/main.applescript"
LAUNCHER="$HERE/launch_fix.sh"
URL_PARSER="$HERE/parse_fix_url.py"
PROMPT="$HERE/fix_prompt.md"

APP_DIR="$HOME/Applications"
APP_NAME="MyCalFix.app"
APP_PATH="$APP_DIR/$APP_NAME"
PLIST="$APP_PATH/Contents/Info.plist"
RESOURCES="$APP_PATH/Contents/Resources"
BUNDLE_ID="com.wuyupeng.mycalfix"
URL_SCHEME="mycalfix"

if [[ ! -f "$SRC_APPLESCRIPT" ]]; then
  echo "✗ source AppleScript not found: $SRC_APPLESCRIPT" >&2
  exit 1
fi
if [[ ! -x "$LAUNCHER" ]]; then
  echo "✗ launcher not executable: $LAUNCHER" >&2
  echo "  fix with: chmod +x $LAUNCHER" >&2
  exit 1
fi
if [[ ! -f "$URL_PARSER" ]]; then
  echo "✗ url parser not found: $URL_PARSER" >&2
  exit 1
fi
if [[ ! -f "$PROMPT" ]]; then
  echo "✗ fix prompt not found: $PROMPT" >&2
  exit 1
fi

mkdir -p "$APP_DIR"

# Recompile: remove previous .app if present (osacompile won't overwrite).
if [[ -e "$APP_PATH" ]]; then
  echo "  → removing existing $APP_PATH"
  rm -rf "$APP_PATH"
fi

echo "  → compiling $APP_PATH"
osacompile -o "$APP_PATH" "$SRC_APPLESCRIPT"

# Copy launcher + prompt into the bundle so the .app is fully self-contained.
# The AppleScript resolves them via `path to me` → Contents/Resources/. This
# matters because the repo may live under a TCC-protected folder (~/Desktop,
# ~/Documents, ~/Downloads) — reading scripts/launch_fix.sh from there would
# EPERM. Files inside the .app bundle are not TCC-gated for the .app itself.
echo "  → installing launcher + url parser + prompt into $RESOURCES"
cp "$LAUNCHER" "$RESOURCES/launch_fix.sh"
chmod +x "$RESOURCES/launch_fix.sh"
cp "$URL_PARSER" "$RESOURCES/parse_fix_url.py"
cp "$PROMPT" "$RESOURCES/fix_prompt.md"

if [[ ! -f "$PLIST" ]]; then
  echo "✗ compiled app missing Info.plist: $PLIST" >&2
  exit 2
fi

# Patch Info.plist: bundle identifier + URL scheme registration.
plutil -replace CFBundleIdentifier -string "$BUNDLE_ID" "$PLIST"

# CFBundleURLTypes is an array of dicts; remove any existing then insert ours.
if /usr/libexec/PlistBuddy -c "Print :CFBundleURLTypes" "$PLIST" >/dev/null 2>&1; then
  /usr/libexec/PlistBuddy -c "Delete :CFBundleURLTypes" "$PLIST"
fi
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes array" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0 dict" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLName string $BUNDLE_ID" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes array" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes:0 string $URL_SCHEME" "$PLIST"

# LSUIElement = true → no Dock icon, no menu bar, no app-switcher entry.
# This is a one-shot URL handler; surfacing it as a normal app is just noise.
if /usr/libexec/PlistBuddy -c "Print :LSUIElement" "$PLIST" >/dev/null 2>&1; then
  /usr/libexec/PlistBuddy -c "Set :LSUIElement true" "$PLIST"
else
  /usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$PLIST"
fi

# Strip quarantine xattr so first click doesn't trigger Gatekeeper warning.
# (Self-built apps are unsigned; Gatekeeper will warn unless we remove the attr.)
xattr -dr com.apple.quarantine "$APP_PATH" 2>/dev/null || true

# Re-register with LaunchServices so it picks up the new CFBundleURLTypes.
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
if [[ -x "$LSREGISTER" ]]; then
  "$LSREGISTER" -f "$APP_PATH" >/dev/null 2>&1 || true
  echo "  → lsregister: $APP_PATH"
fi

echo
echo "✅ installed $APP_PATH"
echo "   bundle:    $BUNDLE_ID"
echo "   scheme:    $URL_SCHEME://"
echo "   launcher:  $RESOURCES/launch_fix.sh  (bundled)"
echo "   parser:    $RESOURCES/parse_fix_url.py  (bundled)"
echo "   prompt:    $RESOURCES/fix_prompt.md  (bundled)"
echo
echo "smoke test:"
echo "  open 'mycalfix://fix?repo=realRoc%2Fmy-calendar&branch=main&comment=https%3A%2F%2Fgithub.com%2FrealRoc%2Fmy-calendar%2Fpull%2F10%23issuecomment-1&pr=https%3A%2F%2Fgithub.com%2FrealRoc%2Fmy-calendar%2Fpull%2F10&origin_cwd=$ROOT'"
echo "  → should open Terminal, cd into $ROOT, attempt git fetch/checkout main, and start claude"
echo
echo "tail logs:  tail -f \$HOME/Library/Logs/MyCalFix/launch_fix.log"
