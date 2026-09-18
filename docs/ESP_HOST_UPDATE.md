# ESP host-authorized update build

## 最新实机状态：2026-09-18 十次连续交替已通过

`normal-alternation-readverify-20260918` 已连续完成十次正常免按键更新，完整镜像身份、A/B 槽位、新启动、VALID、维护确认、持续心跳和服务恢复逐轮通过。1779 次实际测量中两次无回复各由一次严格有界只读重测恢复，原始字节与诊断已独立关联；旧中断任务及恢复不计入本次十次。最新全量回归：Linux 1061 通过／4 跳过，Windows 1050 通过／15 跳过，部署主机代码与测试输入一致。

已发布并实际检查 Linux 命令 `/usr/local/bin/mixos-esp-update`；正式入口的最终实时查询仍为 A `ota_0 / VALID`、ELF `532717dd…`、启动 ID `647101243`，服务恢复 active，没有额外刷写或复位。当前面板版本 `532717dd` 及画面正常已由用户确认。正常更新及有限通信恢复已通过；用户选择保留当前良好设备，准备备用板或独立复位通道后，再单独进行尚未完成的故障镜像、主动链路故障、受控断电验收。使用方法见 [ESP_OTA.md](ESP_OTA.md)，完整证据和保留的审计失败说明见 [DEPLOYMENT.md](DEPLOYMENT.md)。以下早期构建与验收状态仅保留历史含义。

## 历史实机状态：五轮连续成功，第六轮经原事务恢复

2026-09-17 晚间已确认 Linux 原生任务连续完成五次免按键交替更新；第六次新 B 已启动但 USB 通信未恢复，监管任务停止。用户确认屏幕仍正常变化，并另行授权一次仅针对 ESP 子设备的 USB 总线复位；随后原事务经 `apply --resume` 完成精确文件／ELF、VALID、维护确认、持续心跳及服务恢复，没有重新上传镜像。旧监管失败记录保持不变，十轮连续验收尚未通过。正在加入固件重启前显式 USB 断开，详见 [DEPLOYMENT.md](DEPLOYMENT.md) 最新状态与独立证据；旧 A 已获准替换，外部完整备份仍保留。以下首次 B 及更早构建记录均为历史证据。

## 历史里程碑：首次 B 确认通过

2026-09-17 17:33（UTC+8），`observe-health-ack-20260917` 在不复位、不重新刷写的情况下确认实际运行 `ota_1 / VALID`，应用文件 `7beaccda…`、ELF `155c46f4…` 均精确匹配新 HEALTH_ACK 固件。实际维护确认成功后持续收到 9 次心跳，跨度 16.172 秒；服务已恢复，任务和清理退出 0，用户确认实体 LCD 正常。证据已下载并独立核验：`build/deploy/ota-acceptance-20260916-1749/observe-existing-local-verification.json`。用户已授权满足这些条件后解除旧 A 保护并继续 10 次正常交替验收，故障／断电注入仍不在授权内。下文历史的“尚未写入／待验收”不再代表首次 B 的当前状态；完整计划仍未验收完成。

> 本页保留 2026-09-11 主机授权进入 ROM 的历史构建与部署证据，不代表当前候选或设备状态。2026-09-16 早先曾验证恢复镜像 `cfacb3fe… / ota_0 / VALID` 并确认实体显示正常；2026-09-17 的后续无写入资格测试现已通过应用→同一芯片 ROM→双 8 MiB 读回→单次官方复位→精确 A／VALID／持续心跳／服务恢复；下载证据已独立核对与恢复快照零差异。这只证明该资格闭环通过，B 候选尚未写入。最新状态见 [DEPLOYMENT.md](DEPLOYMENT.md)，新版应用 A/B 事务与首次安装条件见 [ESP_OTA_V2.md](ESP_OTA_V2.md)。

## 2026-09-17 维护往返确认修复（实机待验收）

