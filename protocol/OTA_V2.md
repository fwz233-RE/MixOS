# ESP32 OTA transaction protocol v2

Implementation contract. USB framing, HELLO `<HH>` and 136-byte v1 identity remain unchanged. All integers below are little-endian. Types on MAINTENANCE: CAPS_QUERY=77, CAPS=78, OTA_REQUEST=79, OTA_RESPONSE=81; LOG=80 remains reserved. Every request uses a nonzero current-link session. Wire sequence replay is rejected; retries use fresh sequence/request IDs and the same transaction identity.

## Capabilities (36 bytes)

CAPS_QUERY has empty payload. CAPS: byte 0 format=2; byte 1 feature bits (1 durable journal, 2 exact file hash, 4 explicit reboot, 8 protected baseline, 16 maintenance health acknowledgement); u16 chunk at 2 (508), u16 window at 4 (8), u16 reserved at 6; u32 boot_id at 8; bytes running slot/boot slot/image state/reserved at 12/13/14/15; u32 ota0 address/size/ota1 address/size at 16/20/24/28; u32 protected slot mask at 32. Unknown slot/state=255. boot_id changes on MCU startup, not on link epoch change.

## Request (72 bytes)

byte 0 format=2; byte 1 opcode; u16 flags at 2 (must be zero); u32 request_id at 4 (nonzero); 16-byte transaction ID at 8; 32-byte exact app-file SHA256 at 24; u32 image_size at 56; u32 expected_boot_id at 60; byte target slot at 64; bytes 65..67 reserved zero; u32 at 68 (zero except HEALTH_ACK). HEALTH_ACK alone replaces bytes 24..55 with the expected running ELF SHA and places its nonzero challenge at 68.

Opcodes: BEGIN=1, END=2, QUERY=3, REBOOT=4, ABORT=5, VERIFY_RUNNING=6, RELEASE_BASELINE=7, HEALTH_ACK=8.

BEGIN/END/REBOOT/ABORT bind the complete transaction ID, SHA, size and target. BEGIN target must be the inactive slot and image_size fit its slot. BEGIN is idempotent for an identical transaction; ID reuse with a different binding is a conflict. Data uses existing OTA_DATA(offset u32 + bytes) and cumulative OTA_ACK(u32), strictly scoped to the session bound by accepted BEGIN. Control responses are OTA_RESPONSE, not legacy READY/DONE. New transaction END does not automatically reboot.

phase remains IDLE in QUERY when no journal exists. A VERIFY_RUNNING response then reports observed CONFIRMED for VALID or RUNNING_PENDING_VERIFY for trial, with received=size only when hash matches; this is measurement evidence, not an invented journal. QUERY may use an all-zero transaction ID to inspect the latest journal; a nonzero ID only matches that transaction. Query is observational and does not read/validate an entire flash image. VERIFY_RUNNING binds the requested exact file size/SHA to the actual running slot; it asynchronously hashes those bytes and reports the result. It does not invent past transaction evidence. RELEASE_BASELINE is explicit: target=0, SHA/size equal the protected 2026-09-16 baseline, running slot=1 VALID and selected for boot; it durably releases only the baseline guard, without writing app/otadata. The host must require an explicit --allow-replace-baseline option before this operation.

### 首份 HEALTH_ACK 固件的限定兼容

实机验证发现首份应用 `7beaccdac481b4644526030e0cd9e561b119f940516ee09de739ca55c80c9414`（ELF `155c46f4e3d4e952657c1a8277be516920e8e3871f003de79bb1b7774c73aa49`，910448 字节）的 C 基线字节数组抄写错误。该版本只在 RELEASE_BASELINE 的 SHA 字段期待 `7875d9a513acb95463b72e785eb160c70d03f85e965c3a30a93d954bb4cff55f`；这不是真实 A 镜像哈希。修复版固件恢复规范的真实摘要，新增跨语言一致性测试防止同一错误同时进入实现和测试请求。

主机仅在显式 `--allow-replace-baseline`、规范的真实基线元数据、已知首份 B 的精确文件／ELF／长度、本次运行 `VALID`、实际测量及 HEALTH_ACK 全部通过时，预先选择上述旧版本线协议值，并分别记录真实基线和 wire guard。该兼容不修改镜像／发布清单身份，不在 REFUSED 或超时后自动尝试不同摘要，不适用于其他接收端，也不跳过 B 的健康测量。后续修复版使用标准摘要。解除保护仍只写专用 NVS 标志，应用写入须后续独立 BEGIN。

