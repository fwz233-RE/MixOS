# ESP32-S3 recovery flashing procedure

This is the verified procedure for the TypixDeck ESP32-S3 when the device can be reached through the CM5 but the normal running-application updater cannot be used. It is an operator-authorized recovery procedure, not an unattended update mechanism.

## Preconditions

- CM5 is `typixdeck` at `192.168.1.22`.
- ESP32 is connected through the internal USB Host route, physical USB location `5-1.2`.
- The new application has already been built and locally checked.
- The operator has confirmed that stopping `mixosd` is acceptable.
- The device can be placed in ROM download mode with the ESP32 side `BOOT` and `RESET/S3_EN` buttons.

Never switch to External USB Gadget routing during this procedure. It disconnects the internal ESP32 and keyboard.

## 1. Build and validate the application

From Windows PowerShell:

```powershell
wsl.exe bash -lc 'cd /mnt/d/TheEndDEvice/MixOS/firmware/esp32s3 && /mnt/d/TheEndDEvice/MixOS/.tools/idf-tools/python_env/idf5.4_py3.10_env/bin/python /mnt/d/TheEndDEvice/MixOS/.tools/esp-idf-clean/tools/idf.py build'
```

The application file is `firmware/esp32s3/build/mixos_esp32s3.bin`. The partition table and bootloader are generated under the same `build` directory. Confirm the image hash before uploading it.

## 2. Enter ROM download mode

1. Hold the ESP32 `BOOT` button.
2. Briefly press `RESET/S3_EN`.
3. Release `BOOT`.
4. Keep the CM5 powered and keep the internal Host route selected.

Verify from Windows through the approved remote inspection tool:

```powershell
$env:MIXOS_SSH_PASSWORD='your-password'
python tools/flash_esp_remote.py --native --host 192.168.1.22 --serial TD0720
```

The expected ROM identity is `303a:1001`, normally `/dev/ttyACM0`, with ROM serial `70:04:1D:D8:54:14`. The running application identity is different (`303a:80c3`, serial `TD0720`); do not pass the running-app serial when the device is in ROM.

## 3. Make a complete backup before writing

Use a temporary CM5 directory and the pinned `esptool 5.4.0` package. The CM5 uses Debian's externally managed Python environment, so install the wheel with `--break-system-packages` only for this explicitly staged package. Invoke the installed command from a directory other than `/home/pi/.local/bin`; otherwise the launcher file `esptool.py` can shadow the Python package.

Stop `mixosd` immediately before the serial operation. Save a full `0x800000`-byte backup from address `0x0`. Do not proceed if this read fails or the resulting file is not exactly 8 MiB.

## 4. Write only the intended image locations

Write the generated files at these addresses:

```text
0x0000  bootloader.bin
0x8000  partition-table.bin
0x10000 mixos_esp32s3.bin
```

Use `--flash-mode dio --flash-size 8MB --flash-freq 80m`. Require `Hash of data verified.` and exit code zero. Never erase the chip, erase NVS, or use an unverified image.

## 5. Complete readback and restore the service

Enter ROM mode again if the write command's reset left the ESP32 running. Read the entire `0x800000` bytes back. Require exit code zero and compare:

- The application range beginning at `0x10000` with the generated application image.
- The bootloader and partition-table ranges with the files that were written.
- Any unchanged protected/NVS regions with the pre-write backup.

The complete-image hash is expected to differ after an application update. The application-segment hash is the important exact match.

Restore the service even when a later verification step fails:

```sh
sudo systemctl start mixosd
systemctl is-active mixosd
```

The final service state must be `active`.

## 6. Runtime validation

After the ESP32 leaves ROM and enumerates as `303a:80c3` / `TD0720`, inspect the startup log and look for:

```text
STC3117 sample: ... V=3.xxxV ... SOC=xx.xxx%
```

The first displayed SOC can remain at 100% until the fuel-gauge model receives valid discharge samples. Validate it by disconnecting both external charging power and the battery as appropriate for the hardware test, then observe the value over time. Do not infer calibration quality from one immediate percentage reading.

## 2026-09-12 validation record

This procedure was used successfully on the real device. A complete 8 MiB backup was saved with SHA-256 `81ce4145da5fc672d97fae914f438d46b7672600bf5045e66fb5905198c56f15`. The corrected application was written once; esptool reported `Hash of data verified` and the write returned zero. A complete 8 MiB readback returned zero, and its 859824-byte application segment matched SHA-256 `f2ffff9bb37f1c2caae1d52d9a860204eb3e43bcef8d7e07f0089949fdc7e24d`. `mixosd` was restored to `active`.

The earlier application-oriented launcher rejected ROM `303a:1001` because it expected a running application identity and its maintenance epoch. That launcher remains useful for its supported running-app workflow, but this ROM recovery procedure is the correct documented path when hardware download mode is already active.
