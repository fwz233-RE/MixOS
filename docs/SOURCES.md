# 来源、版本与授权

本页记录可复现构建需要的上游来源、版本和授权入口。根目录 [LICENSE](../LICENSE) 适用于其覆盖的 MixOS 内容，不替代键盘固件、组件、字体、模型及其他第三方材料的条款。发布源码或二进制前，应保留相应版权声明、许可证及所需源码。

## 固件基础

- ESP32-S3 基于 `TypixDeck-esp32s3-firmware`，基线 commit `1f29c50`。继承的 MIT 许可见 [ESP LICENSE](../firmware/esp32s3/LICENSE)，第三方组件保留各自授权。
- STM32 键盘基于 `TypixDeck-keyboard-firmware`，基线 commit `a1694e4`。QMK 派生内容按 GPL-2.0-or-later，见[键盘说明](../firmware/keyboard/README.md)和 [LICENSE](../firmware/keyboard/LICENSE)。
- [QMK firmware](https://github.com/qmk/qmk_firmware)：0.28.0，commit `a63fd7f01cdabd9ce85bb09ae2b573fd3b8e60aa`。版本、目标及 keymap 记录在 `firmware/keyboard/QMK_PIN.json`；上游许可入口为 `license.txt`，子模块另有各自声明。
- 本地 USB UAC 组件保留 Apache-2.0，见[组件许可](../firmware/esp32s3/components/usb_device_uac/license.txt)。

MixOS 中的固件副本供本项目构建使用，不应把原始仓库作为生成物目录。基线 commit 表示来源，不表示当前派生版本已经通过实机验证。

## ESP 工具链与组件

[ESP-IDF](https://github.com/espressif/esp-idf/tree/v5.4.2) 固定为 v5.4.2，许可为 Apache-2.0，入口为上游 [LICENSE](https://github.com/espressif/esp-idf/blob/v5.4.2/LICENSE)。

工具布局由 `tools/idf_env.py` 定义：Xtensa GCC 14.2.0／`esp-14.2.0_20241119`、CMake 3.30.2、Ninja 1.12.1、ESP ROM ELF 20241011，以及 IDF Python 3.10 环境。键盘参考工具链为 ARM GCC 10.3.1、binutils 2.38；准备方式见[主机工具](HOST_TOOLS.md)。

`firmware/esp32s3/dependencies.lock` 保存组件来源和完整内容摘要，当前解析版本包括：

- `espressif/cmake_utilities` 1.1.1。
- `espressif/esp_codec_dev` 1.6.2。
- `espressif/esp_lvgl_port` 2.4.4。
- `espressif/freetype` 2.14.3。
- `espressif/tinyusb` 0.19.0~3。
- `lvgl/lvgl` 9.2.2。

组件通过 [Espressif Component Registry](https://components.espressif.com/) 解析，发布时应同时检查组件包和内含上游的许可文件。FreeType 的授权入口为其 `docs/LICENSE.TXT`（FTL／GPLv2 许可选项）；LVGL 和 TinyUSB 各自保留上游 MIT 声明，不能用 ESP-IDF 的许可统括。

组件声明中的版本范围不代替锁文件。升级版本应重新解析、核对许可和覆盖实现，并运行[测试](TESTING.md)，而非只修改文档中的版本号。

## 字体与图标

- MiSans 文本字体由使用者自行提供。当前真实字体集成测试固定输入为 MiSans 4.003，8,092,724 bytes，SHA-256 为 `1a5f4112daaa9473747c6834041646cc9b2c338cb40ab5dbb2f0161f8968ca10`。此摘要用于辨认测试素材，不是授权证明。
- 该输入没有内嵌许可元数据；须向字体供应方取得并保存单独许可，确认子集化、合并图标及再分发条件后再发布。仓库许可不能授权未知来源字体。
- 图标来自 [Google Material Design Icons / Material Symbols](https://github.com/google/material-design-icons)，上游许可入口为 [LICENSE](https://github.com/google/material-design-icons/blob/master/LICENSE)（Apache-2.0）。构建器读取用户准备的 Material Symbols Rounded 文件，不自动下载或固定其上游 commit。
- 图标源文件摘要和固定轴实例写入字体 manifest；发布者还应保存所用下载版本或 commit 及许可副本，不能把可变的上游分支当成固定来源。

字体工具依赖为 fontTools 4.60.1 和 Pillow 11.2.1，见 `tools/requirements-host.txt`。其上游分别保留 fontTools MIT 和 Pillow HPND 许可；实际 Pillow FreeType 引擎版本写入构建清单，与固件的 FreeType 版本分开记录。

构建流程见[字体构建](FONT_BUILD.md)。原始字体、子集字体以及原仓库附带素材均须核对各自来源和授权，技术上可加载不代表可公开分发。

## 翻译后端

上游为 [google-gemma/gemma-translator](https://github.com/google-gemma/gemma-translator)，固定 commit `47f9b3ba40ca3650fb80ee42264a76d6a2b5f8ba`，Apache-2.0。

`linux/apps/translator/vendor` 保存上游 `backend/server.py`、`backend/requirements.txt` 和 `LICENSE` 的原样副本。文件摘要记录于 [PROVENANCE.json](../linux/apps/translator/vendor/PROVENANCE.json)，授权见 [LICENSE](../linux/apps/translator/vendor/LICENSE)。

平台适配位于 `linux/apps/translator/service.py`，不直接修改这些副本。更新时应对固定 commit 重新核验来源和摘要。模型权重和语音素材还需遵守各自许可，后端代码许可不覆盖它们。

## 终端输入法

[adam-ikari/term-ime](https://github.com/adam-ikari/term-ime/tree/1fcb7ae2eab77e1f40947bcb7ac675d49255b3f8) 固定为 v1.0.9，commit `1fcb7ae2eab77e1f40947bcb7ac675d49255b3f8`。源码按需暂存到 `build/ime`，不作为本仓库内的原样上游副本维护。

`tools/stage_ime.py` 的 `PINS` 固定 15 个仓库，包括 term-ime、其直接子模块及 librime 子模块。脚本从固定 commit 下载归档，在 `build/ime/manifest.json` 记录各来源、归档摘要、修改前后摘要及裁剪项。

本地修改包括 librime 的标准头文件补充，以及 term-ime 的控制键、组合输入和终端尺寸适配。修改规则和原因以 `MISSING_INCLUDES`、`SOURCE_EDITS` 为准；上游内容不符合预期时应停止审阅，不能静默丢弃补丁。

从仓库根目录可运行 `python tools/stage_ime.py --explain` 查看策略；`--verify` 核验已有暂存内容，`--verify-pins` 通过 GitHub API 核对子模块引用，`--scan-includes` 检查标准头文件需求。

term-ime 与各子模块的许可证以所固定源码内的授权文件为准，不能沿用 MixOS 根许可。发布输入法包时，应检查暂存树中的全部许可、保留声明并满足相应再分发要求。
