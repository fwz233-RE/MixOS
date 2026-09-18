# Linux 主机服务

`linux/mixosd.py` 连接 ESP32-S3 的 USB CDC 接口，提供普通用户终端、主机状态和应用入口。核心服务使用 Python 标准库；笔记、翻译、输入法和 AI 后端有各自依赖，见 [应用说明](AI_DECK.md)。

## 配置设备与用户

1. 用 `/dev/serial/by-id/` 和 `udevadm info` 核对设备序列号、USB 接口及物理连接。
2. 审阅 `linux/99-mixos.rules.example`，替换其中所有 `CONFIGURE_*`，将串口权限限制为指定设备和组。
3. 选择普通用户运行服务；入口拒绝 UID 0，不提供 root 自动登录。
4. 配置绝对路径的 shell，以及需要使用的应用启动器目录。

终端拥有所选用户的真实权限，不是命令白名单沙箱。USB 串口权限是安全边界；协议 CRC 用于发现损坏，不认证连接者。

## 手动运行

从仓库根目录，以目标普通用户执行：

```sh
tic -x -o ~/.terminfo linux/mixos.terminfo
python3 linux/mixosd.py --device /dev/serial/by-id/DEVICE_ID --shell /bin/bash
```

`--app-dir` 可指定应用启动器目录。服务只按已定义的应用入口分发，不把 USB 消息当任意 shell 命令执行。

`mixos` terminfo 描述终端解析器已实现的能力，包括备用屏幕、滚动区域、插入／删除、256 色和直接颜色；这不意味着完整 xterm 或鼠标协议兼容。服务创建的会话使用该终端类型，应在运行用户的环境中安装 terminfo。

## systemd

`linux/mixosd.service` 是模板，安装前必须核对 User、Group、WorkingDirectory、ExecStart 和设备路径。`linux/mixosd.typixdeck.service` 是特定板卡示例，不是通用默认值。

- 服务进程以普通用户执行，使用 `KillMode=control-group` 清理子进程。
- 串口权限和应用后端权限分别配置，避免为便利而授予任意 root Python 执行权限。
- 查看实际服务状态和日志，不能把启动命令成功当作 USB 会话已经建立。
- 应用更新需要另行配置服务停启策略和维护锁，见 [部署指南](DEPLOYMENT.md)。

## 会话与资源

ESP 发起 HELLO。新 epoch 使旧终端与待发送输入失效；序列和 session 检查用于拒绝陈旧数据。断连不会恢复旧终端、重放旧输出或重新发送历史按键。

服务使用有界队列、信用额度和反压；短写保留未发送后缀，队列或输入溢出明确关闭会话而不是静默丢字节。心跳与控制消息有独立处理，不应被大段终端输出无限阻塞。

离开应用或断连时会清理子进程。手动运行不提供完整进程沙箱，自行脱离进程组的程序需要额外系统策略；systemd 部署应保留进程组清理配置。

## 无桌面启动

`linux/headless.py` 调整下一次启动的 systemd 默认目标，不卸载桌面或驱动，不停止当前桌面，也不自动重启。

```sh
python3 linux/headless.py enable --backup /path/to/new-backup.json
python3 linux/headless.py restore --backup /path/to/existing-backup.json
```

默认仅预览。实施需要管理员明确执行并提供相应 `--execute --confirm` 参数；备份必须唯一且可恢复，已有配置变化时不能覆盖。

## USB 路由

TypixDeck 内部 Host 路由用于连接 ESP32-S3 与键盘。切换到 USB Gadget 网络维护模式可能同时断开内部设备，不能假设屏幕和键盘仍可用。

切换之前应准备独立管理连接与返回 Host 的步骤，避免通过即将关闭的连接执行后续恢复命令。具体板卡开关、系统 overlay 和工具策略必须按硬件核实。

## 维护与测试

正常服务不承担 Flash 写入；独立更新任务协调停服后才独占串口。日常入口与旧维护工具区别见 [更新工具选型](linux-update.md)，离线测试见 [Linux 测试](linux-testing.md)。
