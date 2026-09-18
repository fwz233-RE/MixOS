# Integrated device deployment

## 最新结果：十次连续正常更新通过，Linux 日常命令已发布

2026-09-18，`normal-alternation-readverify-20260918` 从已核实的 A 出发，连续完成十次 A/B 交替（最后回到 A），全部为独立的新事务和新启动。每轮完整文件 SHA、ELF、精确长度、运行／启动槽、实际 VALID、维护 HEALTH_ACK、持续心跳及服务恢复均通过；内核日志逐轮证实正常 USB 断开／重新枚举。整个序列没有请求物理按键、额外 USB 总线复位、ROM 下载或断电。之前中止的序列及对账结果均不计入这十次。

全部 53 个归档成员已下载并逐一校验，归档 `readverify-ten-rounds-evidence.tar.gz` 的 SHA-256 为 `950280a39da046860aa71c8eb3bd6a06c94d1e6725169897400826c939d67ca4`；身份／槽／启动链核验为 `readverify-local-verification.json`。原始通信共有 1779 次 VERIFY 请求，第 1、3 轮各有一次未捕获到回复，分别由一次新 ID 的只读重测恢复；没有捕获到迟到回复、解码错误或序列缺口。每次恢复都与超时诊断逐项对应，最终成功仍来自新的精确 VALID 回复，不能把此结果写成“零丢回复”或底层原因已经完全定位。

原始审计还纠正了一个验证器假设：原计划及既有 `Updater.run()` 允许提交前后的同事务幂等 END／REBOOT 对账，并不要求线上的请求各只能出现一次。实际记录是 10 次 BEGIN、20 次 END、73 次 REBOOT 请求，而非 73 次实际复位；全部控制请求逐条绑定原文件、事务、目标槽和源启动 ID，重复请求具有前序响应／QUERY 阶段证据，进入新启动 VERIFY 后控制请求数为零。接收阶段另有 17 次同事务接收偏移对账、69088 字节内容相同的块重发，重构文件 SHA 全部匹配，没有新 BEGIN 或整轮盲目重刷。这些既有传输／提交行为与本次新增只读重测明确分开。

首版审计因误用“END／REBOOT 各一次”而失败的报告原样保留；冻结版的全部其它检查通过，再由 `verify_readverify_transaction_idempotency.py` 独立核对真实幂等条件、传输内容和四项篡改反例。最终补充报告 `readverify-transaction-idempotency-verification.json` SHA 为 `bb5c87dbabc8502ce9fc0785e2d5a86110424bb3114da89117ee76641ee7e999`，没有改写设备记录或失败原件。

只有上述核验通过后，才发布 `/usr/local/bin/mixos-esp-update`，指向已验收、root 所有的自包含运行器；入口 SHA `a07e56222dc09c53ae7290aa172e673d87c4cd3359862ba2ca1984a341811818`。已实跑包检查和最终任务查询。随后经该正式入口单次 `inspect --live` 再次确认 A `ota_0 / VALID`、ELF `532717dd…`、文件 `1a5121ef…`、启动 ID `647101243` 未变，原事务 `9f0ff7a610b64afda18f6de14a09637b` 已确认，`mixosd` 恢复 active；该检查没有刷写或复位。

证据均位于 `build/deploy/ota-acceptance-20260916-1749/`，另包括 `readverify-release-input-verification.json`、`readverify-daily-publication-01.json`、`readverify-final-live-01.json`。两个平台的 1065 项全量回归与部署的主机代码逐字节绑定。远程日常入口依赖 `/opt/mixos-acceptance/normal-alternation-readverify-20260918/`，该受保护目录须继续保留。

完成的是正常免按键更新、有限只读通信恢复和 Linux 本机入口交付。用户此前看到的 `f19de3d0` 是旧 B 的 ELF 前缀；在最终实时检查之后，用户已明确确认屏幕显示 `532717dd` 且画面正常，实体 LCD 验收现已补齐。确认记录为 `readverify-physical-confirmation-20260918.json`，较早的“实体观察待确认”机器回执保留当时状态，不回写。

计划七项中实现／离线／入口六项已完成，实机验收项仍保留进行中：故障镜像、主动丢 DONE／CDC 干预、受控断电与独立 EN／BOOT 通道尚未完成，不能宣称任意死机和断电恢复已通过。用户已选择保留当前良好状态，先准备备用板或独立复位通道，再单独进行高风险验收；这不授权当前板故障注入、断电或新增接线。后续准备需核实实物引脚占用、默认释放的开漏 EN／GPIO0 控制和保留物理按钮，先验证无写入强制复位／ROM 往返，再逐项批准故障镜像及断电阶段测试。原计划文件未修改。

## 2026-09-18：链路修复与有界只读重测

固件补齐了三项链路生命周期保护：短 DTR 断连锁存直至主线程撤销旧会话、跨队列切代保留新 HELLO、半帧发送恢复时先可靠发送零分隔符。重启前 300 毫秒 USB soft detach 保持启用；没有修改显示双缓冲、时序、字体或分区布局。真实生产 C 测试夹具已修正，分别回退上述三项修复的变异测试均失败，当前实现通过。

`normal-alternation-linkfix-20260918` 完成两槽安装和四次连续更新后，第七轮再次遇到 `VERIFY_RUNNING` 无回复。原始字节证据显示：请求 `1735748155` 完整交给串口 write，随后 60 秒内收到 29 次同一会话心跳，但没有匹配维护回复，也没有解码错误或设备发送序列缺口。这证明主机未收到回复，尚不能确定请求／回复在设备内部的具体丢失位置。失败归档 `linkfix-stopped-20260918.tar.gz`（SHA-256 `9f981a94b6201551954ba84693ea7e91c2dfbee5657820c3cacc701345892d1f`）和旧失败结果均保留。原任务随后只对账成功，仍为 A／VALID、启动 ID `3660939907`；没有新增 BEGIN、END、REBOOT，该恢复不计连续验收。恢复包装器曾读到旧终态而报错，随后只读观察确认原对账成功，没有再次提交。

主机 `verify_actual()` 现在只对满足严格条件的测量超时允许最多两次额外只读请求：已完整发送、等待阶段超时、epoch／session 不变、有新且新鲜心跳、无新增解码错误或畸形回复。每次使用新请求 ID，单次上限为 5 秒且受原绝对总时限约束；仍必须收到新的精确文件／ELF／长度／槽／启动 ID／挑战确认／VALID 回复才能成功。CAPS、HEALTH_ACK、BEGIN、END、REBOOT 不因此重发；条件不满足则停止。`diagnostics.verification` 保留超时、重测请求、停止原因和恢复结果，不能用旧 VALID、QUERY 或心跳代替测量。

同一源码的最新全量测试：Linux 运行 1065 项，1061 通过、4 跳过；Windows 运行 1065 项，1050 通过、15 跳过，均无失败且 210 个源文件前后稳定。两份 911296 字节固件归档于 `build/candidate-archives/linkfix-{a,b}-20260918`：A 文件 SHA `1a5121ef034dc8b3efec5b269aebbd2c39370ea1bec72ad90ba0b688857a48b5`、ELF `532717dddc12521607ac14a752e47335d53514ccce6edf19daa4ca23182458ce`；B 文件 SHA `b266c2b6ec8a6d2b36107540ccfe6e333c270ac0959732d37433c06be02fc3f3`、ELF `c5a64ca0433eb36d22cea2180cce079645eb64dd92ba0bb1414c9086497a1456`。新发布包 `build/releases/esp32-ota-readverify-{a,b}-20260918` 使用这些原始固件及新主机代码，没有重新编造构建来源。

