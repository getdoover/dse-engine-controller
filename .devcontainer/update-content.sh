#!/usr/bin/env bash
# Runs after on-create, and re-runs when source code changes during prebuild updates.
# Syncs project dependencies so they're cached in the prebuild snapshot.
set -e

export PATH="$HOME/.local/bin:$PATH"

uv sync --all-extras --dev

echo "Project dependencies synced."
