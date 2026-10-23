#!/usr/bin/env bash
set -euo pipefail

dependencies=(breezy)
if ! command -v svn >/dev/null; then
	dependencies+=(subversion)
fi
brew install "${dependencies[@]}"