新任务 `normal-alternation-readverify-20260918` 已单次启动，先实时核对上述 A 和同一启动 ID，再从 A→B 开始独立计数十次连续交替。审批 SHA `9d9970c532cf8c420e1087752679c4635cbab4b22989c0b300457e45493c862b`，包 SHA `fb6c67f1e1f9d2b1c2f1082de4e22e313136e9f5be11475c72d1c319530625ea`；任何失败即停止，不自动复位或恢复旧失败序列。此处记录启动事实，最终通过须以随后导出的十轮证据及原始通信独立核验为准。

当前任务看门狗监视 MAIN、CDC I/O 和 OTA 工作任务，但未直接订阅 TinyUSB 内部任务；软件重启进入 SDK 低层前也没有新增独立 RTC 截止。SDK `esp_restart_noos` 在关闭缓存前已有 RTC flashboot 保护，不能表述为 SDK 完全无看门狗。这些边界尚未证实是单次测量丢回复的根因。故障镜像、断电注入和独立 EN／BOOT 接线仍未授权／未验收，正常十次更新通过也不能替代它们。

## 历史阶段：USB 重新枚举通过，新序列第 4 轮测量超时，验收尚未完成

`normal-alternation-usbdetach-20260917` 已成功安装两个修复槽，并完成第 1 次连续交替；第 4 轮（第 2 次连续交替）在读到正确 B、启动 ID `1934265556`、`VALID`、完整文件与 ELF 摘要和 HEALTH_ACK 标志后，下一次 `VERIFY_RUNNING` 等待超时。监管任务保持 `stopped / completed_updates=3 / accepted_updates=1`，没有重启或改写为成功。失败现场归档 `usbdetach-stopped-round4-evidence.tar.gz` 的 SHA-256 为 `d947b92848024ed28ad257b0cf63e9e624502d7a1900dad36e00cb443df6c281`，25 个成员已由 `verify_stopped_usbdetach.py` 独立校验。

本次内核确实记录了正常 USB 断开／重新枚举，设备号由 57 变成 58，之后没有该轮异常断开记录。未使用任何额外 USB 复位、EN、BOOT 或重新刷写，只读检查已重新查询到同一个启动 ID 的 B／VALID。随后进行 90.068 秒仅测量、不发送 HEALTH_ACK 的诊断：530 次测量请求及回复全部对应，44 次心跳跨度 86.913 秒，最大心跳间隔 2.045 秒，原始帧没有 CRC／解码错误或序列缺口。原始记录 SHA-256 为 `0bc60f4195e6be64a032f5e07c7fd00a2e1537a93d45efb57e372b384c3b4bbb`，本地校验见 `usbdetach-measurement-trace-local-verification.json`。这证明后续维护通信可用，不解释也不抹去原来的单次测量超时。

保留失败原件后，对原任务 `fadc83108dc54388aeef995e7323a626` 单次提交对账，确认原事务 `30b4f90acf204c96a301c0f49e9fefbc` 的 B 完整身份、VALID、维护确认及 4 次心跳／6.110 秒，服务恢复 active；对账没有新 BEGIN、END 或 REBOOT 事件。该补救不计入连续交替成功。正在补齐超时原因、请求标识、会话变化及心跳诊断，并继续查明通信间歇故障。日常更新入口尚未发布；十次连续验收和故障恢复验收均不能标记完成。

新修复版本的全量回归记录已核实：Windows 1005 项运行、990 通过、15 跳过；Linux 1005 项运行、1001 通过、4 跳过；两者无失败且 205 个源文件前后哈希一致。这些是随后主机诊断修改之前的测试证据，不替代实机验收。以下为保留的阶段记录。

## 历史阶段：USB 重启修复已构建，2＋10 实机验收已启动

2026-09-17 晚间，`normal-alternation-guardfix-20260917` 连续完成五次正常免按键 A/B 更新；每轮完整文件 SHA、ELF、目标槽、新启动 ID、VALID、维护 HEALTH_ACK、持续心跳及服务恢复均通过。第六轮在 REBOOT 请求后无法恢复 HELLO，监管任务按设计停止，保留 `unknown`，没有继续写入。此前还修正了受保护 A 摘要的 C 字节数组抄写错误；仅对精确已测量的初始 B 接收端使用旧错误摘要的线协议兼容值，真实镜像身份及显式解除保护门槛不变。修复前拒绝任务及五轮后中断任务均保留。

用户报告第六轮失联时实体屏幕仍正常变化。现场 USB 号保持 54，无新的断开记录，自动休眠未发生；对该 USB 设备的 GET_STATUS／GET_DEVICE_DESCRIPTOR 直接读取均返回 EPIPE。用户另行授权后，执行了**一次仅针对 ESP 子设备的 USB 总线复位**，没有新固件传输、ROM 进入、断电或 EN 操作。随后查询发现新 B 已运行：ELF `3409daa5…`，启动 ID `2938611580`，软件复位原因，原事务 `58a1d62903984c8d8ee51a10cb0f7062`，PENDING_VERIFY。这证明恢复后能查询到新应用，不证明总线复位等价于 MCU 复位，也不确定 USB 故障的所有底层原因。

对原任务 `d724b9ddbfc94a408ec9fddbffccb6f3` 执行受控 `apply --resume` 后，精确文件 `d92bfff60063c53b49ba7bb60152605e44a6260b41b1689f32d244a9321b8c3b`、910448 字节、`ota_1 / VALID`、实际维护确认、4 次心跳跨度 6.038 秒和服务恢复均通过。恢复阶段没有新增 BEGIN、END 或 REBOOT 事件，没有重新接收镜像。旧监管任务仍保持第六轮中断，不能把补救成功改计为十轮连续成功。原 A 已按授权被替换；外部完整 8 MiB 备份 `df9c108f…` 再次校验无变化。

下载证据已独立校验：`guardfix-stopped-local-verification.json`（五轮／失败现场归档 SHA `da00518b88f85f047a03f8556fdc190f056755677c91d56699bc9bd0898105a1`）与 `guardfix-round6-reconciled-local-verification.json`（恢复归档 SHA `e6dd0500e91c80c2586eadf246e19019b853e31a5ef3c2d57d211e84a0ef3dbc`），均在 `build/deploy/ota-acceptance-20260916-1749/`。故障镜像、断电注入与任意死机恢复仍未授权／未完成；独立 EN／BOOT 控制仍未建立。

USB 修复源码已完成：两条普通 OTA 重启路径统一到一次性 `mix_restart()`，先调用组件包装的公开 TinyUSB 断开 API，保持至少 300 毫秒，再软件重启；主线程领取后不再经过 UI／键盘／音频工作。保持现有看门狗订阅，不改变试运行回滚与 ROM 路径；REBOOT 日志失败时重新发布真实持久化状态。新全量 Windows 回归运行 **1005 项，990 通过、15 平台／环境跳过，无失败**，205 个源文件在测试前后不变。新构建／发布校验还精确绑定 17 个本地 USB 组件构建输入，防止只检查 main 包装层而遗漏底层修改。

两份 910752 字节修复发布包均完成独立构建和归档：A 为文件 `b2c003d08dc64ab8d7ef72ce619021596b665ff35c2bf528ffbe5490def72608`／ELF `477e857915754782c1d30dd8955051a05d1de419f86601262adb93fed04361f7`；B 为文件 `e661093c08d0c74dcf62568fbeee26506c9fd7cc5473cec74f5186fecc75cf42`／ELF `f19de3d06293ad5c04aba41e8842bb3c78b504240aae836e185bbd36bce5c6f8`。发布目录为 `build/releases/esp32-ota-usbdetach-{a,b}-20260917`，原候选及恢复证据不变。

