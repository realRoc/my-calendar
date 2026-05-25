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
CONFIG_HELPER="$HERE/mycalfix_config.py"
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
if [[ ! -f "$CONFIG_HELPER" ]]; then
  echo "✗ config helper not found: $CONFIG_HELPER" >&2
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
echo "  → installing launcher + url parser + config helper + prompt into $RESOURCES"
cp "$LAUNCHER" "$RESOURCES/launch_fix.sh"
chmod +x "$RESOURCES/launch_fix.sh"
cp "$URL_PARSER" "$RESOURCES/parse_fix_url.py"
# launch_fix.sh reads `mycalfix_config.py claude-flag` to decide whether to add
# `--dangerously-skip-permissions`. Missing this file → the helper call fails
# → launch_fix.sh's fail-safe defaults to yolo. That silently nullifies the
# `mycalfix_interactive_claude: true` safety valve in deployed installs, so
# the helper MUST be bundled alongside launch_fix.sh.
cp "$CONFIG_HELPER" "$RESOURCES/mycalfix_config.py"
cp "$PROMPT" "$RESOURCES/fix_prompt.md"

# Verify the bundled parser is actually runnable end-to-end. Catches a missing
# python3 on $PATH or a syntactic regression in parse_fix_url.py at install
# time, rather than at the moment the user clicks a calendar link and gets a
# silent failure (alert path was added in launch_fix.sh as a runtime fallback).
echo "  → smoke-testing bundled parser"
SMOKE_URL='mycalfix://fix?repo=foo%2Fbar&branch=main&comment=https%3A%2F%2Fgithub.com%2Ffoo%2Fbar%2Fpull%2F1&pr=https%3A%2F%2Fgithub.com%2Ffoo%2Fbar%2Fpull%2F1'
if ! smoke_out=$(python3 "$RESOURCES/parse_fix_url.py" "$SMOKE_URL" 2>&1); then
  echo "✗ bundled parser failed to run: $smoke_out" >&2
  echo "  (python3 may not be available, or parse_fix_url.py is broken)" >&2
  exit 3
fi
if [[ "$smoke_out" == *URL_ERROR=* ]]; then
  echo "✗ bundled parser rejected a known-good URL — regression?" >&2
  echo "$smoke_out" >&2
  exit 3
fi

# Smoke-test the bundled config helper exactly the way launch_fix.sh calls it.
# Use a clean HOME so the user's real ~/.config/my-calendar/config.json doesn't
# influence the assertion. Default contract: `claude-flag` prints the empty
# string (interactive — claude asks for approval on every tool call). If the
# helper isn't bundled or emits the yolo flag by default, launch_fix.sh would
# silently upgrade every click to no-approval execution. Catch it at install.
echo "  → smoke-testing bundled config helper"
CONFIG_TMP_HOME=$(mktemp -d)
if ! cfg_out=$(HOME="$CONFIG_TMP_HOME" python3 "$RESOURCES/mycalfix_config.py" claude-flag 2>&1); then
  echo "✗ bundled mycalfix_config.py failed to run: $cfg_out" >&2
  rm -rf "$CONFIG_TMP_HOME"
  exit 3
fi
rm -rf "$CONFIG_TMP_HOME"
if [[ -n "$(printf '%s' "$cfg_out" | tr -d '[:space:]')" ]]; then
  echo "✗ bundled mycalfix_config.py emitted unexpected claude-flag: $cfg_out" >&2
  echo "  (expected empty string under a clean HOME — interactive is the safe default)" >&2
  exit 3
fi

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

# TCC usage descriptions for Desktop/Documents/Downloads. Required for the
# bundled launcher to run `git -C <origin_cwd>` when the user's repo lives
# under one of these folders — without these keys macOS silently denies file
# access and git fails with "Operation not permitted" (looks like "not a git
# worktree" in the log). With these keys, the first click triggers a TCC
# prompt; user grants once, future clicks are silent.
#
# Uses plutil rather than PlistBuddy: PlistBuddy's command tokenizer trips on
# apostrophes/spaces inside the value string, plutil takes the value as a
# single shell-quoted arg.
TCC_MSG="MyCalFix needs to access this folder to read your local checkout of the PR repository."
for KEY in NSDesktopFolderUsageDescription NSDocumentsFolderUsageDescription NSDownloadsFolderUsageDescription; do
  # -replace creates the key if absent, overwrites if present.
  plutil -replace "$KEY" -string "$TCC_MSG" "$PLIST"
done

# Strip quarantine xattr so first click doesn't trigger Gatekeeper warning.
# (Self-built apps are unsigned; Gatekeeper will warn unless we remove the attr.)
xattr -dr com.apple.quarantine "$APP_PATH" 2>/dev/null || true

# Re-register with LaunchServices so it picks up the new CFBundleURLTypes.
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
if [[ -x "$LSREGISTER" ]]; then
  "$LSREGISTER" -f "$APP_PATH" >/dev/null 2>&1 || true
  echo "  → lsregister: $APP_PATH"
fi

# Reset TCC decisions for this bundle id. macOS persists "deny" verdicts per
# bundle id; rebuilding the .app does NOT clear them. Without this, a user who
# accidentally dismissed an old TCC prompt would have every future click
# silently denied. tccutil exits non-zero when there are no records to clear,
# which is fine — that's exactly the state we're trying to reach.
echo "  → tccutil reset for $BUNDLE_ID"
tccutil reset All "$BUNDLE_ID" >/dev/null 2>&1 || true

echo
echo "✅ installed $APP_PATH"
echo "   bundle:    $BUNDLE_ID"
echo "   scheme:    $URL_SCHEME://"
echo "   launcher:  $RESOURCES/launch_fix.sh  (bundled)"
echo "   parser:    $RESOURCES/parse_fix_url.py  (bundled)"
echo "   config:    $RESOURCES/mycalfix_config.py  (bundled)"
echo "   prompt:    $RESOURCES/fix_prompt.md  (bundled)"
echo
echo "smoke test:"
echo "  open 'mycalfix://fix?repo=realRoc%2Fmy-calendar&branch=main&comment=https%3A%2F%2Fgithub.com%2FrealRoc%2Fmy-calendar%2Fpull%2F10%23issuecomment-1&pr=https%3A%2F%2Fgithub.com%2FrealRoc%2Fmy-calendar%2Fpull%2F10&origin_cwd=$ROOT'"
echo "  → should open Terminal, cd into $ROOT, attempt git fetch/checkout main, and start claude"
echo
echo "tail logs:  tail -f \$HOME/Library/Logs/MyCalFix/launch_fix.log"
