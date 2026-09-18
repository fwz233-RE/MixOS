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

The existing ESP-IDF 5.4.2 checkout used by the build is `.tools/esp-idf-clean`; the isolated tool directory is `.tools/idf-tools`. `tools/idf_env.py` is the single description of that layout: the pinned versions and every derived path live there, and the shell script and the Python drivers all read them from it.

Build from the project root on Windows:

```powershell
py -3.12 tests/esp_font_build.py build
```

The driver enters the environment through `idf_env.build_command()` and runs the vendored `idf.py build` under WSL, then writes the deployment manifest `build/esp32s3/font-app-build.json`. Do not let a bare `idf.py build` in `firmware/esp32s3` be the last build before a deployment: the binary it produces is correct, but the manifest still describes the previous one, and `tools/deploy_ota.py` will refuse the image. `BUILD.md` has the exact refusals and why `--skip-build-check` is the wrong answer to them.

The checkout's `export.sh` is not used. Full activation insists on a RISC-V debugger that is not installed and has nothing to do with this ESP32-S3 target, so `idf_env.py` exports the required paths explicitly instead of downloading or modifying the tool installation. Inspect the resulting `build/project_description.json` and component resolution rather than assuming a globally installed IDF version.

## The AI deck

Three tools serve the four-button interfaces, and they are separate because
they fail for separate reasons.

```powershell
$env:MIXOS_SSH_PASSWORD='...'
py -3.12 tools/inventory_pi.py --host 192.168.1.22    # read-only; no sudo, no writes
py -3.12 tools/stage_models.py                        # download here, where the network works
py -3.12 tools/deploy_models.py --host 192.168.1.22   # send, resumably, and verify on the device
py -3.12 tools/stage_speech.py                        # the speech-recognition models, same reason
py -3.12 tools/deploy_speech.py --host 10.12.194.1 --block-mb 64
py -3.12 tools/usb_gadget.py   --status --host 192.168.1.22   # which USB arrangement the device is in
py -3.12 tools/deploy_apps.py  --host 192.168.1.22    # interfaces, launchers, units, polkit rule
```

`inventory_pi.py` writes its answers into `docs/AI_DECK.md` between the
`inventory` markers and keeps the raw output under `build/inventory/`.
`stage_models.py` and `deploy_models.py` both resume: the measured rates are
2.8 MB/s from the mirror to this machine and 0.20 MB/s from here to the device
over Wi-Fi, so neither transfer is something to start over. `usb_gadget.py`
switches the CM5 between driving the internal hub and being a USB network
adapter, which turns that second rate into tens of MB/s at the cost of the
screen and keyboard while it is active; Wi-Fi stays up in both arrangements, so
the device cannot be stranded. `deploy_apps.py --check-only` reports what is on
the device without changing anything, and `--print-script` shows the one
privileged step without connecting at all.

`stage_speech.py` and `deploy_speech.py` are the same pattern for the speech
models, and they exist because `mixos-aiserver.service` runs with
`IPAddressDeny=any`: `moonshine_voice` downloads a model on first use, inside
the request handler that is trying to transcribe or speak, and on that unit the
download cannot succeed at all. Both directions are covered - recognition and
synthesis - and the models have to be on disk before the first press of the
microphone button. Two details about `download.moonshine.ai`, both measured
2026-09-13: it answers `HEAD` with 403, so sizing files that way reports every
model as forbidden, and it rejects the default `urllib` user agent with 403
while serving the identical request to a browser agent. The staging tool sends
a browser agent and sizes files with a one-byte ranged `GET`.

## Windows upload/font tools
Python: `C:/Users/123/AppData/Local/Programs/Python/Python312/python.exe`. OpenSSH/SCP use the previously verified host key for the CM5 at `192.168.1.22`; neither uploader auto-accepts an unknown host key. Credentials are supplied through `MIXOS_SSH_PASSWORD` and a temporary askpass helper, not saved in release manifests.

The CM5 has `dfu-util 0.11` and pyserial 3.5. The WSL dfu-util package is 0.9 and is **not** the approved production keyboard flashing tool; real USB devices are reached through the CM5 in internal Host mode.