新的 `normal-alternation-usbdetach-20260917` 已单次提交：先正常安装两个修复槽，再单独计数十次连续交替，总计 12 次真实新启动事务。包 SHA `cdd53574e9d8799c07a982fdd611e30c9b7c670090910314c89aa544cd95155a`，审批 SHA `24d5890e932761cf6f50d11179cc7b03e283396b95465ff19936408686bc900b`。Linux 本地 systemd 执行不依赖持续 SSH；任何失败停止，不自动 USB 复位、不重新传输旧事务。此记录表示验收已启动，最终成功仍需下载全部结果、核对真实 USB 断开／重新枚举及实体屏幕。

## 历史里程碑：首次 B 实机确认通过（2026-09-17 17:33 UTC+8）

安装任务 `install-health-ack-20260917` 已完成 B 写入、单个非活动 4 KiB otadata NEW 提交和完整 8 MiB 读回，原 A 与共享区域未变。随后应用自行重新枚举，具体起因未证明；因此没有重复安装或额外启动复位，而是执行独立 `observe-health-ack-20260917`。

该观察已确认运行 B `ota_1 / VALID`：910448 字节，文件 SHA-256 `7beaccdac481b4644526030e0cd9e561b119f940516ee09de739ca55c80c9414`，ELF `155c46f4e3d4e952657c1a8277be516920e8e3871f003de79bb1b7774c73aa49`，启动 ID `1754943808`。实际 HEALTH_ACK 往返通过后，9 次心跳跨度 16.171977 秒；`mixosd` 恢复 active，观察和 ExecStopPost 均成功退出 0，USB 号保持 49。用户明确报告实体屏幕正常。观察收据 SHA-256 为 `8f6ac5c30c4cc1aa7229c75fbe2c9ea77e55e893f17e282d4d3039bed4bbd4f3`，下载归档 SHA-256 为 `51ecffe90f01b2e4392714b7382eb4b1c5b61239ea75257fd91bb4c96aa397f4`；本地复核记录为 `build/deploy/ota-acceptance-20260916-1749/observe-existing-local-verification.json`。

用户已另行明确授权在 B 健康及实体显示正常后解除旧 A 保护，继续至少 10 次正常交替更新，外部完整旧备份继续保留。授权记录为同目录 `normal-alternation-user-authorization-20260917.json`。交替验收仍待完成；故障镜像、断电注入及任意死机恢复尚未验收，不能把本次 B 成功当作整个计划完成。以下 running／未安装等段落是历史过程记录，以本节和后续新证据为准。

The delivery consists of the ESP32 display/application, its raw MiSans font partition, the STM32 I2C keyboard firmware, and the existing unprivileged Linux terminal service. Build success, upload/staging, verified flash contents, and actual input/display behavior are separate states; this document does not treat one as proof of another.

## Current device and preserved state

- **Layout as of 2026-09-13: A/B.** The no-write qualification `qualify-reviewed-binary-20260917` completed successfully at 2026-09-17 01:01 UTC+8: the exact A application initiated PREPARE/ENTER_BOOT, the same-chip USB-OTG ROM was verified, two complete 8 MiB reads matched the recovery baseline, and one official watchdog reset returned ELF `cfacb3fe…` in `ota_0 / VALID` with sustained heartbeats. `mixosd` was restored. The downloaded evidence was independently verified locally with zero differing bytes. A is preserved as `7875d9a513acb95463b72e785ebd160c70d03f85e965c3a30a93d954bb4cff5f` (894560 bytes). The subsequent separately authorized `install-health-ack-20260917` task has now written and read back the B application and is checking the full precommit image; final installation and boot acceptance are still pending. This earlier qualification passes the no-write roundtrip only, not B installation or A/B reliability acceptance. The operator's LCD confirmation remains the earlier recovery observation, not a new physical-panel test.
- CM5: `typixdeck`, `192.168.1.22`, Debian 13. Internal USB Host routing must remain selected. The ESP32 and keyboard are on physical ports `5-1.2` and `5-1.1` respectively. External USB gadget routing disconnects both internal devices.
- `mixosd.service` is installed under `/opt/mixos/linux`, runs as `pi:dialout`, and serves the existing 80×28 ordinary-user Bash terminal over the ESP's CDC interface. The Linux desktop remains installed; `multi-user.target` is the reversible headless boot choice.
- The previously deployed ESP application is preserved in `build/esp32s3/previous-mixos_esp32s3.bin`, SHA-256 `9593904eab8c1bcaaef9c383abc9a5308ae4beeece94989436360289eafc675b`. A previous full 8 MiB backup remains on the CM5 at `/home/pi/mixos-flash-20260910-191910/original-flash-8MB.bin`. A new deployment makes a fresh backup rather than assuming that historical snapshot is current.
- The earlier ESP deployment passed the chip-side write hash and application heartbeat. Its later independent app readback did not complete; no independent-readback success is retroactively claimed.

## Controlled first-B installation — 2026-09-17 afternoon (running)

The HEALTH_ACK candidate completed an isolated build with stable inputs and exit 0. Application size is 910448 bytes, file SHA-256 `7beaccdac481b4644526030e0cd9e561b119f940516ee09de739ca55c80c9414`, whole ELF SHA-256 `155c46f4e3d4e952657c1a8277be516920e8e3871f003de79bb1b7774c73aa49`. Build/reports are under `build/candidates/health-ack-20260917` and `build/candidate-reports/health-ack-20260917`; the new portable release is `build/releases/esp32-ota-health-ack-20260917`. The 50 original protected artifacts remain unchanged. Actual BIN/ELF startup ranges and first CPU0 CORE hook were rechecked; this does not prove physical pre-takeover timing.

Final Linux confirmation/first-install/packaging tests passed 300/300 without skips, with stable source inputs and the original output retained in `confirmation-tests-20260917-v2`. Final frozen-source Windows regression ran 938 tests: 924 passed, 14 skipped, no failures. An earlier concurrent run failed a source-inspection test because this session edited the source after Python loaded it; that failure remains recorded, and the frozen-source rerun passed. Independent review also found that the general identity helper could resend a lost first query; the boot classifier now sends exactly once and fails on a missing reply, covered by a new regression.

The user explicitly selected a controlled current-board experiment, accepting unmeasured startup/manual BOOT/EN recovery risk, with no fault injection or A protection release. The new `mixos-reviewed-candidate-bootstrap/v1` path binds old finite binary facts, the corrected candidate, confirmation review, original test evidence and exact host code. It does not widen the historical qualification-only route. Candidate evidence SHA-256 is `6fd6c522d1be081c7c8527fcd5931ba042ba5ea030d6c661f58f1336c3c21d38`.

The exact package was verified and published on the Pi, then its real packaged CLI passed an offline candidate/plan check before one root-claimed systemd submission. Task: `install-health-ack-20260917`; package SHA-256 `4feefc34b89b771d4e4675f7a9c0520defab9db11e59e57e473be37f5b3e0f39`; approval SHA-256 `0b211ca4b08a6b24cfe7b61a1c3a6bfcf5a3da2a0ac656ba4de12de5e8617d12`. The first observation shows a fresh 8 MiB backup and complete B-slot readback saved, with precommit full reading in progress. `mixosd` is held stopped, USB is the same-chip ROM at enumeration 48. Final full comparison and the single inactive 4 KiB NEW metadata commit remain to be established by `verified.json`; the installer never resets automatically. A separate boot-only task will require that exact writer proof and a new full device read before one official watchdog reset. This is ongoing installation, not a successful B boot or physical LCD acceptance.

