#!/usr/bin/env bash

# Only show welcome on first attach (not every terminal)
WELCOME_FLAG="$HOME/.doover-welcome-shown"
if [ -f "$WELCOME_FLAG" ]; then
    exit 0
fi
touch "$WELCOME_FLAG"

echo ""
echo "========================================="
echo "  Welcome to the Doover App Template!"
echo "========================================="
echo ""
echo "Your environment is ready with:"
echo "  - Python 3.13, uv, and all project dependencies"
echo "  - Doover CLI (doover)"
echo "  - Claude Code with doover-skills"
echo ""
echo "Common commands:"
echo "  uv run pytest tests -v      Run tests"
echo "  uv run export-config        Export doover_config.json"
echo "  doover app run              Run app locally with simulator"
echo "  claude                       Open Claude Code"
echo ""

# Prompt doover login if not already authenticated
if [ ! -f "$HOME/.doover/config" ]; then
    echo "-----------------------------------------"
    echo "Let's log in to your Doover account:"
    echo ""
    doover login-dv2
else
    echo "Doover CLI: already authenticated."
fi

echo ""
echo "Happy building! Use /doover in Claude Code to explore available skills."
echo ""
