#!/usr/bin/env bash
# Runs once when the container is first created (cached in prebuilds).
# Installs tooling that doesn't change with source code updates.
set -e

# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# Install doover-cli
uv tool install doover-cli

# Set up doover-skills plugin for Claude Code
CLAUDE_DIR="$HOME/.claude"
PLUGINS_DIR="$CLAUDE_DIR/plugins"
MARKETPLACES_DIR="$PLUGINS_DIR/marketplaces"
DOOVER_SKILLS_DIR="$MARKETPLACES_DIR/doover-skills"

mkdir -p "$PLUGINS_DIR/cache" "$PLUGINS_DIR/data" "$MARKETPLACES_DIR"

# Clone doover-skills marketplace
if [ ! -d "$DOOVER_SKILLS_DIR" ]; then
    git clone https://github.com/getdoover/doover-skills.git "$DOOVER_SKILLS_DIR"
fi

# Register the marketplace
cat > "$PLUGINS_DIR/known_marketplaces.json" << EOF
{
  "doover-skills": {
    "source": {
      "source": "github",
      "repo": "getdoover/doover-skills"
    },
    "installLocation": "$DOOVER_SKILLS_DIR",
    "autoUpdate": true
  }
}
EOF

# Register the installed plugin
COMMIT_SHA=$(cd "$DOOVER_SKILLS_DIR" && git rev-parse HEAD)
CACHE_DIR="$PLUGINS_DIR/cache/doover-skills/doover-development/1.1.2"
mkdir -p "$CACHE_DIR"
cp -r "$DOOVER_SKILLS_DIR"/* "$CACHE_DIR/" 2>/dev/null || true

cat > "$PLUGINS_DIR/installed_plugins.json" << EOF
{
  "version": 2,
  "plugins": {
    "doover-development@doover-skills": [
      {
        "scope": "user",
        "installPath": "$CACHE_DIR",
        "version": "1.1.2",
        "installedAt": "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)",
        "lastUpdated": "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)",
        "gitCommitSha": "$COMMIT_SHA"
      }
    ]
  }
}
EOF

# Enable the plugin in Claude Code settings
mkdir -p "$CLAUDE_DIR"
if [ ! -f "$CLAUDE_DIR/settings.json" ]; then
    cat > "$CLAUDE_DIR/settings.json" << 'EOF'
{
  "enabledPlugins": {
    "doover-development@doover-skills": true
  }
}
EOF
fi

echo "Base tooling installed."