## Offline OTA-safety implementation — 2026-09-16

The new receiver, Linux-native transaction worker and protected-slot bootstrap are described in `ESP_OTA_V2.md`. The initial delivery was source changes, builds and offline tests. The operator subsequently authorized SSH/device acceptance at `192.168.1.22`; that acceptance is in progress and has not passed. The 9/16 recovery task and failed qualification tasks remain historical evidence and must not be rerun.

### Successful no-write qualification — 2026-09-17

Task `qualify-reviewed-binary-20260917` used a newly reviewed explicit binary-evidence approval, not a replay or relabeling of a failed job. The immutable package pins 20 Python code files, 14 evidence artifacts and four runtime wheels. Archive SHA-256 is `4ebd183d1052e4c43cf163f40b7278dbc931493259a414f48a098797fdd87cc0`; approval SHA-256 is `b641ee399d7e632468af1c27d5c4fced2e7c8601f7d11719ff382e8b6a179fc5`. Installer and installed-code/permissions/locks were verified before the single systemd submission. No Flash programming was authorized or performed.

- The actual chain was PREPARE/ENTER_BOOT from A → USB-OTG ROM `303a:0009`, MAC `70:04:1d:d8:54:14`, path `5-1.2` → two exact 8 MiB reads → one official watchdog reset → exact A/VALID/heartbeat → automatic service restoration.
- Both complete reads match each other and the preserved recovery **byte for byte**, SHA-256 `df9c108f6248f2cfde22f097187beaeef5d76dbfd34a173140671c164939a73e`. A, B, bootloader, table, otadata, font and NVS/phy are unchanged at those observations.
- Both before and after ROM, the old application received exactly one identity query between separate 20-second heartbeat observation windows. The final post-query window contained 10 pings spanning 18.121 seconds, with exact ELF `cfacb3fe…`, `ota_0`, address `0x10000`, VALID. These results do not identify the sole cause of earlier hangs.
- The systemd task ended successfully with exit 0 and zero process IDs; `mixosd` returned to its original active state. Final observed application USB number was 47. No manual key press was requested or used in this qualification.
- Qualification receipt SHA-256: `436d5631c140dd8ae3b477644da339d5252cc1d13cb153dda0a19b471a2bd5b5`. Remote task evidence is `/var/lib/mixos/bootstrap-ota/qualify-reviewed-binary-20260917/`.
- Local evidence under `build/deploy/ota-acceptance-20260916-1749/`: `reviewed-qualification-evidence.tar.gz` (SHA-256 `f88658c7458e5ef1db1f14d86e5fd9800cf7d7de6e64418942e0da81e58aa542`) and `reviewed-qualification-local-verification.json`; the export contains both full reads, claims, service result and journal. `verify_reviewed_qualification.py` independently checked the downloaded archive, exact bytes and production policy.

Windows regression: **797 passed, 12 skipped**. Linux bootstrap/packaging/protocol/host/platform coverage: **359 passed**. Counts overlap. The default historical-build route still rejects missing historical provenance. The explicit binary-evidence route remains qualification-only; successful qualification does not authorize B installation or prove the candidate fits the old bootloader's pre-takeover budget.

### Candidate pre-takeover review — 2026-09-17

The offline review of the exact candidate `faafb49aa8f4ae6d0e215e7b4543f6cf72e41e19e4821a7bf7bc497b492f87e3` (ELF `34580ab8658d32736905b8cf460e913bcd0f14561567a861da79842b151a4821`) found no static image-format, partition-size or mapped-segment conflict. The candidate fits B, and its early RTC watchdog hook is the first CPU0 CORE initialization entry. This is a static result, not a B-install approval or a measured startup-time bound.

- The hook runs **after** Flash setup, external PSRAM initialization and code/read-only-data copying, CPU1 startup waiting, PSRAM testing and clock initialization. Being first in the CORE array does not protect these earlier stages with the hook's new settings.
- The image header declares 80 MHz; the effective candidate configuration enables **120 MHz Flash and PSRAM**, PSRAM instruction/read-only-data use and memory testing. The header alone does not describe the entire startup path.
- Before the hook, `esp_clk_init` feeds/reconfigures the RTC watchdog to a **nominal 1.6 seconds** during slow-clock calibration, then feeds/configures the application setting of nominal 30 seconds. Calibration repeats while its result is zero, without a fixed source-level iteration bound. This does not extend the old bootloader's nominal 9-second image-loading/pre-clock interval. All three values are nominal settings, not measured worst-case budgets.
- The official SDK-pinned ROM function-byte audit is complete (`build/candidate-reports/early-rtc-20260917/rom-wdt-static-audit.json`); live-device ROM bytes, physical watchdog timing and reset behavior remain unmeasured. Candidate BIN/ELF binding and initialization evidence are in `early-rtc-link-audit-v2.json` in the same report directory.
- The exact A BIN is preserved, but an original A ELF with whole-file SHA `cfacb3fe25931e918b8f46d1f840da8d57ba39ef799bd0af4629939b9185761a` has not been located in the reviewed artifacts. A read-only search of the device's deployment-package, installer and bootstrap-evidence roots found no ELF files; a broader home-directory search was bounded and stopped at its file limit without a match, so it is not treated as exhaustive. The ordinary build ELF is a different candidate and must not stand in for A. The new candidate is 14432 bytes larger than A and changes mapping; it is not established to be merely A plus a hook.

The latest read-only device inventory confirms `mixosd.service` remains active/running, the qualification service is inactive/dead with MainPID and ControlPID 0, and USB remains `303a:80c3 / TD0720 / dev47`. It opened no ESP serial device, sent no identity query and performed no Flash programming. A read-only GPIO inventory found CM5 GPIO controllers but did not establish electrical control of ESP EN or BOOT/GPIO0; no GPIO was requested or driven. This leaves arbitrary-hang recovery unproven; it is not by itself a ban on every normal first-B experiment.

Scope correction after rereading the original plan: a hash-bound original A ELF would improve startup comparison, but exact A BIN/full-flash evidence remains available and its own startup instructions can be analyzed directly. Neither recovering that ELF nor adding independent EN/BOOT was specified as a universal prerequisite for normal updating. Independent EN/BOOT or a suitable spare remains the preferred prerequisite for high-risk fault injection and is needed to guarantee recovery from arbitrary firmware hangs. A first-B attempt still needs candidate-specific evidence, explicit acceptance of unmeasured startup/recovery risks, protected A and a bounded stop/reconciliation policy; the qualification-only route is unchanged and does not grant write permission.

### Implementation gaps reopened — 2026-09-17 afternoon

The previous six-completed-stages count described the earlier implementation, not proven end-to-end acceptance. Reinspection found that trial confirmation used local progress and control PING/PONG but did not require a verified maintenance-worker round trip. The firmware safety and offline-test tasks have been reopened. The existing `faafb49…` candidate is preserved as a historical build, not relabeled as containing this new fix. A new isolated build and separate candidate review will be required.

The host now initiates exact running-file measurement while B is still PENDING, rather than waiting for VALID before exercising maintenance. This avoids a confirmation dependency cycle when the firmware gate is added. The boot-only path can also distinguish exact A restoration from B success: it verifies the preserved A file and VALID record against the installation evidence and pre-reset full readback, observes 20 seconds of heartbeats before one identity query and 20 seconds after, and writes a separate `boot-fallback.json`. Only this bound proof permits restoring the original Linux service; the command exits 2, does not create a B-success receipt, and performs no second reset, rewrite or automatic retry. Seeing A does not establish whether B executed or why the bootloader selected A. Uncertain identity, changed epoch/session or inadequate heartbeats still fail closed. These are offline implementation changes, not a new device acceptance result.

