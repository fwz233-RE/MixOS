# 文档导航

文档按项目功能组织，命令默认从仓库根目录执行。示例中的主机地址、设备路径、用户和镜像参数必须替换为实际配置。

## 开发入门

- [系统结构与接口](IMPLEMENTATION.md)：各处理器和主要模块的职责。
- [开发环境](HOST_TOOLS.md)：Python、C 编译器、ESP-IDF 与 QMK。
- [构建指南](BUILD.md)：固件构建、发布包和构建来源校验。
- [测试指南](TESTING.md)：离线检查、平台依赖与可选测试。
- [来源与许可证](SOURCES.md)：上游版本、第三方代码和字体限制。

## Linux 与应用

- [Linux 主机服务](linux.md)：USB 串口权限、普通用户终端和 systemd。
- [应用与 AI 后端](AI_DECK.md)：笔记、翻译、输入法及语音依赖。
- [Linux 测试](linux-testing.md)：协议、伪终端和服务生命周期测试。
- [更新工具选型](linux-update.md)：日常 A/B 更新与旧维护工具的区别。

## 固件与硬件

- [部署流程](DEPLOYMENT.md)：准备、检查、安装与恢复边界。
- [ESP32 日常更新](ESP_OTA.md)：发布包、任务查询与异常处理。
- [A/B 更新实现](ESP_OTA_V2.md)：事务、健康确认与有限通信恢复。
- [主机维护授权](ESP_HOST_UPDATE.md)：维护协议与进入 ROM 的区别。
- [ESP32 恢复](ESP32_RECOVERY_FLASH.md)：独立授权的 ROM 恢复条件。
- [键盘实现](keyboard.md)与[键盘刷写](KEYBOARD_FLASH.md)
- [界面实现](ui-implementation.md)、[字体构建](FONT_BUILD.md)与[字体渲染](ESP_FONT_BUILD.md)
- [音频硬件](AUDIO_HARDWARE_NOTES.md)

## 协议

- [USB 通信](../protocol/USB_V1.md)
- [键盘事件](../protocol/KEYBOARD_V1.md)
- [A/B 更新事务](../protocol/OTA_V2.md)

## 文档与运行记录

公开文档描述功能、接口、操作前提和限制。设备专属的验收流水、完整备份、读回镜像、原始日志及本机清单留在本地，不作为项目使用说明发布。

构建报告和更新回执仍是工具进行来源校验与恢复对账的必要材料，应在本地妥善保存。仓库中的恢复基线描述属于安全策略输入，不是供其他设备直接套用的验收结果；移植时需要独立核对与配置。
