#!/usr/bin/env bash
# Install LaunchAgents for my-calendar.
#
# Two jobs:
#   1. com.<user>.calendar.daily       — every day at 06:00 (节日扫描)
#   2. com.<user>.calendar.pr-watcher  — every 10 min while awake (PR 监控)
#
# Re-running is safe: each job is unloaded first if already present.

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

USER_TAG="${USER:-calendar}"

declare -a JOBS=(
    "daily:com.calendar.daily.plist.template"
    "pr-watcher:com.calendar.pr-watcher.plist.template"
)

mkdir -p "${HOME}/Library/LaunchAgents"
mkdir -p "${ROOT}/logs"

for entry in "${JOBS[@]}"; do
    suffix="${entry%%:*}"
    template_name="${entry##*:}"
    template="${ROOT}/${template_name}"
    label="com.${USER_TAG}.calendar.${suffix}"
    target="${HOME}/Library/LaunchAgents/${label}.plist"

    if [[ ! -f "${template}" ]]; then
        echo "✗ template not found: ${template}" >&2
        exit 1
    fi

    echo "→ rendering ${target}"
    sed -e "s|__INSTALL_DIR__|${ROOT}|g" \
        -e "s|__LABEL__|${label}|g" \
        -e "s|__HOME__|${HOME}|g" \
        "${template}" > "${target}"

    if launchctl list 2>/dev/null | grep -q "${label}"; then
        echo "→ unloading existing job ${label}"
        launchctl unload "${target}" 2>/dev/null || true
    fi

    echo "→ loading ${label}"
    launchctl load "${target}"
done

echo
echo "✓ installed:"
launchctl list | grep "com.${USER_TAG}.calendar\." || true
echo
echo "  daily      → ${ROOT}/logs/daily.log"
echo "  pr-watcher → ${ROOT}/logs/pr-watcher.log"