### Earlier authorized acceptance diagnostics — 2026-09-16 (superseded)

Evidence is preserved under `build/deploy/ota-acceptance-20260916-1749/`.

- The earlier a1–a7 tasks produced no successful `qualification.json`; the later successful task is documented above. The safety candidate has never been programmed into B. Qualification a4/a5 each read the entire Flash twice without programming; both reads matched the recovered snapshot. Later a6/a7 failed before ROM entry. Failed task IDs are not permission to retry.
- Fresh ROM protocol synchronization on USB `303a:1001`, physical `5-1.2`, confirmed the ESP32-S3 and MAC. This determination uses protocol/register replies, not enumeration alone. ROM-only full readback used no stub, erase, flash write or reset. The downloaded 8 MiB file was independently compared locally: **zero differing bytes**, SHA-256 `df9c108f6248f2cfde22f097187beaeef5d76dbfd34a173140671c164939a73e`. Both A and all shared regions remain the protected snapshot at that observation.
- Startup register `GPIO_STRAP=0x23` records GPIO0 low at reset; the later live input reads high with input enabled, and `FORCE_DOWNLOAD_BOOT=0`. This explains download-mode selection, but does not determine whether button identification, reset-time electrical behavior or hardware causes the low sample.
- One ROM watchdog recovery initially stopped after the unlock write because the diagnostic parser incorrectly required reserved status bytes to be zero. Read-only reconciliation proved unlock succeeded and the timer was still disabled. A separately recorded continuation completed only the remaining three official watchdog writes, without replaying unlock. The firmware returned as application USB `303a:80c3`, but identity verification timed out and subsequent tracing received no HELLO. This is **not** a successful A health result or successful qualification.
- The first physical-reset capture expired without user action. At the user's request a second 10-minute window was armed and readiness confirmed. The user's single reset was captured at 14:41:47 UTC: application USB 41 disconnected and JTAG/serial USB 42 appeared; no application enumeration followed. Read-only ROM synchronization again succeeded and showed the same low-at-reset/high-now GPIO0 state. The identity-query comparison could not run; no conclusion that identity requests caused the hang is established.
- The USB42/held-stopped observation was superseded by `boot-query-comparison-command.json`: a new ROM-only 8 MiB read matched `df9c108f…`, one complete official four-write watchdog reset returned A, and one identity query matched exact ELF/`ota_0`/VALID. The query followed 20 seconds of heartbeats (9 pings spanning 16.109 seconds) and was followed by another 20 seconds (10 pings spanning 18.115 seconds). The originally active `mixosd` service was restored; a later read-only SSH inventory (`resume-early-rtc-inventory.json`) confirmed active/running and no active qualification worker. No Flash was written. This does not prove a startup delay fixed the cause of the previous hangs.
- Historical bootloader `6ea16d37…` and current `78648f98…` are not behaviorally identical: the executable watchdog timeout changes from nominal 9 seconds to 30 seconds. The default historical-build route still rejects the missing migration/build binding. A separately explicit `mixos-reviewed-bootloader-binary-evidence/v1` route now verifies fixed original binaries, reference ELF/configuration and six bounded audit reports. It permits only current-A no-write qualification, never B installation or boot-only; it does not recover historical configuration or prove candidate timing. No historical receipt was fabricated.
- The earlier Windows **702 passed, 12 skipped** and Linux bootstrap/CDC/service **100 passed** are historical results before the latest changes, not the current complete regression count. The controlled boot diagnostic has 10 separate offline parser/command-boundary tests.

After these earlier diagnostics, the separately reviewed no-write roundtrip described above passed. The isolated early-RTC candidate's static initialization review is complete; the old bootloader's pre-takeover timing and physical recovery compatibility remain unproven, as detailed above. Separately approved B installation and A/B acceptance remain pending. Reset-time BOOT/EN behavior remains unexplained; use actual board/button identification and independent boot logs where available. Repeated reset/qualification attempts, service-active status or USB enumeration cannot replace these gates.

## Recovery after OTA completion timeout — 2026-09-16

The operator entered ROM with BOOT + S3_EN. Fresh inventory identified
`303a:1001`, MAC `70:04:1d:d8:54:14`, at physical path `5-1.2`.
A no-stub, no-reset ROM read found valid otadata records with sequences 9 and 6,
selecting `ota_0`; its ELF identity was still `c36d5f95`. The first failed OTA
image (`97cfc42f`) was in `ota_1`. A missing OTA_DONE alone had not established
which build was running; the later no-HELLO attempts wrote no new firmware.
The earlier claim that two framebuffers were inherently incompatible with the
UI was unsubstantiated. Recovery retains the prior `num_fbs=2` configuration.

The previous staging receipt `mixos-display-20260915-155326` was not started.
Its font manifest had been manually edited; instead, the real font builder was
rerun before this recovery. Coverage and FreeType load validation passed,
producing the same font bytes (`d56c56c6...`) and a freshly generated manifest.
Use the new receipt, not the superseded staging package.

Job `mixos-display-20260916-024325` was submitted once. It made a fresh 8 MiB
backup, wrote the application and existing font payload once each, and read
back the entire 8 MiB. Both chip-side hashes and independent local byte-for-byte
comparison passed. The bootloader, partition table, nvs, phy_init, otadata,
font contents and ota_1 are unchanged. `current-device-app.bin` was extracted
from that readback and `DEPLOYED_APP_SHA256` updated accordingly.

- Backup SHA-256: `fa7eacf1b20d193443d717620487eee2aa52f71e8943f49eb69590f6d4814886`.
- Readback SHA-256: `df9c108f6248f2cfde22f097187beaeef5d76dbfd34a173140671c164939a73e`.
- Local evidence prefix: `build/deploy/recovery-20260916-024325-` (backup,
  readback, readback verification, boot verification, audit and screenshot).
- Remote original job evidence: `/opt/mixos-display-packages/mixos-display-20260916-024325/work/session/`.

The job's RTS reset left the device in download mode, so its original exit
status remains 1. The job was not rerun. A separate boot-only operation checked
the stopped worker, exact readback hash, chip/security identity, cleared
force-download flag and a fresh device-side MD5 of all 8 MiB. It consumed one
persistent claim and used esptool's official watchdog reset without uploading
a stub or programming flash. USB then returned as `303a:80c3` / `TD0720`.
Verification observed four heartbeats over 15 seconds, exact expected ELF
identity `cfacb3fe...` in valid `ota_0`, and a complete 1024x768 screenshot.
The screenshot shows the home page, centred clock and Wi-Fi to the left of the
right-aligned battery reading, with no anomalous left-edge line in the captured
framebuffer. This does not itself prove physical scanout is healthy.
`mixosd.service` was restored and observed active.

The OTA hang is not claimed fixed by this recovery. Source investigation found
that image validation maps/unmaps flash with caches disabled while the RGB
bounce-buffer ISR can still read PSRAM (`CONFIG_LCD_RGB_ISR_IRAM_SAFE=y`).
Also, `CONFIG_ESP_SYSTEM_PANIC_PRINT_HALT=y` can halt instead of rebooting after
a panic. These are investigation leads, not a captured fault backtrace; neither
setting was changed in the recovery image. Do not use repeated OTA attempts or
framebuffer-count changes as a substitute for a controlled diagnosis.

