# Linux 工作流验证记录

验证日期：2026-09-09。只测试本工作流文件，没有安装服务、修改默认启动目标、连接物理设备或刷机。

## 环境与结果

| 环境 | 协议测试 | Linux/安全状态测试 | 结果 |
| --- | --- | --- | --- |
| Windows Python 3.12.0 | 10/10 通过 | 20 通过、3 个 Linux-only 显式跳过 | 共 33 项，无失败 |
| WSL Ubuntu，Python 3.10.12，UID 1000 | 10/10 通过 | 23/23 通过 | 共 33 项，无失败 |
| WSL `tic -c linux/mixos.terminfo` | — | 语法验证 | 通过，不安装 terminfo |

Windows Python 实际可执行路径为 `C:/Users/123/AppData/Local/Programs/Python/Python312/python.exe`；虽然命令发现和 Glob 搜索没有找到它，已通过绝对路径运行成功。WSL 用 `C:/Windows/System32/wsl.exe` 启动测试。所有 Python 测试带 `-B`，不产生字节码缓存。

执行命令（项目根目录）：

- `python -B -m unittest discover -s tests -p test_protocol.py -v`
- `python -B -m unittest discover -s tests -p test_linux.py -v`

## 覆盖范围

- 确定性 HELLO/DATA 黄金帧、标准 CRC32 向量、COBS 边界长度和随机内容。
- 逐字节分片、每个分割点、连接帧、CRC 错误、错误版本/标志/长度、畸形 COBS、10 万字节过长帧后重新同步。
- epoch 切换、重复 HELLO、陈旧/重复序列号、32 位回绕、会话隔离、绝对累计 credit 与回绕、旧 epoch 半写帧后的发送重同步。
- 有界队列、EAGAIN、短写、零写断开、输入溢出明确 ERROR/CLOSE、输出洪泛反压与心跳控制帧。
- PTY 维度修改、EXIT、8 秒超时清理、固定 64 MiB SHA256 结果/进度/取消、缺失主机指标 null。
- 魔串仅为普通输入、服务拒绝维护、updater 等待 ESP HELLO、不响应旧 epoch PING、合法 PING/PONG、维护请求匹配、授权超时、新 epoch 中止、ENTER_BOOT 只用一次。
- 合成 ESP 镜像的哈希/芯片/段校验/分区拒绝；dry-run 和不支持的自动刷写路径保证不打开硬件。
- root 服务入口拒绝；headless dry-run、备份、还原和人工修改冲突拒绝。所有 systemctl 修改调用均为 mock，测试日志中的“target changed”是模拟结果，不代表机器设置被改变。
- Linux 真实 PTY shell 输入/输出、退出码、raw 串口短写，以及普通用户运行 selectors 守护进程，通过另一端 PTY 完成 HELLO→OPEN→CREDIT→INPUT/DATA→STATUS→PING/PONG，并模拟拔线。使用的是本机虚拟终端，不是 USB 设备。

## 保留限制

自动 app 刷写明确拒绝，原因及后续安全验收要求见 `linux-update.md`。实际 ESP 固件与屏幕确认、USB/ROM 身份连续性、真实设备分区/eFuse 读回、udev/systemd 部署、硬件恢复和长时间压力测试仍未验证。

shell 进程退出时尚未读取的尾部输出可能丢弃；离线没有重挂载或重放。普通 shell 不是安全沙箱，手动运行时自行脱离进程组的后代不保证被清理；systemd 部署应使用所提供的 KillMode=control-group 并审阅隔离策略。
