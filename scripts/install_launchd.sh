#!/usr/bin/env bash
# Generate ~/Library/LaunchAgents/com.<user>.calendar.daily.plist from template
# and load it. Re-running is safe (unloads first if already present).

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

USER_TAG="${USER:-calendar}"
LABEL="com.${USER_TAG}.calendar.daily"
TEMPLATE="${ROOT}/com.calendar.daily.plist.template"
TARGET="${HOME}/Library/LaunchAgents/${LABEL}.plist"

if [[ ! -f "${TEMPLATE}" ]]; then
    echo "✗ template not found: ${TEMPLATE}" >&2
    exit 1
fi

mkdir -p "$(dirname "${TARGET}")"
mkdir -p "${ROOT}/logs"

echo "→ rendering ${TARGET}"
sed -e "s|__INSTALL_DIR__|${ROOT}|g" \
    -e "s|__LABEL__|${LABEL}|g" \
    "${TEMPLATE}" > "${TARGET}"

if launchctl list 2>/dev/null | grep -q "${LABEL}"; then
    echo "→ unloading existing job"
    launchctl unload "${TARGET}" 2>/dev/null || true
fi

echo "→ loading"
launchctl load "${TARGET}"

echo
echo "✓ installed: ${TARGET}"
echo "  next fire: every day at 06:00 local"
echo
launchctl list | grep "${LABEL}" || true
