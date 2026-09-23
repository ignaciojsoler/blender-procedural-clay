#!/usr/bin/env bash
# Builds the installable extension zip (Blender 4.2+) into dist/.
# Usage: scripts/build_extension.sh [path/to/blender]
set -euo pipefail
BLENDER="${1:-blender}"
cd "$(dirname "$0")/.."
mkdir -p dist
"$BLENDER" --command extension validate procedural_clay
"$BLENDER" --command extension build --source-dir procedural_clay --output-dir dist
