#!/usr/bin/env bash
# Build the ESP32-S3 application with the vendored ESP-IDF toolchain.
#
# The repository root is derived from this script's own location, so the file
# works from any checkout. It used to begin with ROOT="/mnt/d/TheEndDEvice/MixOS",
# which meant it only ran on one machine, under WSL, at one path.
#
# The toolchain layout itself lives in tools/idf_env.py so that this script,
# the Python build driver and the documentation cannot drift apart.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="$ROOT/firmware/esp32s3"

PYTHON="${MIXOS_PYTHON:-python3}"
eval "$("$PYTHON" "$ROOT/tools/idf_env.py" --shell)"

if [[ "${1:-}" == "--clean" ]]; then
    rm -rf "$PROJECT/build"
    shift
fi
if [[ $# -ne 0 ]]; then
    echo "Usage: $0 [--clean]" >&2
    exit 2
fi

cd "$PROJECT"
"$IDF_PYTHON_ENV_PATH/bin/python" "$IDF_PATH/tools/idf.py" build

for artifact in \
    build/mixos_esp32s3.bin \
    build/mixos_esp32s3.elf \
    build/bootloader/bootloader.bin \
    build/partition_table/partition-table.bin; do
    [[ -s "$artifact" ]] || { echo "Missing build artifact: $artifact" >&2; exit 1; }
done

echo "ESP32-S3 build verified"
stat -c '%n %s bytes' \
    build/mixos_esp32s3.bin \
    build/mixos_esp32s3.elf \
    build/bootloader/bootloader.bin \
    build/partition_table/partition-table.bin
