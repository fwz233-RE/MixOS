# ESP32-S3 日常 A/B 更新

A/B 是 ESP32-S3 的两个应用分区。Linux 负责发起、传输和核验，固件负责写入、启动选择与试运行确认。日常更新使用应用 USB CDC 通道，不进入 ROM 下载模式，也不要求按 BOOT、RESET 或在屏幕上确认。

## 使用前提

- 设备已有正确 A/B 布局，并运行支持第二版更新协议和维护健康确认的接收端。
- 当前运行状态明确且允许更新，候选镜像适配目标芯片、分区和安全配置。
- Linux 普通用户有精确设备的串口权限，已配置维护锁、受控的 `mixosd.service` 停启权限和 systemd 用户任务。
- 发布包由[构建工具](BUILD.md)生成，包含 `app.bin`、`manifest.json` 和自包含运行器。

当前分区定义见 [`partitions.csv`](../firmware/esp32s3/partitions.csv)。通用移植还需核对源码中固定的 USB 身份与板卡策略；仅更换端口路径不代表适配其他设备。

## 检查发布包

以下命令只检查本地文件，不打开串口、不停止服务：

```sh
python3 tools/mixos_esp_update.py inspect --package /path/to/release
python3 tools/mixos_esp_update.py apply --package /path/to/release --dry-run
```

如果已经安装了 `mixos-esp-update` 入口，也可用它代替 `python3 tools/mixos_esp_update.py`。入口需要指向完整、长期保留的运行器目录，不能只复制一个 Python 文件。

## 查看设备

```sh
mixos-esp-update inspect --live --device /dev/serial/by-id/DEVICE_ID
```

实时检查会取得维护锁、协调服务并访问设备，属于明确的设备操作。`inspect --package` 与 `inspect --live` 具有不同作用，前者不能证明正在运行哪个版本。

## 提交更新

```sh
mixos-esp-update apply --package /path/to/release --device /dev/serial/by-id/DEVICE_ID --timeout 60 --health-timeout 180 --wait
```

`apply` 是实际写入授权。它创建持久任务并交给 systemd 执行；`--wait` 只等待结果，SSH 会话不是更新进程的生命周期。保存输出中的任务 ID。

```sh
mixos-esp-update status --job JOB_ID
mixos-esp-update status --job JOB_ID --wait
```

排队或执行中不代表成功，等待超时也不等于设备已经失败或回退。

## 成功判定

一次更新只有同时满足以下条件才返回整体成功：

1. 运行文件的完整 SHA-256、ELF 摘要和精确长度匹配发布包。
2. 运行槽、启动槽和新启动身份符合本次事务。
3. 固件完成维护健康确认，实际镜像状态为 VALID。
4. 在同一启动与会话上观察到连续、新鲜的协议心跳。
5. Linux 服务恢复到原先应有状态，最终结果已持久保存。

100% 数据 ACK、USB 枚举、旧查询记录或服务 active 均不能单独替代这些条件。

## 超时与对账

先查询已有任务。对已经提交、但主机未观察到最终结果的事务，可以明确请求同一任务对账：

```sh
mixos-esp-update apply --resume JOB_ID
mixos-esp-update status --job JOB_ID --wait
```

`--resume` 不从中断的接收阶段重新上传镜像。仍在接收、身份不符或状态不明时，工具会拒绝不安全的继续操作，不能反复创建新任务来绕过。

运行身份测量只在请求已完整发送、会话不变、有新鲜心跳且无新增解码错误等条件下，最多增加两次只读请求。每次使用新请求 ID，并保持原绝对截止时间；最终仍需新匹配的完整身份回复。它不会因测量超时重刷或重新复位。事务提交阶段原有的幂等 END／REBOOT 对账与此机制分开，见 [实现说明](ESP_OTA_V2.md)。

## 基线保护与恢复边界

- 普通更新只写非运行槽，未知或待确认状态禁止开始下一次更新。
- 受保护 A 槽的替换需要明确授权、已知 B 包及实际 B 健康证据；不能仅凭设备自报摘要解除保护。
- 两个槽无法同时永久保留固定旧版本并无限交替，外部完整备份仍应保留。
- 试运行候选可在复位后由 bootloader 回退；已经 VALID 的应用后来卡死，不保证自动切到另一槽。
- A/B 不替代硬件复位。USB Hub 重置、Linux 重启不等于 ESP32 EN 复位或断电。

首次安装、单槽迁移和 ROM 恢复需要单独授权，不能由日常更新失败自动触发。详见 [部署指南](DEPLOYMENT.md)和 [恢复说明](ESP32_RECOVERY_FLASH.md)。
