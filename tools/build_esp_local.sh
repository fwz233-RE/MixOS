#!/usr/bin/env bash
set -euo pipefail

ROOT="/mnt/d/TheEndDEvice/MixOS"
IDF="$ROOT/.tools/esp-idf-clean"
VENV="$ROOT/.tools/idf-tools/python_env/idf5.4_py3.10_env"
PROJECT="$ROOT/firmware/esp32s3"

export IDF_PATH="$IDF"
export IDF_TOOLS_PATH="$ROOT/.tools/idf-tools"
export IDF_PYTHON_ENV_PATH="$VENV"
export ESP_ROM_ELF_DIR="$ROOT/.tools/idf-tools/tools/esp-rom-elfs/20241011"
export IDF_SKIP_CHECK_SUBMODULES=1
export PATH="$ROOT/.tools/idf-tools/tools/xtensa-esp-elf/esp-14.2.0_20241119/xtensa-esp-elf/bin:$ROOT/.tools/idf-tools/tools/cmake/3.30.2/bin:$ROOT/.tools/idf-tools/tools/ninja/1.12.1:$PATH"

if [[ "${1:-}" == "--clean" ]]; then
    rm -rf "$PROJECT/build"
    shift
fi
if [[ $# -ne 0 ]]; then
    echo "Usage: $0 [--clean]" >&2
    exit 2
fi

cd "$PROJECT"
"$VENV/bin/python" "$IDF/tools/idf.py" build

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
