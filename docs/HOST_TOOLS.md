# Local build tools

All source changes and generated release artifacts belong to `D:/TheEndDEvice/MixOS`. The original firmware repositories are not build destinations.

## Linux host regression environment

WSL distribution: `Ubuntu-22.04`; user: `fwz233`. Native `cc`, Clang, CMake, Python 3.10, and make are available. Font builder tests additionally require the pinned packages in `tools/requirements-host.txt`; use an isolated environment rather than modifying system Python:

```sh
python3 -m venv /home/fwz233/mixos-host-venv
/home/fwz233/mixos-host-venv/bin/pip install -r /mnt/d/TheEndDEvice/MixOS/tools/requirements-host.txt
cd /mnt/d/TheEndDEvice/MixOS
cmake -S tests -B build/host
cmake --build build/host -j 4
ctest --test-dir build/host --output-on-failure
/home/fwz233/mixos-host-venv/bin/python -m unittest discover -s tests -v
```

Real-device fast ESP32 recovery path (validated 2026-09-12): when the ESP32 is already in ROM download mode and enumerates on the CM5 as `303a:1001` at `/dev/ttyACM0`, the application-oriented deployment launcher may reject it because it expects the running-app identity. The validated fallback is to stop `mixosd`, use the pinned esptool 5.4 package on the CM5, read a complete 8 MiB backup, write bootloader/partition/app at `0x0/0x8000/0x10000`, read the complete 8 MiB again, compare the application segment and restore `mixosd`. This path requires explicit operator approval and must never skip the backup or readback. On 2026-09-12 it wrote the battery-fix app successfully (`Hash of data verified`, `WRITE:0`, `READ:0`); the readback application segment was 859824 bytes and matched SHA-256 `f2ffff9bb37f1c2caae1d52d9a860204eb3e43bcef8d7e07f0089949fdc7e24d`. The complete-image hash is expected to differ from the pre-write backup because the app changed. The fallback should be promoted into a reviewed deployment tool rather than treated as an ad-hoc command.

## Keyboard cross-build

The pinned QMK checkout is `.tools/qmk-0.28.0`, revision `a63fd7f01cdabd9ce85bb09ae2b573fd3b8e60aa`. Required submodules include ChibiOS, ChibiOS-Contrib, printf, and LUFA. The isolated QMK Python environment is `/home/fwz233/mixos-qmk-venv`.

Installed ARM tools: GCC 10.3 (Ubuntu package `15:10.3-2021.07-4`), binutils 2.38, and newlib. Build using `tools/build_keyboard.py`; generated ELF/map/bin files and memory evidence are in `build/keyboard`. The manifest, not tool availability, determines target-build verification.

```sh
cd /mnt/d/TheEndDEvice/MixOS
/home/fwz233/mixos-qmk-venv/bin/python tools/build_keyboard.py
```

## ESP32-S3 cross-build

The existing ESP-IDF 5.4.2 checkout used by the build is `.tools/esp-idf-clean`; the isolated tool directory is `.tools/idf-tools`. Use the checkout's `export.sh` with `IDF_TOOLS_PATH` set to that absolute directory, then run `idf.py build` from `firmware/esp32s3`. Inspect the resulting `build/project_description.json` and component resolution rather than assuming a globally installed IDF version.

## Windows upload/font tools

Python: `C:/Users/123/AppData/Local/Programs/Python/Python312/python.exe`. OpenSSH/SCP use the previously verified host key for the CM5 at `192.168.1.22`; neither uploader auto-accepts an unknown host key. Credentials are supplied through `MIXOS_SSH_PASSWORD` and a temporary askpass helper, not saved in release manifests.

The CM5 has `dfu-util 0.11` and pyserial 3.5. The WSL dfu-util package is 0.9 and is **not** the approved production keyboard flashing tool; real USB devices are reached through the CM5 in internal Host mode.