REBOOT requires matching transaction, boot selection and source boot_id. A duplicate after the new boot is observational and must not reboot a pending candidate again. ABORT cannot undo boot selection. New BEGIN is rejected while boot partition differs from running partition or while running state is not VALID.

## Response (192 bytes)

byte 0 format=2; byte 1 opcode echoed; byte 2 phase; byte 3 result; u32 request_id at 4; transaction ID at 8 (16 bytes); expected file SHA at 24 (32 bytes); u32 total/received/boot_id at 56/60/64; bytes target slot/running slot/boot slot/image state at 68/69/70/71; signed i32 error at 72; u32 flags at 76; stored/readback file SHA at 80 (32 bytes); running ELF SHA at 112 (32 bytes); NUL-padded error text at 144 (48 bytes).

Phases: IDLE=0, RECEIVING=1, HASH_CHECK=2, IMAGE_VALIDATE=3, SELECT_INTENT=4, BOOT_SELECTED=5, REBOOT_REQUESTED=6, RUNNING_PENDING_VERIFY=7, CONFIRMED=8, FAILED=9, ABORTED=10, VERIFYING_RUNNING=11.

Results: OK=0, BUSY=1, CONFLICT=2, REFUSED=3, NOT_FOUND=4, ERROR=5. A queued operation initially responds BUSY; QUERY establishes completion. flags: bit0 journal available, bit1 exact flash hash verified, bit2 baseline protected, bit3 maintenance health challenge offered, bit4 maintenance health acknowledgement accepted. Unknown image state=255. Requests rejected at validation must not destroy or abort an existing transaction. A response request_id/opcode is matched in addition to epoch/session. A current-state response includes the journal transaction identity (not an untrusted request echo).

Exact hash means SHA256 of image_size bytes starting at the app partition address, including the file's appended hash if present, excluding partition padding. It is different from ELF SHA and esp_partition_get_sha256 semantics.

## Trial maintenance acknowledgement

A trial stays PENDING until both local/control-heartbeat health and a maintenance-worker round trip have been established, followed by the continuous 20-second health hold and successful VALID write/readback. PING/PONG, cached identity, elapsed time, or enqueueing a measurement response alone cannot satisfy this gate. Host absence leaves a locally healthy trial pending; it is not a local fault.

1. The host checks capabilities (required mask `0x17`), the intended file SHA/length, ELF, running/selected slot and this boot ID. It starts VERIFY_RUNNING while still PENDING; waiting for VALID before verification would create a dependency cycle.
2. A matching VERIFY_RUNNING reply offers flag `0x08` and message `health-challenge:` followed by exactly eight lowercase hexadecimal digits. The nonzero random challenge is scoped to that exact measurement, boot, link generation and maintenance session. It is sequencing evidence, not authentication of a potentially malicious host.
3. After checking the complete measurement, the host sends HEALTH_ACK with the same transaction, exact size, boot ID and target; bytes 24..55 carry the checked ELF SHA rather than the file SHA, and bytes 68..71 echo the challenge. Its request ID must be newer than the VERIFY request that issued the challenge, under signed 32-bit modular ordering.
4. An OK ACK reply uses the **original file-SHA binding**, not an echo of the ACK's ELF field, and sets flag `0x10`. The host checks the full response, then polls VERIFY_RUNNING until actual VALID and sustained fresh heartbeats. Repeated ACK/VERIFY must not restart the healthy interval. A dropped ACK reply is an unknown observation, not permission to restart the update or issue another reset.

The firmware invalidates the volatile proof on link loss, OTA-request maintenance session changes (including switching away and back), reboot, or incompatible measurement. CAPS/IDENTIFY are link-layer observations: they never grant health proof and do not by themselves change the OTA worker's request session. Unknown/old boot IDs, incorrect challenge/ELF/file binding and stale request IDs cannot confirm. Older receivers lacking capability `0x10` are rejected by the updated verifier; preserved historical packages are not retroactively declared to support it. Request/response wire lengths remain 72/192 bytes.

## Persistence and recovery

Journal is a versioned, CRC-checked NVS blob in an isolated namespace, written only at phase boundaries. SELECT_INTENT is durable before IDF changes otadata. Boot metadata and journal are reconciled after reset; neither a timeout nor a journal label alone proves rollback. Receive interruption aborts transfer; no cross-reboot resume in v2. END/selection survives link loss. No per-chunk NVS writes.

A successful host job requires verified actual running file bytes, full ELF identity, expected slot, VALID state, sustained fresh heartbeat and successful service restoration. Pending, mismatch and unknown remain distinct. The currently deployed old receiver is not made safe by uploading a new candidate: first installation has a separate ROM bootstrap gate.
