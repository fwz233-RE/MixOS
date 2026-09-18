# STM32 键盘构建

所有项目命令从仓库根目录执行，建议使用 Linux／WSL。构建不刷写、不打开串口、不连接 SSH，但会复制键盘源码到指定 QMK 工作区，并更新本地构建产物及 `firmware/keyboard/QMK_PIN.json` 的验证字段。

## 环境

- QMK 源码固定为 0.28.0，具体 commit 见 [QMK_PIN.json](QMK_PIN.json)。QMK CLI 的版本与固件版本是不同概念。
- 需要 ARM GCC、binutils、newlib、GNU make、QMK Python 依赖及主机 C 编译器。参考工具链为 ARM GCC 10.3.1、binutils 2.38。
- 在固定版本 QMK 工作区内初始化所需子模块：

```sh
git submodule update --init --depth 1 lib/chibios lib/chibios-contrib lib/printf lib/lufa
```

LUFA 为构建提供 USB 描述符类型，不会启用物理按键的 USB HID 输入。构建驱动在编译前核验 QMK 及所需子模块版本。

## 构建命令

回到 MixOS 根目录，使用已安装 QMK 依赖的 Python：

```sh
python tools/build_keyboard.py --jobs 4
```

默认 QMK 目录为 `.tools/qmk-0.28.0`，可用 `--qmk-root PATH` 指定同一固定版本的其他工作区。`--build-only` 仅用于编译排错，不建立完整验证状态。

驱动使用 `qmk compile --clean` 分别编译 `default` 和 `diag`。清理构建可避免新增 `mcuconf.h` 等覆盖头文件后仍复用旧依赖对象；不要用来源不一致的增量产物替代发布构建。

后续步骤包括 ELF 内存审计、固定上游源码验证和键盘测试。上游验证会下载少量固定 commit 文件；源码验证与目标编译、硬件验证是不同检查。

## 产物与原始镜像

`build/keyboard` 保存二进制、ELF、链接映射、内存审计、日志、工具版本、来源清单及 SHA-256 摘要。大小与摘要以本次构建结果为准，不复用文档中的历史数值。

严格 DFU 工具使用 `keebdeck_6r11c_default.raw.bin` 或 `keebdeck_6r11c_diag.raw.bin`：

- 从 ELF 导出原始二进制。
- 校验 QMK `.bin` 的 DFU 后缀、身份及 CRC。
- 要求原始镜像与去掉后缀后的 QMK 内容逐字节一致。
- 调用刷写工具的可移植镜像验证函数，不调用硬件入口。

对已有且来源仍一致的完整验证构建，可以不重新编译地导出：

```sh
python tools/build_keyboard.py --export-raw-only
```

该模式仍会核对保存的源码和产物摘要，并运行导出相关测试；不能用于给任意旧镜像补造来源。

## 容量与验证边界

`ld/STM32F042x6.ld` 将应用 Flash 限制为 30 KiB，保留最后 2 KiB 给 EEPROM 仿真；SRAM 为 6 KiB。审计计入加载内容、静态 RAM、两个保留栈及对齐，遗漏栈符号或超限都会失败。

只有两个目标编译、内存审计、上游验证及键盘测试均通过后，工具才设置目标构建验证字段。静态容量检查不证明运行期栈峰值、冷启动、USB 时序、实际 I²C 上拉或矩阵行为。

仅运行离线键盘测试可使用：

```sh
python -m unittest discover -s tests -p 'test_keyboard*.py' -v
```

时钟测试需要固定版本的 QMK 源码，`QMK_HOME` 可指定其位置。启动实现见 [STARTUP.md](STARTUP.md)，部署前提见[键盘刷写](../../docs/KEYBOARD_FLASH.md)。
