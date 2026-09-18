# 主机维护授权

MixOS 的维护请求通过 USB 协议的维护通道传递。普通终端文本不会触发刷写或进入 ROM，屏幕确认也不是日常应用更新的前提。

## 两类维护操作

- **应用 A/B 更新**：在当前应用内接收并验证候选，写入非运行槽，重启后进行健康确认；见 [ESP32 日常更新](ESP_OTA.md)。
- **进入 ROM 下载模式**：使用 PREPARE_UPDATE／ENTER_BOOT 握手，供独立批准的首次安装或恢复流程使用。准备成功本身不刷写、不复位。

这两类流程使用不同操作，不能把进入 ROM 的授权当作任意 Flash 写入许可。

## PREPARE／ENTER_BOOT 条件

固件只接受当前在线连接中、正确维护通道、非零请求标识、当前 epoch、新序列和合法空载荷的 PREPARE_UPDATE。终端先关闭；只有 UPDATE_READY 成功入队才授予该请求一次性许可，期限为 15 秒。

ENTER_BOOT 必须具有相同请求标识与 epoch、正确通道、新序列、合法空载荷，并处于有效期内。许可在发布启动请求前消费，领取启动请求也只发生一次。

断连、链路重建、心跳超时或到期都会撤销许可；重复 PREPARE 不能延长期限。正在接收或提交应用更新时，不能并发开启 ROM 操作。

## 授权边界

固件自动响应合法主机维护握手，不要求操作屏幕。Linux 操作员仍需明确授权实际设备操作，运行器仍需校验目标、构建来源和允许变化范围。

协议使用 CRC、epoch、session 和序列号防止数据损坏与陈旧消息混入，不认证主机身份。只有受信任用户才能访问维护串口；普通 shell 拥有用户实际权限，不是命令沙箱。

当前固件不使用旧的文本魔串进入下载模式。将类似命令打印到终端不会获得维护权限。

## 相关测试

从仓库根目录运行：

```sh
python3 -m unittest discover -s tests -p test_link_update.py -v
python3 -m unittest discover -s tests -p test_link_io.py -v
python3 -m unittest discover -s tests -p test_ota_health_ack.py -v
```

测试覆盖自动许可、请求绑定、有效期、重复进入、断连撤销和队列故障。主机测试不能替代具体板卡的 ROM 身份、硬件复位与读回检查；恢复条件见 [ESP32 恢复说明](ESP32_RECOVERY_FLASH.md)。
