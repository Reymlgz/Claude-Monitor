#!/usr/bin/env bash
# Installs claudemonitor into its own virtualenv and links it into ~/.local/bin.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv "$HOME/.claudemonitor-venv"
"$HOME/.claudemonitor-venv/bin/python" -m pip install --quiet .
mkdir -p "$HOME/.local/bin"
ln -sf "$HOME/.claudemonitor-venv/bin/claudemonitor" "$HOME/.local/bin/claudemonitor"
echo "Installed. Run: claudemonitor"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Add ~/.local/bin to your PATH: echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc" ;; esac
