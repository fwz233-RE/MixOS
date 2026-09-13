# Linux 主机工作流

## 范围与安全边界

`linux/mixosd.py` 是仅依赖 Python 标准库的 Linux 服务，推荐 Python 3.10 及以上。它作为明确配置的普通用户启动一个交互式 PTY（伪终端）shell，绝不做 root 自动登录。运行入口检测到有效 UID 为 0 会拒绝启动。USB 串口权限和实体设备访问构成信任边界；CRC32 仅检测数据损坏，不提供身份认证。

Python 协议实现位于 `linux/protocol.py`，遵循 `protocol/USB_V1.md`；ESP 端 C 编解码由另一个工作区所有者实现。USB 音频不受本服务修改。服务只接受现有 CDC 通道上的 COBS 帧；普通终端 payload 中的 `REBOOT_TO_BOOT_MODE` 没有维护功能。

## 操作员配置后手动启动

1. 用 `id`、`udevadm info --attribute-walk --name=/dev/ttyACM…` 和 `/dev/serial/by-id/` 确定普通用户、串口组、USB VID/PID、序列号和 CDC 接口编号。确认该设备是自己的 MixOS。
2. 审阅 `linux/99-mixos.rules.example`，填写所有 `CONFIGURE_*`。该文件是样板，不能原样安装。规则仅授权指定串口给指定组，模式为 0660，不使用全局可写权限，不匹配所有 ttyACM。
3. 手动编译保守 terminfo 到该普通用户的目录：`tic -x -o ~/.terminfo linux/mixos.terminfo`。能力仅包括基本光标移动、清屏、8 色、粗体与反显、方向键；不宣称鼠标、真彩色、备用屏幕或完整 xterm 兼容。
4. 从普通用户会话运行 `python3 linux/mixosd.py --device /dev/serial/by-id/实际设备标识 --shell /bin/bash`。shell 必须是绝对路径。该 shell 拥有此用户的实际权限，不是命令白名单沙箱。
5. 如需 systemd，审阅并填写 `linux/mixosd.service` 的 User、Group、WorkingDirectory、ExecStart 设备路径，之后由操作员自行安装和启用。本项目没有自动安装、提权、添加组或开启 root 登录脚本。

`TIOCEXCL` 和文件锁保证协作程序的独占访问；端口设为 raw/nonblocking，清空旧输入，关闭 HUPCL，不主动切换 DTR/RTS 来复位。即使这样，不同板子的 USB/串口实现仍需实机验证。

## 链路与资源行为

- ESP 发起 HELLO；主机只回复 HELLO_ACK，不与它竞争发起连接。重复 HELLO 保留当前会话，新的非零 epoch 会关闭旧 PTY、取消工作、丢弃未发送内容和旧输入。
- 全局序列号进行 32 位模比较，旧帧与重复帧丢弃。会话 ID 单独验证；同一时刻只有一个 PTY 和一个示例工作。
- 双向 2 秒心跳；8 秒无有效接收会清空链路状态。串口 EOF、错误或队列耗尽会关闭端口和会话，1 秒后重试同一明确设备路径。没有会话重挂载、输出重放或旧按键重放。
- 发送队列上限 16 KiB，DATA 高水位 8 KiB；PTY 输入队列上限 4096 字节。只有 DATA 消耗累计 CREDIT，窗口最大 4096。没有 credit 或发送队列到高水位时停止读取 PTY，让操作系统反压 shell。
- 写入保留未完成后缀，处理 EAGAIN 与短写；单轮 I/O 工作量有限。控制帧不消耗终端 credit，并保留发送队列空间。控制洪泛耗尽硬上限时关闭连接，而不是无限积压。
- INPUT 溢出返回 ERROR 与 CLOSE，并销毁会话，不静默丢弃一部分命令继续运行。断连对 PTY 进程组发送 HUP/KILL；正常守护进程退出时 systemd 的 KillMode=control-group 也清理后代。自行脱离进程组的用户程序可能需 systemd cgroup 清理，普通手动运行没有完整进程沙箱保证。
- shell 退出发送 EXIT。原型在进程退出时可能丢弃尚未从 PTY 读取的尾部输出，尤其是在无 credit 时；这不是可恢复终端录制服务。