当前源码新增 `HEALTH_ACK`（操作码 8、能力位 `0x10`）：主机在 PENDING 时先测量实际运行文件，核对完整文件 SHA、ELF、长度、槽位和本次启动身份，再回送维护任务发出的随机挑战；固件收到匹配确认后，仍需连续 20 秒本地功能和链路健康，并且写入 VALID、复查 VALID 均成功，才结束试运行。主机再持续观察心跳并核实实际状态。ACK 回复丢失记录未知，不自动重发 ACK、刷写或复位。

链路断开、OTA 请求的维护 session 改变、错误或畸形 VERIFY／ACK 会撤销该临时确认。CAPS／IDENTIFY 只是链路层查询，既不授予确认，也不单独切换工作任务的 OTA session。旧 `faafb49…` 启动保护候选不含此修复，不能作为本轮最终安装候选。

首次 B 失败后如果观察到精确受保护 A／VALID，主机会在一次身份查询前后分别验证心跳，保存独立失败恢复记录并恢复服务，进程退出码仍为 2；这不证明 B 启动成功或自动回滚原因。当前板一次保留 A 的 B 安装及独立启动已获风险授权，但新候选必须先完成隔离构建、来源及测试绑定检查，再签发精确包的一次性审批。原始 A ELF 和独立 EN／BOOT 不是正常首次实验的统一前置条件；任意死机恢复和高风险故障测试仍需独立硬件或合适备用板。

## Firmware behavior

