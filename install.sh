#!/usr/bin/env bash
# install.sh — install bounce into a target Claude Code project.
#
# Usage:
#   ./install.sh /path/to/your/project
#   ./install.sh                          # installs into current directory
#
# Copies bounce.py, bounce.md, and a starter config into the target project's
# .claude/ tree. Does not overwrite existing files unless --force is passed.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-$PWD}"
FORCE="${2:-}"

if [ ! -d "$TARGET" ]; then
  echo "error: target directory does not exist: $TARGET" >&2
  exit 1
fi

TARGET="$(cd "$TARGET" && pwd)"
echo "Installing bounce into: $TARGET"

# Create the directory structure
mkdir -p "$TARGET/.claude/scripts/bounce-presets"
mkdir -p "$TARGET/.claude/commands"

copy_file() {
  local src="$1"
  local dest="$2"
  if [ -f "$dest" ] && [ "$FORCE" != "--force" ]; then
    echo "  skip:  $dest (already exists; use --force to overwrite)"
  else
    cp "$src" "$dest"
    echo "  copy:  $dest"
  fi
}

copy_file "$REPO_DIR/scripts/bounce.py" "$TARGET/.claude/scripts/bounce.py"
copy_file "$REPO_DIR/commands/bounce.md" "$TARGET/.claude/commands/bounce.md"

chmod +x "$TARGET/.claude/scripts/bounce.py"

# Starter config — only create if missing
if [ ! -f "$TARGET/.claude/bounce-config.json" ]; then
  cat > "$TARGET/.claude/bounce-config.json" <<'EOF'
{
  "presets": [],
  "extra_denylist_patterns": [],
  "_note": "Add preset names (e.g. 'my-project') to enable project-specific denylist patterns. Define presets at .claude/scripts/bounce-presets/<name>.json. See repo presets/ directory for examples."
}
EOF
  echo "  create: $TARGET/.claude/bounce-config.json (empty starter)"
fi

echo ""
echo "Install complete."
echo ""
echo "Next steps:"
echo "  1. Set OPENAI_API_KEY in ~/.claude/settings.json (env block) — NOT in ~/.zshrc."
echo "     See README for details."
echo "  2. Configure pricing at ~/.claude/bounce-pricing.json — see README for schema."
echo "  3. Restart Claude Code in $TARGET so the /bounce slash command picks up."
echo "  4. Test with:  echo 'review this design' | $TARGET/.claude/scripts/bounce.py"
echo ""
echo "Optional:"
echo "  - To enable a preset (project-specific PII denylist), copy a preset JSON file"
echo "    from this repo's presets/ directory into $TARGET/.claude/scripts/bounce-presets/"
echo "    and add the name to 'presets' in $TARGET/.claude/bounce-config.json."
