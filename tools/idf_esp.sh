#!/usr/bin/env bash
# Run an arbitrary idf.py subcommand inside the vendored toolchain.
#
# tools/build_esp_local.sh only ever runs "build". Dependency work needs
# "reconfigure", "fullclean" and "size-components" as well, and driving those
# through `wsl bash -lc "..."` from PowerShell mangles the quoting every time.
# This script takes the arguments verbatim instead.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="$ROOT/firmware/esp32s3"

PYTHON="${MIXOS_PYTHON:-python3}"
eval "$("$PYTHON" "$ROOT/tools/idf_env.py" --shell)"

cd "$PROJECT"
exec "$IDF_PYTHON_ENV_PATH/bin/python" "$IDF_PATH/tools/idf.py" "$@"
