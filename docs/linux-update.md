# Linux 更新工具：离线预检与受限维护

## 当前能做什么

`tools/update_esp.py` 默认 dry-run：校验指定 app 镜像 SHA256、ESP 镜像格式、ESP32-S3 芯片 ID、各段边界、段 XOR 校验、镜像附加 SHA256（如果存在），并核对明确提供的分区 CSV 与当前工厂布局完全一致。不会打开串口、停止服务、复位或刷写。

当前唯一接受的布局是：nvs 0x9000/0x6000、phy_init 0xf000/0x1000、factory app 0x10000/0x200000、font 0x210000/0x400000。app 文件必须完整落在 2 MiB factory 分区内。拒绝全闪存镜像、尾随数据、未知布局、未知芯片以及当前未支持的签名封装。文件哈希必须来自操作员另行核对的可信构建产物；工具计算的哈希本身不证明来源可信。

调用示例（标识与哈希必须替换，以下仅作参数说明）：

`python3 tools/update_esp.py --device /dev/serial/by-id/实际设备 --vid 303a --pid 实际PID --serial 实际序列号 --location 1-2.3 --image app.bin --sha256 64位可信SHA256 --chip esp32s3 --partitions firmware/esp32s3/partitions.csv`

`--location` 是 sysfs USB 物理端口路径，例如 `1-2.3`，不是随重枚举变化的 ttyACM 编号。预检输出显式声明 `live_chip_layout_verified: false`。

## 为什么自动刷写被拒绝

`--execute` 在没有 `--maintenance-only` 时一定拒绝，且在打开任何硬件之前返回退出码 2。执行请求需要 `--audit 用户指定日志.jsonl`，拒绝也会记录原因。当前仓库没有经实机核验的以下安全闭环：

1. 应用 CDC 与 ROM 下载端口的可靠身份连续性；VID/PID/序列号/物理口匹配只是观察，不是硬件认证。
2. ROM 读取芯片型号、修订版、容量与 eFuse 状态，以及 secure boot/flash encryption 的明确策略。
3. 将真实设备分区表读回并与预检表比较，确认写入 0x10000 不影响 bootloader、分区表、NVS 或字体。
4. 固定且验证过的刷写工具版本、app-only 参数和复位行为，以及写后逐字节/摘要读回验证。
5. 启动新 app 后重新发现正确物理设备、等待新的 HELLO epoch 并验证健康状态的硬件测试。

因此工具没有隐藏的 write_flash、寄存器/内存写命令或“忽略校验继续”开关。满足离线校验并不授权自动刷机。应先建立并审阅硬件更新配置和测试，再实现真正刷写。本次工作没有连接硬件或运行任何刷写命令。

此工厂布局没有 A/B 回滚；app 损坏可能必须通过硬件 BOOT+RESET 进入 ROM 恢复。全闪存备份/恢复是另一个单独审阅的流程，不属于 app-only 工具。

## 维护握手：只能进入 ROM，仍然不刷写

Linux 操作员可明确选择 `--execute --maintenance-only`，再提供：

- `--service-stopped`：操作员已经停止正常 `mixosd.service`；工具不会自行调用 stop 或 sudo。它还运行只读 `systemctl is-active` 检查，并要求明确 inactive/failed 状态。自定义 unit 用 `--service` 指定。
- `--rom-vid`、`--rom-pid`、`--rom-serial`：提前确认 ROM 重枚举后的身份。ROM 身份必须可与应用身份区分，物理端口仍必须一致；没有稳定序列号或未知 ROM 身份的设备应拒绝自动化。
- `--audit`：用户可写、追加的 JSONL 审计日志。记录预检、操作员确认、设备本地确认、ENTER_BOOT 发出、ROM 重枚举观察或中止原因，并 fsync。该日志是本机记录，不是防篡改审计系统。

流程：

1. 要求 Linux 非 root 用户及串口权限；使用明确 `/dev/serial/by-id/` 路径，检查 sysfs 的 VID/PID/序列号/物理口后独占打开，打开后再次核对。
2. 终端必须是交互式本地输入。操作员完整输入 `ENTER BOOT 实际序列号`，不能通过普通协议 payload 或终端文本替代。
3. updater **等待 ESP 主动 HELLO**。服务停止后旧 epoch 最晚在 ESP 的 8 秒超时后失效。updater 不把旧 PING 当成新连接，也不在等待 HELLO 时回复 PONG 延长旧 epoch，因此不会与 ESP 的发起者角色冲突。
4. 新 HELLO 到达后回复 HELLO_ACK，再发 MAINTENANCE/PREPARE_UPDATE 与随机非零请求 ID。此后回复 ESP 每 2 秒 PING，8 秒无响应则中止；总等待本地屏幕确认上限 90 秒。
5. 只有同一 epoch、同一请求 ID、正确维护 channel 的 UPDATE_READY 才授予本机状态机进入 ROM 的机会。要求设备屏幕上的实体本地确认。服务自身始终拒绝维护请求。
6. 在首次 READY 后 15 秒内只发送一次 ENTER_BOOT。新的 epoch、错误响应或过期确认全部中止，不自动重试危险步骤。固件还必须独立检查本地授权有效期。
7. 关闭旧端口，在 20 秒内查找同一物理口上的明确 ROM VID/PID/序列号，记录观察结果。没有重枚举则报告需要硬件恢复；不猜另一个 ttyACM，也不继续刷写。该工具到此结束，不自动重启服务或触碰设备的闪存。

`REBOOT_TO_BOOT_MODE` 出现在 TERMINAL/DATA、INPUT 或任何任意文本中均不能授予维护权限。CRC 正确的帧也必须满足 epoch、序列、channel、请求 ID 与设备本地授权。

## 测试与未验证项

`tests/test_linux.py` 用合成镜像测试哈希、芯片、段校验和布局拒绝；mock 串口确保 dry-run 和拒绝刷写路径从不接触硬件。维护状态机测试旧 PING、新 HELLO、PONG、本地请求匹配、15 秒授权期限、重复进入拒绝、新 epoch 中止和普通魔串无效。

真实 USB 权限、udev 规则、ESP 屏幕确认、ROM 枚举身份、固件 ENTER_BOOT 行为、安全启动策略和真实闪存读回尚未验证。WSL 的 PTY 测试不替代这些硬件验证。

Python/C 共享黄金帧（包含末尾 00 分隔符）：

- HELLO，epoch=0x12345678、session=0、sequence=1、payload=00020010：`02010201057856341201010102010101020401020206103b55d14a00`
- DATA，channel=1、epoch=0x12345678、session=7、sequence=2、payload=41 00 42 1b 5b 33 31 6d：`04010112067856341207010102020101020802410b421b5b33316d0c1929ef00`