## Interface icons — 2026-09-15 21:25 (UTC+8)

Job `mixos-display-20260915-131011` wrote the application and the font partition
once each and passed a complete 8 MiB readback: `app_updated: true`,
`all_other_flash_unchanged: true`, old app `371bdd75…`, new app `58ba1e24…`,
full image `bcc6058b…`. A panel screenshot then showed the five settings-rail
glyphs, the status-bar battery and radio, and Chinese text rendering together,
with `build c36d5f95` matching the local image's ELF hash.

The interface now draws icons as Private Use Area glyphs in the same partition
MiSans occupies. `tools/build_font.py` reads the `ICON_*` literals out of
`mix_ui.c` and merges exactly those glyphs from Material Symbols, pinning the
variable font to one static instance, dropping layout features so no Latin
name-ligature glyphs enter the image, and scaling 960 units per em to MiSans'
1000. Twenty-seven icons cost 4,728 bytes; the partition keeps 2.4 MiB spare.
Adding an icon therefore needs a font flash, exactly as a new Chinese character
does — an OTA does not rewrite this partition.

Three attempts failed before the successful one, each for its own reason, and
none of them wrote a byte:

- The staged package carried `linux/mixosd.py` without `linux/netctl.py`, so the
  job died at import. Both the launcher and its test now derive the daemon
  package by globbing, as `tools/_mixlib` already did.
- The application's own `ENTER_BOOT` did not leave the chip in download mode,
  and the one-use maintenance grant was then spent. Hardware `BOOT` + `S3_EN`
  entry is the documented answer, and `flash_font_on_pi.py` already accepts a
  device that is already in ROM.
- `DEPLOYED_APP_SHA256` was stale, because USB OTA had updated the device many
  times and does not maintain it. Two guesses from OTA receipts were both
  refused: a receipt records the image that was *sent*, and the device had since
  booted a different slot. Reading `otadata` and the active slot out of a fresh
  backup settled it.

One fix came out of that last point. The full backup used to be written to disk
only *after* validation, so a refused image also discarded the 8 MiB that had
just taken two minutes to read. It is now durable before anything can reject it.

Leaving hardware-entered download mode still needs a short press of `S3_EN`
alone; the job's own reset does not achieve it, and its truthful exit-code
failure after a verified readback means exactly that.

## Why the Chinese fix includes an application update

The previous application never called `ttf_font_init()` before initializing the UI. Consequently, a valid font partition alone could not enable Chinese. The fix initializes the font, retains an ASCII recovery interface if loading fails, and uses glyph metrics when fitting proportional MiSans glyphs into fixed terminal cells. The font builder records actual UI coverage and a host FreeType load check. See `FONT_BUILD.md` for repertoire/alias/license limitations.

## Build evidence

- ESP: `build/esp32s3/font-app-build.json`, preserved recovery binaries, and candidate application/build log. Use `py -3.12 tests/esp_font_build.py build`; successful stable inputs and artifacts are bound in `build/esp32s3/completed-build.json`. The `report` command only revalidates that existing receipt and cannot certify edited sources against an older binary. Portable v2 packaging is performed by `tools/esp_release.py`. See `BUILD.md` and `ESP_OTA_V2.md`.
- Font: `build/font/MiSans-Normal-gb2312.ttf` and its adjacent manifest. Staging verifies current `mix_ui.c` against the manifest using only exact raw/LF/CRLF representations; arbitrary source changes are rejected. Font and application binaries remain exact-hash checked.
- Keyboard: `build/keyboard/manifest.json`, both keymap binaries, ELF/map files and memory reports. `target_build_verified` requires the pinned source and automated checks. `hardware_verified` remains false until actual device evidence is obtained.

## Keyboard clock correction and boot result — 2026-09-11 13:11 (UTC+8)

A keyboard-local HSI48 oscillator-enable correction was clean-built for both
keymaps; actual ARM disassembly confirms the enable and readiness wait.
The raw image is 15,920 bytes, SHA-256
`8ec21047a264da7c968070812af30dc2b675a1ee67e7e8cb5cf3762492388706`.
The previous complete build is preserved at
`/home/pi/mixos-keyboard-build-before-clock-20260911`.
The full host suite passed **205 tests, zero skips; CTest 2/2**, including the
actual font and renderer. An initial incremental build retained stale board
objects; it was rejected before staging. Future target builds use `--clean`.

Receipt `build/deploy/mixos-keyboard-20260911-050842.json` was started exactly
once with the keyboard already in ROM device 8; no physical button was needed.
The worker backed up all 32 KiB (SHA-256
`83787b1b472d0452825fe4732c141a6a50227a99379b4e89ed571b24bc495920`),
wrote 16 KiB of application-covered pages once, and verified all 32 KiB
(SHA-256 `a6a89ddeb1c4ffd8e2184beaa9ce4913f526022d0a9e3500a06897f6b49238d7`),
including every untouched tail/EEPROM byte. Its single leave request returned
zero, but the kernel then recorded ROM `0483:df11`, serial `FFFFFFFEFFFF`,
**device 21** at `5-1.1`. Application startup, I2C communication and physical
input remain unverified. The clock fix did not resolve ROM return; no repeat
write or leave was issued. The ESP remains `303a:80c3`, `TD0720`, device 20;
`mixosd.service` remains active. Evidence:
`build/deploy/keyboard-20260911-clock-deployment.json`.

## Stage once, then authorize from the host

`tools/deploy_display.py --stage` uploads the verified app/font package and saves a receipt under `build/deploy`. `tools/deploy_keyboard.py --stage` does the same for the verified default keyboard build. Staging does not stop services, reset chips, change USB routing, or write flash.

The display launcher accepts `--start RECEIPT`; this explicit host command authorizes the update, with no mandatory on-device confirmation in the new firmware. First migration from the old application may require hardware ESP BOOT + S3_EN entry because the old application's confirmation policy is still running. It temporarily stops `mixosd`, starts a detached single-execution job, and restarts the service on completion or failure. The worker verifies the old app, exact live partition table, chip/security identity and a fresh 8 MiB backup before touching flash. With the explicit new-app option it writes only the 2 MiB app partition and 4 MiB font partition. Its full 8 MiB readback must exactly equal the expected image, including all unchanged bootloader/table/NVS/other bytes. The original app-only updater remains unchanged.

The keyboard requires the local Fn+diamond or Sym+diamond rescue chord when running an application; fresh inventory confirming it is already in ROM DFU requires no additional physical entry action. Its actual ROM serial is obtained with `tools/deploy_keyboard.py --inventory RECEIPT`, then passed to `--start RECEIPT --serial EXACT_SERIAL`. The root-owned isolated worker makes a complete 32 KiB backup, writes only application-covered 1 KiB pages, verifies all 32 KiB including unchanged tail pages, then requests DFU exit. It leaves `mixosd`, the ESP, USB routing and power alone. See `KEYBOARD_FLASH.md` for the exact safeguards.

Both launchers have `--status RECEIPT`. A submitted systemd job is not a successful flash. After a disconnect or failure, inspect that same job and its audit; never blindly repeat `--start`. Backups are retained, and neither tool performs automatic rollback, unprotect or chip erase.

## After every successful flash, update what the next flash will check

A serial flash leaves two records behind that describe *the device*, not the
build. If they are not refreshed, the next serial operation reads a full 8 MiB
backup and then refuses to write, because the application it finds is not the
one it was told to expect:

```text
STOP: Live application differs from the staged, verified MixOS app
```

That refusal is correct and must not be worked around by relaxing the check.
Avoid it by finishing the job:

