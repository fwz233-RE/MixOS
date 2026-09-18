# MixOS STM32 键盘固件

本目录是基于 KeebDeck 6×11 矩阵与 QMK 的 STM32F042 键盘固件。物理按键通过 I²C 传给 ESP32-S3，再由 MixOS 处理文本与本地快捷键；不发送普通 USB 键盘、消费控制或鼠标按键报告。

## 硬件与输入

- 固定 QMK 0.28.0，commit `a63fd7f01cdabd9ce85bb09ae2b573fd3b8e60aa`，详见 [QMK_PIN.json](QMK_PIN.json)。
- `default` 与 `diag` 使用相同的自定义物理键映射。USB 仍可能枚举，但 ESP 断连时没有 USB 键盘输入后备路径。
- 在 QMK 去抖与防鬼键处理之后、普通 HID 处理之前截获事件；空白矩阵位置保持 `KC_NO` 并由物理掩码拒绝。
- I²C1 使用 PB6／PB7，地址 `0x1f`；PA13 为开漏事件中断。背光使用 PA15，范围为 0～8，默认 3。
- 行引脚为 PA0、PF1、PF0、PB8、PB5、PB4；列引脚为 PA14、PB3、PA1～PA7、PB0、PB1。PB8 同时承担 BOOT0，恢复时必须考虑板级电气状态。

完整按键行为见[键盘实现](../../docs/keyboard.md)，线协议见 [KEYBOARD_V1.md](../../protocol/KEYBOARD_V1.md)。

## 本地恢复入口

- Bootmagic：上电时按住 Tab（行 2、列 0）。
- 运行期救援：仅按住 Fn（3,0）与菱形键（0,9），或 Sym（5,0）与菱形键，持续 3 秒。

这些入口不依赖 ESP 或 Linux。普通短按菱形键及 Fn+Space 是 ESP 背光操作，不应与救援组合混淆。启动与 ROM 跳转实现见 [STARTUP.md](STARTUP.md)。

## 构建与部署

从仓库根目录，在配置好的 Linux／WSL QMK 环境中运行 `python tools/build_keyboard.py`。工具构建两个 keymap、审计内存、验证固定上游并运行键盘测试，产物保存在本地 `build/keyboard`。

部署使用经验证导出的 `.raw.bin`；QMK 常规 `.bin` 带 DFU 后缀，不能直接交给严格的原始镜像写入工具。详细流程见[构建说明](BUILD.md)与[键盘刷写](../../docs/KEYBOARD_FLASH.md)。

链接器为应用保留 30 KiB Flash，最后 2 KiB 用于 EEPROM 仿真；SRAM 容量为 6 KiB。构建审计包含静态分配、保留栈和对齐，但不能替代实际栈峰值、USB、I²C 和矩阵电气检查。

`QMK_PIN.json` 中的验证字段供构建流程使用，不是其他设备的硬件合格证明。历史批量刷写脚本保持拒绝执行，刷写需单独确认设备身份、备份和恢复条件。

## 许可证

QMK 派生键盘固件采用 GPL-2.0-or-later，见 [LICENSE](LICENSE)。
