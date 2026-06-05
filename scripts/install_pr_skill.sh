#!/usr/bin/env bash
# Install the lightweight /pr skill for local AI agents.

set -euo pipefail

FORCE=0
if [[ "${1:-}" == "--force" ]]; then
    FORCE=1
elif [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    cat <<'EOF'
Usage:
  bash scripts/install_pr_skill.sh [--force]

Installs .agents/skills/pr into:
  ~/.agents/skills/pr   shared agent skills
  ~/.claude/skills/pr   Claude Code global skills
  ~/.codex/skills/pr    Codex global skills
EOF
    exit 0
elif [[ $# -gt 0 ]]; then
    echo "ERROR: unknown argument: $1" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SOURCE="$ROOT/.agents/skills/pr"

if [[ ! -f "$SOURCE/SKILL.md" ]]; then
    echo "ERROR: source skill missing: $SOURCE" >&2
    exit 1
fi

install_one() {
    local target_root="$1"
    local target="$target_root/pr"

    mkdir -p "$target_root"
    if [[ -e "$target" ]]; then
        if [[ "$FORCE" -ne 1 ]]; then
            echo "ERROR: $target already exists; rerun with --force to replace it" >&2
            exit 1
        fi
        rm -rf "$target"
    fi

    cp -R "$SOURCE" "$target"
    chmod +x "$target/scripts/light_pr.sh"
    echo "Installed $target"
}

install_one "$HOME/.agents/skills"
install_one "$HOME/.claude/skills"
install_one "$HOME/.codex/skills"

echo "Done. Restart Claude Code/Codex sessions for the new /pr skill to appear."
