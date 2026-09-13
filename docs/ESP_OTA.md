# Updating the ESP32-S3 over USB

This is how firmware reaches the display chip once the device runs the A/B
partition layout. It replaces ROM download mode for every ordinary update.

```sh
export MIXOS_SSH_PASSWORD=...
python3 tools/deploy_ota.py --image firmware/esp32s3/build/mixos_esp32s3.bin
```

One command, about a minute, no buttons, no cables moved, no backup ceremony,
no 4 MiB font rewrite. The command finishes when the new build is running.

## Why this is safe to run casually

The serial procedure is careful because it can brick the device: it erases and
rewrites regions the chip needs in order to boot at all. A USB update cannot,
because of four properties that hold at the same time:

- **The running slot is never written.** The image goes into the *other*
  application slot, `ota_0` or `ota_1`, whichever is not in use.
- **Nothing is selected until everything arrived.** The device hashes the
  bytes as it writes them and compares the result with the SHA-256 the host
  announced up front, then ESP-IDF independently validates the image header
  and its own embedded hash. Only then is the new slot marked bootable.
- **An interrupted transfer is a no-op.** Unplugging the cable, killing the
  host, or losing power halfway leaves the old slot selected and running. The
  device times the transfer out and forgets it.
- **A bad build undoes itself.** The new slot boots on trial. It is confirmed
  only after about 20 seconds of healthy running with the USB host present, or
  90 seconds of running without one. A build that crashes, hangs the watchdog,
  or boot-loops resets while still on trial, and the bootloader then returns to
  the previous slot on its own, without any host involvement.

So the worst realistic outcome is that the device keeps running exactly what it
was running before. That is why `tools/deploy_ota.py` runs to completion in the
foreground rather than submitting an audited detached job the way the serial
flasher does.

## What actually happens

1. The image is checked locally: 0xE9 magic, ESP32-S3 chip id, it fits a
   slot, and `build/esp32s3/font-app-build.json` says this exact binary is the
   current cross-build of the current sources. `--skip-build-check` drops the
   last condition; the rest are not optional.
2. `tools/ota_esp.py`, `linux/protocol.py`, `linux/mixosd.py` and the image are
   uploaded to a fresh `/home/pi/mixos-ota-<timestamp>/` and hash-verified
   there before anything runs.
3. `mixosd.service` is stopped, because it holds the CDC node. The updater
   itself runs as `pi`, never as root.
4. The image is streamed over the existing USB CDC protocol as `OTA_BEGIN`,
   windowed `OTA_DATA`, `OTA_END`, with the device acknowledging offsets and
   the host resynchronising from the device's own position when a chunk is
   lost. Progress is printed as it goes.
5. The device reboots into the new slot, the host waits for the CDC node to
   come back and completes a fresh handshake.
6. `mixosd.service` is started again, whether or not the update succeeded.
7. A receipt lands in `build/deploy/mixos-ota-<timestamp>.json` with the exact
   uploaded hashes and the result.

## When it does not apply

`tools/deploy_ota.py` needs two things that the historic firmware does not
have: `ota_0`/`ota_1`/`otadata` partitions, and an application that understands
the OTA messages. A device that has neither reports it in one of two ways:

- **"no OTA slot: device still has the factory-only partition table"** — the
  application is new enough to answer but the flash layout is the old one.
- **"never replied to OTA_BEGIN"** — the application predates USB updates
  entirely, so it ignores the request.

Both mean the same thing: the device needs the one-time migration below.

## The one-time migration

This is the last serial flash the device should ever need. It installs the
bootloader, the A/B partition table from `firmware/esp32s3/partitions.csv`, and
an OTA-capable application.

```sh
export MIXOS_SSH_PASSWORD=...
python3 tools/deploy_display.py --stage --migrate
python3 tools/deploy_display.py --start build/deploy/mixos-display-<timestamp>.json
python3 tools/deploy_display.py --status build/deploy/mixos-display-<timestamp>.json
```

The table is built so the migration rewrites as little as possible:

| Region | Offset | Migration |
| --- | --- | --- |
| bootloader | `0x000000` | rewritten |
| partition table | `0x008000` | rewritten |
| `nvs` | `0x009000` | **untouched** — brightness and language survive |
| `phy_init` | `0x00f000` | **untouched** |
| `ota_0` | `0x010000` | new application |
| `otadata` | `0x200000` | blanked to 0xFF, inside the old application's tail |
| `font` | `0x210000` | **untouched** — the 4 MiB MiSans image is not rewritten |
| `ota_1` | `0x610000` | left erased; the first OTA fills it |

`otadata` deliberately sits at `0x200000` rather than the conventional
`0xd000`, because `0xd000` is inside the old `nvs` partition and using it would
destroy the stored preferences. With `otadata` blank and no `factory`
partition, the ROM bootloader boots `ota_0` and records the sequence itself.

The whole 8 MiB is read back afterwards and compared: the four regions above
must match what was written, and every other byte must be identical to the
pre-flash backup. The result is recorded as `migrated_to_ab` in the audit log
with the resulting layout, boot slot and the partitions carried across.

`--migrate` refuses to run unless both `partitions.csv` and the binary in
`build/` are the A/B table, so a stale build directory cannot silently install
a layout with no OTA slots.

## Checking where a device stands

`tools/update_esp.py` identifies a live partition table as either `legacy` or
`ab` and refuses anything else, so a preflight against the attached device
answers the question directly. The local build's own answer is in
`build/esp32s3/font-app-build.json` under `partition_layout` and `ota_capable`.