状态请求返回 `/proc/uptime`、`/proc/stat` 的 CPU 差分和 `/proc/meminfo` 的可用内存；首次 CPU 采样和不可用指标返回 null。温度始终为 null，因为没有配置确定含义的主机传感器，不能把任意 thermal_zone 猜作 CPU 温度。状态请求每秒最多一次。

固定 `sha256` 示例对固定 64 MiB 内容逐块计算摘要，每次事件循环处理 64 KiB，报告整数百分比，支持按工作 ID 取消。协议不接受文件路径或 shell 命令，不做 shell 插值。工作结果包含实际字节数和摘要。

## 可撤销 headless 设置

`linux/headless.py` 只调整 systemd 的下一次启动默认目标，不卸载桌面、不删除 GPU/音频/输入驱动、不修改启动参数、不停止当前桌面、不自动重启。

- 预览：`python3 linux/headless.py enable --backup /自己选择的安全路径/mixos-headless.json`
- 实施需要操作员明确以 root 调用，并追加 `--execute --confirm multi-user.target`。工具从不调用 sudo。
- 实施前用 O_EXCL 创建 0600 JSON 备份并 fsync，已有备份拒绝覆盖。
- 还原预览：`python3 linux/headless.py restore --backup /原备份路径`
- 还原实施追加 `--execute --confirm graphical.target`，确认值必须与备份中的原目标一致。若当前目标已被别人修改，拒绝覆盖。还原后保留备份。
- 仅支持已知的 graphical.target 与 multi-user.target；其他系统必须人工审查。

## TypixDeck TD0720 实机部署（2026-09-10）

本机已将 `linux/mixosd.py`、`linux/protocol.py` 与 terminfo 安装到 CM5 的 `/opt/mixos/linux/`。实际 systemd 配置来自 `linux/mixosd.typixdeck.service`，以普通用户 `pi`、组 `dialout` 启动，精确设备为 `/dev/serial/by-id/usb-TypixDeck_TypixDeck_UAC+CDC_TD0720-if03`。udev 配置来自 `linux/99-mixos.rules.typixdeck`，只匹配 VID `303a`、PID `80c3`、序列号 `TD0720`、接口 `03`，权限为 `0660`；这些实机值不能直接复制到其他设备。

CM5 原生 Debian 13 上的协议/Linux 测试 33 项全部通过，包括 3 项 Linux 专属 PTY/raw 串口测试。默认启动目标已由 `graphical.target` 改为 `multi-user.target`，恢复记录在设备 `/var/lib/mixos/headless-target.json`。切回 Host 并重启后，内部 Hub、STM32 键盘与 ESP32 均正常枚举；`mixosd` 连续运行、重启计数为 0，10 秒采样期间读取字符增加 9,970、写入字符增加 821，确认协议链路存在实际双向流量。

设备的 CM_USB 由 SW8 在底部 USB4 与内部 Hub 之间二选一。USB Gadget 网络维护模式与 ESP32/键盘内部 USB **不能同时使用**：

- 正常 MixOS：SW8 置 Host，配置 `dtoverlay=dwc2,dr_mode=host`，Linux 可连接内部 Hub、ESP32 和键盘。
- USB 网络维护：先在 Linux 执行 `sudo rpi-usb-gadget on` 并重启，再将 SW8 置 Device，电脑接底部 USB4；Windows RNDIS 下 CM5 默认为 `10.12.194.1/28`。此时内部 ESP32/键盘从 CM5 断开。
- 返回正常模式：通过 USB SSH 执行 `sudo rpi-usb-gadget off`，确认配置中不再存在 `dr_mode=peripheral`，将 SW8 置 Host 后重启。避免在 USB SSH 前台直接关闭当前网卡后再依赖同一会话执行剩余命令。

## 更新与验证

更新边界、维护握手及拒绝刷写原因见 `docs/linux-update.md`。服务本身永远拒绝维护请求；操作员先停止服务，独立 updater 才能占用串口。

无需安装依赖的测试命令：

- `python3 -B -m unittest discover -s tests -p test_protocol.py -v`
- `python3 -B -m unittest discover -s tests -p test_linux.py -v`
- `tic -c linux/mixos.terminfo` 只检查描述语法，不安装数据库。

Windows 显式跳过 Linux-only PTY/raw serial 测试，其余协议、队列、会话、工作和安全预检状态测试可运行。测试使用假设备或本机 PTY，从不打开物理设备、安装服务或刷机。
