# Linux 测试

Linux 服务与更新工具的离线测试使用模拟设备、文件／服务替身或本机伪终端，不通过这些用例刷写物理硬件。完整环境配置与可选项见 [测试指南](TESTING.md)。

## 运行

从仓库根目录执行：

```sh
python3 -B -m unittest discover -s tests -p test_protocol.py -v
python3 -B -m unittest discover -s tests -p test_linux.py -v
python3 -B -m unittest discover -s tests -p test_host_integration.py -v
python3 -B -m unittest discover -s tests -p test_serial_transport.py -v
tic -c linux/mixos.terminfo
```

最后一条命令只检查 terminfo，不安装数据库。真实 PTY、文件锁和信号相关测试要求 Linux；Windows 上的跳过应以测试输出为准，不能当作已执行。

## 关注点

- COBS／CRC、分片、畸形帧、长度上限和重新同步。
- HELLO、epoch、会话与序列回绕，旧消息与旧输入拒绝。
- 累计信用额度、有界队列、短写、EAGAIN 和输出反压。
- 伪终端输入／输出、退出、窗口尺寸、断连和进程清理。
- 普通用户运行、root 拒绝、精确串口身份和独占访问。
- 服务停启、维护锁、持久任务、失败结果和同事务对账。
- A/B 完整身份、维护确认、持续心跳及有限只读重测。

更新侧用例包括 `test_mixos_esp_update.py`、`test_ota_v2.py`、`test_ota_supervisor.py`、`test_ota_diagnostics.py` 与 `test_ota_remeasurement.py`。

## 时间回归

```sh
python3 -B -m unittest discover -s tests -p test_time_sync.py -v
```

该文件覆盖主机单次 UTC 采样、运行中时区刷新、UTC／Asia/Shanghai／半小时及四十五分钟偏移、夏令时边界，以及 Python 协议帧经生产 C 解码器、真实 cJSON 和 `mix_link.c` 后的时间状态。故障注入包括 STATUS 丢失／字段缺失或非法、USB 断连重连、连接 epoch 更新、队列故障、心跳超时、旧序列与损坏帧、主机前后校时、32 位毫秒回绕、模拟离线 50 天和 Unix 秒溢出。

真实时区规则用例要求 POSIX `tzset`；Windows Python 会跳过这两项，应在 WSL 内运行 Python 补齐。C 链路测试使用现有 vendored cJSON、POSIX 编译器及 AddressSanitizer／UndefinedBehaviorSanitizer；缺失依赖会明确跳过。测试不修改实际系统时区、不连接 Pi，也不刷写硬件。现场时区设置与断线保时限制见 [Linux 主机服务](linux.md)。

## 限制

伪终端不能证明真实 USB 端点、重枚举、udev 权限或芯片复位行为。模拟服务也不代表生产 systemd 权限已经配置。

shell 拥有其普通用户的实际权限，不是安全沙箱。手动运行时，自行脱离进程组的子进程不一定被清理；systemd 部署还需要审阅进程组与隔离策略。现场运行记录应本地保存，不将某次测试结果写成所有环境的保证。
