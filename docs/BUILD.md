# 构建指南

本页说明主机检查、ESP32-S3 应用和 STM32 键盘的构建入口。所有命令从仓库根目录执行；示例路径需按环境替换。依赖准备见[主机工具](HOST_TOOLS.md)，版本与授权见[来源说明](SOURCES.md)。

构建和打包只生成本地产物，设备写入按[部署指南](DEPLOYMENT.md)单独进行。主机测试通过不能证明硬件可用。

## 主机检查

在配置好 Python、C 编译器、CMake 和 Node.js 的 Linux／WSL 环境中运行：

```sh
python tools/run_checks.py
```

该入口汇总静态检查、C/Python 测试、界面预览和文档路径检查。Windows、可选字体及跳过项的限制见[测试指南](TESTING.md)。

## ESP32-S3 构建前提

- 使用 ESP-IDF 5.4.2、ESP32-S3 目标及仓库锁定的组件版本；具体工具目录由 `tools/idf_env.py` 定义。
- 核对 `firmware/esp32s3/main/idf_component.yml`、`firmware/esp32s3/dependencies.lock` 和实际生成配置。
- 显示仅保留 `1024×768`、RGB565 双缓冲、26 MHz 像素时钟和 16 行 bounce 缓冲的稳定路径；高刷与 RGB332 实验实现及配置入口已删除，CPU、PSRAM 和看门狗配置保持不变。
- `firmware/esp32s3/codec_overlay.cmake` 将 ES8389 编译单元替换为本地覆盖实现；应检查解析出的完整组件和覆盖结果，而非直接修改受管理依赖。
- 准备[字体构建](FONT_BUILD.md)生成的 TTF 和 manifest，驱动按固定名称读取 `build/font/MiSans-Normal-gb2312.ttf` 及其清单。
- `tests/esp_font_build.py` 仍绑定已知恢复应用和先前候选的大小、摘要。它要求 `build/esp32s3` 内有匹配的保留副本，或能从现有产物严格核验并保存。

**新的公开检出不具备这些恢复文件时，发布构建驱动会拒绝继续。** 当前没有命令行参数可为任意设备创建新基线；应另行审阅恢复基线和工具适配，不能删除摘要检查或伪造清单来通过。

## 应用构建与记录

满足上述前提后，Windows 使用：

```powershell
py -3.12 tests/esp_font_build.py build
```

Linux／WSL 可将 `py -3.12` 换为配置好的 `python`。驱动先核验恢复副本，再调用本地 ESP-IDF，并生成：

- `font-app-build.log`：构建输出。
- `completed-build.json`：编译前后保持一致的输入及应用、ELF、分区表、生成配置的摘要。
- `font-app-build.json`：应用大小、分区容量、镜像校验、链接的字体符号、来源与字体关联信息。

这些记录默认位于 `build/esp32s3`。驱动从本次构建的分区表读取应用容量，不用历史槽大小代替。

可成对指定隔离的编译和报告目录，两者不得互相重叠，也不得覆盖默认受保护目录：

```powershell
py -3.12 tests/esp_font_build.py build --build-dir build/esp-candidate --report-dir build/esp-candidate-report
```

隔离输出仍依赖原恢复副本及固定字体路径。对于已经配置在隔离目录内的实验 `sdkconfig`，构建与打包都要显式传入 `--isolated-config`；构建器会直接使用并绑定 `--build-dir/sdkconfig`，不会把默认稳定配置当成实验配置的来源。缺少该文件会拒绝构建，编译期间文件变化也会拒绝生成证明。该选项必须与隔离的编译、报告目录同时使用，不修改项目默认 `sdkconfig`。

`preserve` 只保存／核验既有镜像；`report` 只复核稳定构建记录，不能为旧二进制补造当前源码来源。

`bash tools/build_esp_local.sh` 可用于 Linux／WSL 编译排错，但它与裸 `idf.py build` 都不会生成上述发布记录。发布前须通过构建驱动重新构建并核验；使用跳过构建检查的选项不能修复来源不一致。

ESP-IDF 的应用版本可能来自 Git 描述，提交变化会影响后续构建产物。文档修改、提交或本地测试均不能替代重新构建后的来源核验。

## 本地发布包检查

`tools/esp_release.py` 检查 `main` 顶层 C/H 文件与构建声明的完整集合、本地组件输入、指定项目输入、产物摘要和实际安全配置，并要求匹配的受保护恢复应用。它证明这些记录一致，不代表全部依赖已做独立供应链审计或设备已通过验证。

```sh
python tools/esp_release.py --output build/releases/example-release
python tools/mixos_esp_update.py inspect --package build/releases/example-release
python tools/mixos_esp_update.py apply --package build/releases/example-release --dry-run
```

输出目录必须不存在；使用隔离构建时，打包也需提供相同的 `--build-dir` 和 `--report-dir`。上述命令不连接设备。

源码、配置或产物变化后应重新构建，保留 source/hash 校验。解除恢复槽保护属于独立授权，详见 [A/B 更新说明](ESP_OTA_V2.md)。

## STM32／QMK

在 Linux／WSL 的 QMK 环境中使用固定版本和已初始化的子模块：

```sh
python tools/build_keyboard.py --jobs 4
```

驱动复制 MixOS 键盘源码到指定 QMK 工作区，编译两个 keymap，审计 Flash/RAM、校验原始镜像并运行源码验证和测试；产物在 `build/keyboard`。它会更新 `firmware/keyboard/QMK_PIN.json` 中的验证状态，因此不能当作只读检查。

`--build-only` 仅供编译排错，不建立完整验证状态。普通 USB 键盘报告保持禁用，本地 Bootmagic／救援组合必须独立于 ESP 和 Linux；详见[键盘说明](keyboard.md)。

## 验证边界

字体文件、固件编译和主机测试分别验证不同内容。LCD 时序、PSRAM、GPIO 复用、音频、USB、看门狗、回滚及断电恢复仍需分阶段实机验证，不能沿用其他设备的通过记录。