1. `build/esp32s3/current-device-app.bin` — the exact application bytes now on
   the device. Take them from the verified full readback the job saved, at
   offset `0x10000`, cut to the image's own length from its header. Do not
   substitute the local build output; they are the same bytes only when the
   flash actually succeeded.
2. `DEPLOYED_APP_SHA256` in `tools/deploy_display.py` — the SHA-256 of that
   file, with a comment saying which job produced it.

On 2026-09-13 this step had been skipped after the 2026-09-12 recovery flash,
and the next migration attempt cost a full backup cycle to discover it.

An OTA through `tools/deploy_ota.py` does not need this, because it never
inspects the running application's bytes. It is a serial-path obligation only.

## A/B migration — 2026-09-13

Receipt `build/deploy/mixos-display-20260913-110839.json`. The device moved
from the factory-only layout to A/B and received the current build in the same
session, started once from the running application with no hardware button
press in the successful attempt.

- Backup: 8388608 bytes, SHA-256 `2ef0d3a96e11ad067d7698b41cff6ba0bfc81fcc07c1263a0d09dba747aed9ce`, saved in the job's `work/session/original-flash-8MB.bin` on the CM5.
- Entry: `display_host_prepare_queued` then `display_host_enter_boot_sent`; the application rebooted itself into USB-OTG download mode.
- Writes: five regions, each with its own `display_write_start` and chip-side `display_chip_hash_verified`.
- Readback: full 8388608 bytes, SHA-256 `aa7da4fe41b24233ff498a0455e8eb0ee297c9065691a3bfc56582496bb2f0d0`, byte-exact against the expected image. `all_other_flash_unchanged: true`.
- Result: `migrated_to_ab` = layout `ab`, boot slot `ota_0`, `otadata` blank, `nvs` and `phy_init` carried across. Old app `f2ffff9b...`, new app `17b67a3e...`.
- Boot: `303a:80c3` / `TD0720` at `5-1.2`, 7 heartbeats over 15.017 s. `mixosd.service` active.

Four earlier attempts stopped before writing anything. Each was a defect in the
deployment tooling rather than a device fault, and all four are now fixed or
documented; the causes and the reasoning are in `ESP_OTA.md`. In order: the
staged package omitted `tools/_mixlib/guards.py` and its siblings so the job
died at import; `DEPLOYED_APP_SHA256` was stale; an aborted attempt left the
esptool stub running and the next one required a fresh ROM; and button-entered
download mode gave the USB-Serial/JTAG identity, whose `0x4000` stub write
block cannot express the migration's `0x1000` table and `0x2000` otadata
writes.

The physical LCD appearance, touch response and keyboard input after this
update remain unverified; only USB enumeration and the heartbeat were observed.

## Current host-authorized migration — 2026-09-11

The user explicitly removed mandatory screen confirmation. New app `bf60ca6891029d011d1d5c8cddb59ae4e711a1404b017ab042a65abeac7819e5` (857008 bytes) automatically grants a valid maintenance request; the matching one-use ENTER_BOOT is still required. See `ESP_HOST_UPDATE.md`. Host `--execute`, exact identity/security checks, fresh full backup, single-submission writes and complete readback remain mandatory.

Receipt `build/deploy/mixos-display-20260911-040257.json` was started once after fresh hardware ROM enumeration as `303a:1001`, device 18, with the exact MAC and USB path. It includes pinned esptool 5.4.0 and modern ESP32-S3 stub 1.2.2, including the released USB full-packet termination fix. All 55 esptool package files match the pinned official source distribution. Its isolated import with a controlled configuration and artifact dry run passed on the CM5 without opening hardware. Full regression passed: **198 Python tests, zero skips; CTest 2/2**, with the actual font and renderer enabled. The fresh 8 MiB backup completed in approximately 165 seconds and was durably saved with SHA-256 `34bd99cae7f4a555281f19a4d65ddb5f3d6ac7dec4129f4ea32223adb51276a0`. Live layout/app validation passed; the 2 MiB app and 4 MiB font partitions were each written once and passed chip-side hashes. The complete 8 MiB readback then exactly matched the expected image, SHA-256 `b7d8e82940788c2040f6a54f99e138ba6e4a53db9e59e232c11648f5a0509f8b`, including all unchanged protected bytes. Both complete snapshots were copied to `build/deploy/display-20260911-040257-{original-flash,readback}-8MB.bin` and their local hashes verified.

The original job retains its truthful exit-code failure: its default RTS reset did not leave hardware-entered ROM within 40 seconds, after readback had already passed. It was not restarted. A separately claimed boot-only operation checked the stopped job, exact stored readback, unchanged USB device 18, exact MAC, zero security flags and cleared force-download register, then issued one official ESP32-S3 watchdog reset without programming flash. The new application enumerated as `303a:80c3`, `TD0720`, device 20. A separate post-flash check passed HELLO plus three heartbeats over 15.016 seconds and observed automatic UPDATE_READY approximately 0.200 seconds after PREPARE without local touch/key input. That check sent no ENTER_BOOT and performed no flash programming. `mixosd.service` was restored and confirmed active. The physical LCD appearance and keyboard input remain unobserved.

The prior read-only diagnostic at 11:40:10 (UTC+8) failed to connect after a software reset (`termios` input/output error); it performed no flash read or write and restored the old application USB `303a:80c3`, `TD0720`, device 17. The subsequent hardware ESP BOOT + S3_EN entry produced fresh ROM device 18 for the migration, without screen confirmation or a countdown. The keyboard remains ROM device 8, unchanged. Evidence: `build/deploy/display-20260911-host-authorized.json`.

## Previous attempt — 2026-09-11 10:47 (UTC+8)

Job `mixos-display-20260911-024626` **received fresh local confirmation** at 10:46:48, identified the exact ESP ROM/MAC and 8 MiB flash, and started the backup with pinned esptool 4.8.1. It reported 64/128/192 KiB in approximately 1.24/2.46/3.69 seconds, then stopped at 10:47:04 with `Packet content transfer stopped (received 4033 bytes)`. No backup was published, no write claim exists, and no app/font flash writes started. Worker/control PIDs are zero. `mixosd` restarted, but the ESP remains USB-OTG ROM `303a:0009`, device 16; no app link is claimed. The prior slow measurement used `303a:1001`, so the current faster partial read cannot be attributed solely to the tool upgrade. No programming or consent replay was attempted after failure. User was told they could leave the device powered with Host retained; no further physical action requested. Evidence: `build/deploy/display-20260911-024626-attempt.json`.

## Previous attempts — 2026-09-11 10:29–10:36 (UTC+8)

Job `mixos-display-20260911-022937` subsequently started at 10:33:32 but also timed out waiting for local approval at 10:35:02 without writing. The user reported no dialog. A separate HELLO/PONG-only trace confirmed the old application remained responsive. Source inspection established that the host waits 90 seconds but firmware grants only a 30-second pending-confirmation window; a waiting log alone does not establish popup display. Evidence: `build/deploy/display-20260911-022937-attempt.json`.

