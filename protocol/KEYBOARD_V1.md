# MixOS keyboard I2C v1

This protocol replaces the original destructive single-byte FIFO. **No v0 fallback and no USB HID fallback.** Single ESP32-S3 master, STM32F042 slave, 7-bit address `0x1f`, 400 kHz. Multi-byte integers are unsigned little-endian; never cast wire bytes to a packed C structure.

## Atomic frame read

Master sends `START address+W 00 REPEATED_START address+R`, reads exactly **126 bytes**, then NACK/STOP. The STM32 latches the complete frame at the read-address interrupt, including up to 32 oldest FIFO entries. Reading never removes events. A short, aborted or repeated read therefore cannot lose an event. Beyond byte 125 returns `ff`. Only pointer `00` and the repeated-start form are supported; a STOP between pointer and read invalidates the pointer.

| Offset | Bytes | Meaning |
|---|---:|---|
| 0 | 1 | ID `6b` |
| 1 | 1 | Protocol version `01` |
| 2 | 1 | Bit 0: overflow counter nonzero; bits 7:1 zero |
| 3 | 1 | Total pending events, 0..128 (not just entries in this frame) |
| 4 | 2 | Next physical event sequence, modulo 65536 |
| 6 | 4 | Dropped-event counter, modulo 2^32, reset on MCU boot |
| 10 | 12 | Six row masks, 16 bits each; columns 0..10 in bits 0..10 |
| 22 | 1 | Requested backlight level, 0..8; task applies asynchronously |
| 23 | 1 | Reserved, zero |
| 24 | 4 | Master session cookie; zero after MCU reset |
| 28 | 96 | Up to 32 entries, 3 bytes each; unused entries zero |
| 124 | 2 | CRC-16/CCITT-FALSE of bytes 0..123, little-endian |

CRC: polynomial `0x1021`, initial value `0xffff`, no reflection, xor-out zero; check vector ASCII `123456789` gives `0x29b1`.

Event: sequence low byte, sequence high byte, physical edge byte. Edge bit 7 is down, bits 6:4 row 0..5, bits 3:0 column 0..10. Row 0 real columns are **1,2,3,7,8,9** (mask `0x038e`); all columns of rows 1..5 are represented. Blanks/tick events are never enqueued. Matrix masks describe the accepted debounced, ghost-filtered event state, **not raw electrical samples**. Snapshot, sequence, overflow count and FIFO prefix share a single serialization boundary. An IRQ may fall between two accepted edges of one scan; it cannot split an individual edge/state/sequence update.

FIFO holds 128 full events (384 bytes). A full FIFO drops the newest event, increments the dropped-event counter, **still updates matrix and next sequence**, and preserves older entries. Duplicate identical edges are ignored. Sequence increments for every accepted transition, including dropped transitions. PA13 open-drain interrupt is low while FIFO is nonempty; polling is authoritative.

## Commands

Each write is a separate `START address+W [command bytes] STOP`. Commands commit only at STOP, require the exact length, and do not allocate memory. Unknown, overlong and malformed commands do nothing. Read transactions never commit commands. There is no remote bootloader/reset command.

| Bytes | Meaning |
|---|---|
| `11 seq_lo seq_hi` | Acknowledge the prefix ending at this sequence, provided it occurs within the **first 32** current events. Remove that prefix only. Missing sequence does nothing, making immediate retry idempotent. |
| `13 cookie0 cookie1 cookie2 cookie3` | Set session cookie. Does not clear FIFO or matrix. Master uses a fresh nonzero cookie on each new session. |
| `20 level` | Set backlight to 0..8. Invalid levels ignored. No EEPROM write; hardware application runs in the keyboard task, not ISR. |

Commands have no separate CRC. A corrupted ACK can at worst cause an observable sequence/snapshot discontinuity; the ESP suppresses and resynchronizes input. Frame CRC detects corrupted reads before any callback. This is reliability/error detection, not authentication or a security boundary against another bus master. Sequence and cookie wrap are modulo arithmetic; delayed ACK retries across 65536 new edges are unsupported.

## ESP behavior and recovery

`mix_keyboard_tick(now_ms)` is called from the main task every 20 ms and also rate-limits itself. Maximum one 126-byte combined read, one ACK write and one backlight write per tick; each IDF call timeout is 5 ms. Thus at most three I2C operations / 15 ms of configured I/O timeout per tick, plus CPU/RTOS scheduling overhead. No loops wait for a FIFO to empty. Offline failures retry after 1000 ms. First contact or MCU cookie loss requires a cookie write and a later matching frame. `online` means a compatible responding v1/session, not that held keys are armed.

The ESP validates CRC, version, bounds, session, sequence, transition consistency and (when the whole remaining FIFO fits) reconstructed matrix against the atomic snapshot. ACK succeeds **before** delivering the batch, avoiding duplicate terminal text after uncertain ACK. This favors safety over guaranteed text delivery: events can be discarded during recovery or host reset. A failed ACK, bus error, CRC error, boot/session change, overflow or explicit input reset clears repeat/modifier interpretation and enters release-wait. The driver discards at most 32 entries each tick until it ACKs a complete frame with an all-zero matrix. It then sets expected sequence to that frame's next sequence and initializes fresh input state. Edges arriving after that snapshot survive the prefix ACK and are processed next tick. No held key or modifier is replayed on reconnect. Repeats are suppressed while backlog could contain a release.

`mix_keyboard_overflows()` returns observed dropped-event increments across MCU sessions since driver init (modulo 2^32). A new master session can observe historic drops again; this is a diagnostic count, not a unique global accounting of losses. `mix_keyboard_backlight_step()` only queues work, does not issue nested I2C operations, and is ignored offline.

## Validation limits

Host tests exercise the real portable transport, input, ESP driver with an IDF stub, and STM32 board/IRQ C with register stubs. They cannot verify electrical I2C stretching, register clear semantics, NVIC timing, USB reports, real ghosting/domes or F042 RAM/linker layout. The exact QMK pin and source-order verifier are in `firmware/keyboard/QMK_PIN.json` and `firmware/keyboard/tools/verify_qmk.py`. Production requires both target builds and bench testing; v1 is not claimed hardware-validated.