This change supersedes the mandatory on-device confirmation described in older update documentation. An explicit administrator update request on the established maintenance protocol now receives `UPDATE_READY` automatically. The host remains responsible for authorizing its operator (`--execute` and the deployment launcher's existing administrative policy). The USB maintenance protocol does not cryptographically authenticate the peer.

Only an online link's `PREPARE_UPDATE` on maintenance channel 4, with a nonzero request/session ID, the current epoch, a fresh sequence and an empty payload can create a grant. The terminal is closed first. A successful READY enqueue grants exactly that request for 15 seconds. A failed enqueue cannot grant permission. Duplicate PREPARE requests cannot extend the grant or replace it with another request.

`PREPARE_UPDATE` does not boot. Only an empty `ENTER_BOOT` with the matching channel, epoch and granted request/session, a fresh sequence, and an unexpired grant requests ROM entry. The grant is consumed before the boot request is published, and the public boot-request accessor consumes that request once. Disconnect, link restart, heartbeat timeout and grant expiry revoke the grant. No new PREPARE is accepted while a boot request is still awaiting consumption.

当前 `mix_link` 自动授权路径不再提供 `mix_link_update_answer()`；UI 不参与主机更新批准。PREPARE／ENTER_BOOT 与应用 A/B 事务是两套不同操作，正在接收或选择启动槽的事务会阻止 ROM 准备请求。历史 UI 模态框文字不构成当前协议行为；其它本地确认仍独立处理。

## Scope

Only MixOS firmware, its native tests, local build/report helper, and build evidence were changed. No deployment launcher, sibling TypixDeck repository, SSH connection or hardware operation was used by this work. Host backup, identity and full-readback requirements remain the deployment owner's responsibility and were not relaxed here.

## Reproduction and evidence

From `D:\TheEndDEvice\MixOS` with Windows Python 3.12:

```text
py -3.12 -m unittest discover -s tests -p test_link_update.py -v
py -3.12 -m unittest discover -s tests -p test_preview_ui.py -v
py -3.12 -m unittest discover -s tests -p test_protocol.py -v
py -3.12 -m unittest discover -s tests -p test_linux.py -v
py -3.12 tests/esp_font_build.py build
```

The six new maintenance tests compile actual `mix_link.c`, `mix_protocol.c` and `mix_terminal.c` under WSL Ubuntu-22.04 as `fwz233`, with GCC warnings-as-errors, AddressSanitizer and UndefinedBehaviorSanitizer. SDK queue/USB/task/JSON calls are stubbed; no hardware is opened. Cases cover automatic readiness without UI input, terminal closure, malformed/offline/wrong-epoch/wrong-session requests, strict expiry including timer wraparound, sequence replay, one-time boot entry, disconnect/restart/heartbeat revocation, and queue failure. The existing UI harness also verifies that the automatic-ready notice with firmware's nonpending view does not open a modal or emit a UI action.

The application build uses the existing pinned ESP-IDF 5.4.2 / Xtensa GCC 14.2.0 WSL setup documented in `ESP_FONT_BUILD.md`. Both prior images are verified and preserved before compilation:

- `build/esp32s3/previous-mixos_esp32s3.bin`: 469104 bytes, SHA-256 `9593904eab8c1bcaaef9c383abc9a5308ae4beeece94989436360289eafc675b`.
- `build/esp32s3/previous-font-candidate-mixos_esp32s3.bin`: 857136 bytes, SHA-256 `c0e99db4d5cb854e5ea2bf24753fcfb17ef77f8bce7b3b1c6a20b824418e2a69`.

Current app sizes/hashes, exact source hashes including `mix_link.c`, independent esptool image checksum/embedded-hash validation, linked font symbols and font provenance are in `build/esp32s3/font-app-build.json`. Build output is in `build/esp32s3/font-app-build.log`.

Build completed with exit 0 on 2026-09-11. Both `firmware/esp32s3/build/mixos_esp32s3.bin` and `build/esp32s3/mixos_esp32s3.bin` are **857008 bytes** (`0xd13b0`), SHA-256 **`bf60ca6891029d011d1d5c8cddb59ae4e711a1404b017ab042a65abeac7819e5`**. The 0x200000-byte app partition has 0x12ec50 bytes free. Esptool 4.12.0 reports checksum `03` valid and embedded validation hash `17c586a5dfcfc91c8807bc73defdf23e78cfbc2259e000f92a0d29f8ca6e5768` valid. The build explicitly compiled `mix_link.c` and generated the app. It emitted existing misleading-indentation warnings, an unset component version-environment warning, and subsecond WSL-mounted-filesystem clock-skew warnings; it had no compile error.

Tests: 6 maintenance + 4 UI + 10 Python protocol + 20 Linux-host unit tests passed on Windows (3 Linux-only PTY cases skipped). The 2 cross-language protocol tests were skipped on Windows, then both passed under the existing `/home/fwz233/mixos-host-venv/bin/python` in WSL. Total: **42 passed**, with 3 Linux-only PTY tests not run in this scoped change. The native maintenance tests had no sanitizer findings.

## Font provenance caveat

No font or UI-source change is required by this firmware behavior change. The existing generated font is 1724276 bytes with SHA-256 `859ec555afa5dfbd234fc80e7058aba243d85e3f32551f266d0457ea326d8bfc`.

The pre-existing font manifest records the historical CRLF UI-source hash (`afa1e20d3c2481b32da16558b4352951774602d3e5de96deaf871652573c2697`), while the current workspace UI source uses LF. The report records the actual source hash and separately checks exact-byte versus CRLF-normalized equivalence. A normalized match is evidence of unchanged text, **not** a claim that exact-byte release validation passes. Release staging must handle the pre-existing line-ending provenance mismatch explicitly; this change does not silently rewrite the UI source or manifest.

## Hardware status

The firmware-scoped build/test work above did not operate hardware. The subsequent integrated deployment **did** write this exact app and the font once, verify a fresh complete 8 MiB readback, and start the new application using a separately recorded boot-only watchdog reset after the job's RTS reset failed to exit download mode. The running firmware passed three heartbeats over 15.016 seconds and automatically returned UPDATE_READY about 0.200 seconds after host PREPARE without local touch/key input. The post-flash readiness test sent no ENTER_BOOT and did not reflash. The Linux service was restored. Full integrated regression passed 198 Python tests with no skips and CTest 2/2. See `DEPLOYMENT.md` and `build/deploy/display-20260911-host-authorized.json` for precise evidence and the original job's preserved failure status. Physical LCD appearance and keyboard input remain unverified.