- The old ESP application recovered before this attempt and passed HELLO plus three heartbeats over 13 seconds.
- Display-only tooling now pins a locally built wheel from official esptool 4.8.1 sources, including the released USB Serial/JTAG read-flush fix. Wheel SHA-256: `d554ff923993071819b60b9e3d4b20185de5ef28a7134407ded742b469dfd5fb`. The app-only updater and firmware/font bytes are unchanged. Existing read deadlines and single-submission writes are unchanged. CM5 isolated import/default-stub loading passed; actual improved read throughput remains unverified.
- Regression: 180 Python tests passed with no skips; CTest 2/2 passed.
- Job `mixos-display-20260911-021351` requested local confirmation at 10:18:43 and exited at 10:20:13 because no confirmation arrived. Its stopped process IDs are zero. It has no boot-send, ROM, backup or write events and no flash writes.
- The kernel recorded the ESP entering `303a:0009` download mode at 10:27:20, after the job had stopped. The user reported clicking late and a black screen. Firmware has not been updated by this job.
- Fresh receipt `build/deploy/mixos-display-20260911-022937.json` is **staged only**, with no active confirmation countdown. The user was asked to short-press **S3_EN alone**, BOOT released and Host retained. Verify the old application returns and wait for user readiness before starting the fresh job. Do not restart the expired job or reuse its consent.
- Evidence: `build/deploy/display-20260911-attempt.json`. Keyboard remains ROM `0483:df11`, device 8; no keyboard operations were performed in this session.

## Earlier deployment and recovery state — 2026-09-11 (UTC+8)

The receipts below identify the original packages and one subsequent bounded recovery attempt. Both display jobs are stopped; neither wrote flash. The original display consent was consumed by the one-use recovery claim, and neither job may be blindly restarted.

- Display receipt: `build/deploy/mixos-display-20260910-175627.json`; staging `/home/pi/mixos-display-20260910-175627`. The Pi-side no-write preflight passed for the new app and font. App SHA-256: `c0e99db4d5cb854e5ea2bf24753fcfb17ef77f8bce7b3b1c6a20b824418e2a69`; font SHA-256: `859ec555afa5dfbd234fc80e7058aba243d85e3f32551f266d0457ea326d8bfc`.
- Keyboard receipt: `build/deploy/mixos-keyboard-20260910-180127.json`; staging `/home/pi/mixos-keyboard-20260910-180127`. It contains the **15,900-byte `.raw.bin`**, SHA-256 `932c9892566e53050636c9b06d005ce91885c908d60f6ecd0e9779d12c4b1b82`, not QMK's DFU-suffixed `.bin`. ELF audit reserves 5,080 of 6,144 SRAM bytes including both stacks; the final two flash pages remain reserved for EEPROM.
- **Keyboard flash was written and fully verified.** The worker saved all 32,768 original bytes (SHA-256 `d0af7028ce83645aa7db1225236ab84bcb01bc16aa47f67f3ecd997da0766dcf`), wrote 16,384 application-covered bytes once, and verified all 32,768 bytes including unchanged tail pages (SHA-256 `83787b1b472d0452825fe4732c141a6a50227a99379b4e89ed571b24bc495920`). Evidence remains under `/var/lib/mixos/keyboard-flash/mixos-keyboard-20260910-180127`.
- Its first DFU leave returned zero, but the kernel recorded ROM re-enumeration in the same second. A separate tested `tools/leave_verified_keyboard.py` diagnostic freshly uploaded and matched the exact full-flash hash, then issued exactly one leave-only command without programming. That command also returned zero; ROM re-enumerated as device 8 at 18:44:51 UTC. Evidence: `/var/lib/mixos/keyboard-flash/mixos-keyboard-leave-20260910-184500`. No further leave or programming retries are authorized by this diagnostic. The retained SRAM marker alone is insufficient to explain both returns. Application boot and keyboard input remain unverified.
- **The display update received local approval but stopped before any flash write.** ROM security checks passed and the stub started, then the full-backup stream stalled. The original workdir contains no backup, write claim, or write-start event. The service was stopped; `mixosd` restarted. No new application or font bytes were written.
- **ESP hardware recovery succeeded at 18:55:01 UTC.** After locating the correct side ESP BOOT and round `S3_EN` buttons, the user reset the chip. The kernel recorded device 5 disconnecting and fresh `303a:1001` device 9, serial `70:04:1D:D8:54:14`, at `5-1.2`. A bounded no-stub, no-reset, read-only `flash_id` probe successfully identified the ESP32-S3-PICO-1, exact MAC, and 8 MiB flash. Its blank screen is compatible with download mode. Keep Host selected; no further button press is currently needed.
- Normal re-staging stopped locally because current `mix_ui.c` uses LF line endings (SHA-256 `5dbb90ea96823d98cfdb09eaa7ba8b691f369c7b27981990e842d753c5005039`) while the built release recorded CRLF (`afa1e20d3c2481b32da16558b4352951774602d3e5de96deaf871652573c2697`). Reconstructing CRLF from the current 319 LF lines reproduces the exact recorded hash and 23,962-byte length; there is no other content difference. The working file is preserved. Recovery staging explicitly binds to the original approved app/font artifacts rather than rebuilding or changing their hashes.
- Recovery receipt `build/deploy/mixos-display-20260910-190739.json` was staged with `--stage --stage-from-receipt` against the original receipt. It retains every original app/font/layout/helper byte except the corrected font worker and generated launcher. Starting it once with `--resume-no-write-source` verified the original approval/no-write evidence, then exclusively consumed that source. ROM security, stub startup, explicit SPI attach, flash ID and parameters all passed. Reading the first 256 KiB backup chunk reported 65,536 bytes at 47.17 seconds, then hit its 60-second deadline. The worker exited with an audited failure at 19:09:17 UTC, before any backup publication or flash write; `mixosd` restarted. This demonstrates slow progress, not a total lack of communication. Exact cause remains unresolved. No automatic resume replay is permitted.
- A subsequent no-write `usb_reset` / chip-MAC probe / hard-reset command completed successfully, but the ESP remained `303a:1001`; application USB and heartbeat were not observed. The next minimal physical action is a short press of **S3_EN alone**, with ESP BOOT released, to leave hardware-entered download mode. Keep Host selected.
- Final automated regression: **175 Python tests passed, zero skips; CTest 2/2 passed**. Build-source verification permits only exact recorded bytes or their LF/CRLF representations; firmware and font hashes remain strict.
- Both application links are currently unavailable. Chinese display and actual I2C keyboard operation are **not verified**. `mixosd` being active does not establish a working ESP link.

At start, reviewed payloads are installed into an exclusive root-owned package. Display package: `/opt/mixos-display-packages/JOB`; its unprivileged flash worker writes backups/audit under `work/session`. Keyboard package: `/var/lib/mixos/keyboard-packages/JOB`; its privileged worker writes backups/audit under `/var/lib/mixos/keyboard-flash/JOB`. Uploaded home-directory staging never serves as a privileged executable source without hash-verified root-owned installation.

- **Validated fast recovery flash (2026-09-12):** The application-oriented launcher rejected a correctly hardware-entered ROM device because it required the running-app USB identity. A separate explicit, operator-approved recovery path stopped `mixosd`, saved a complete 8 MiB backup, wrote the corrected application package, completed `Hash of data verified`, read the full 8 MiB back, matched the 859824-byte application segment to SHA-256 `f2ffff9bb37f1c2caae1d52d9a860204eb3e43bcef8d7e07f0089949fdc7e24d`, and restored `mixosd` to active. The complete-image hash differs from the backup by design because the application was replaced. The recovery path is now documented in `docs/HOST_TOOLS.md`; it must be implemented as a reviewed tool before routine use.


A deployment record must identify the actual job receipts, artifact hashes, backup paths, readback result, and observed application/USB state. Hardware-only display and keyboard input behavior must be described as observed or unverified, not inferred solely from enumeration or build results.

Local AI model execution, independently verified CM5 power sequencing, and standalone local audio operation are not provided or certified by this app/font/keyboard deployment. The Linux service is a headless terminal/metrics/task service, not a Linux reinstallation or an AI model runtime.
