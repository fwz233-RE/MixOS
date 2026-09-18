# 测试指南

测试用于验证协议、解析器、构建工具和主机可模拟的固件逻辑。所有命令从仓库根目录执行；依赖配置见[主机工具](HOST_TOOLS.md)。本页不记录某台设备的验收结果。

## 汇总入口

建议在依赖齐全的 Linux／WSL 环境运行：

```sh
python tools/run_checks.py
```

默认包含五个阶段：

- `lint`：Python 文件 UTF-8、语法和未使用导入等检查，需要 `pyflakes`。
- `ctest`：通过 CMake 编译并运行主机 C 测试，需要本机 CMake、CTest 和 C 工具链。
- `python`：用当前 Python 执行 `unittest discover`，汇总失败和跳过项。
- `preview`：运行 `tests/test_preview.cjs`，需要 Node.js。
- `docs`：检查文档中匹配特定目录和扩展名的文件引用，不是完整的 Markdown 链接校验器。

```sh
python tools/run_checks.py --quick
python tools/run_checks.py --strict
python tools/run_checks.py --staging
```

`--quick` 只移除独立的 `ctest` 阶段，Python 测试仍可能调用 C／Clang 编译器。`--strict` 在 Python 用例被跳过时返回非零；当前实现不会仅因整阶段标为 `unavailable` 而失败。持续集成还需检查阶段汇总，不能只看退出码。

`--staging` 设置 `MIXOS_CHECK_STAGING=1`，比较本地部署回执、构建产物和当前源码；它不查询设备。缺失回执可能跳过，旧包与新源码不同可能失败，均应根据本次验证目标解释。

## Windows 的执行边界

`tests/_support.py` 为采用它的测试提供 WSL 编译入口：

- `MIXOS_WSL_DISTRO`：显式选择发行版，默认取 WSL 列表中的首个名称。
- `MIXOS_WSL_USER`：选择 Linux 用户，默认使用发行版自身默认用户。
- `MIXOS_HOST_CC`：选择 POSIX 编译器，默认 `cc`。
- `MIXOS_PROJECT_ROOT`：覆盖辅助模块的仓库根目录，默认从文件位置推导。

这些设置不会把整个测试进程搬进 WSL。汇总入口的 CMake 阶段仍使用当前系统工具；部分 UI 测试直接查找本机 Clang；Windows Python 也不能加载 Linux `.so`。需要完整 POSIX 行为时，应在 Linux／WSL 内运行 Python。

## 可选输入与跳过项

- `MIXOS_TEST_FONT` 指向 MiSans 原始字体，启用重复构建与实际 UI 覆盖测试。当前用例固定检查源文件大小和摘要，并非任意 TTF 测试接口；见[字体构建](FONT_BUILD.md)。
- `MIXOS_TEST_RENDER_FONT` 指向供原生测试使用的真实字体，通常为生成的 TTF；还需 FreeType 静态库，见下节。
- `MIXOS_CHECK_STAGING=1` 开启本地发布记录一致性检查。

其他跳过原因还包括：Linux PTY／原始串口／文件锁／定时器支持、编译器缺失、链接权限、IDF 或 TinyUSB 源码缺失，以及本地恢复镜像和归档证据缺失。

公开检出可能不包含这些文件。应记录实际跳过项及原因，补齐合法依赖后重跑；不得伪造恢复文件、改掉固定摘要或把跳过写成通过。

## 单独运行

```sh
python -m unittest discover -s tests -v
python -m unittest discover -s tests -p test_flash.py -v
python -m pytest tests
cmake -S tests -B build/host
cmake --build build/host
ctest --test-dir build/host --output-on-failure
node tests/test_preview.cjs
```

`pytest` 需另外安装。主机测试可能生成临时文件和 `build` 下的测试产物，但这些结果不是 ESP 或键盘的发布构建记录。

## Host FreeType

`tests/test_ttf_render.py` 编译生产 `ttf_font.c` 的测试桩，使用真实 FreeType 栅格化；它要求 `build/host-freetype/libfreetyped.a`。仅安装 Pillow 不满足这项依赖。

按[字体渲染与测试](ESP_FONT_BUILD.md)准备 FreeType 2.14.3，再运行：

```sh
python -m unittest discover -s tests -p test_ttf_render.py -v
python -m unittest discover -s tests -p test_build_font.py -v
python -m unittest discover -s tests -p test_preview_ui.py -v
```

## ESP32 A/B 更新离线测试

下列用例使用本地生产 C 代码测试桩、协议模拟和文件／服务模拟，不打开设备或操作实际 systemd 服务：

```sh
python -m pytest tests/test_ota_firmware.py tests/test_ota_firmware_wire.py tests/test_link_update.py tests/test_mix_health.py -q
python -m pytest tests/test_ota_esp.py tests/test_ota_v2.py tests/test_mixos_esp_update.py tests/test_ota_supervisor.py tests/test_ota_bootstrap.py tests/test_deploy_ota.py tests/test_esp_release.py -q
```

覆盖状态机、字节协议、摘要、故障注入、受保护槽、备份／读回规则、独占锁及服务恢复等。部分用例依赖编译器、操作系统或本地证据，仍须检查跳过项。

离线 A→B→A 模拟不证明设备已经交替启动。交叉构建见[构建指南](BUILD.md)；实际 LCD、USB 重枚举、看门狗、回滚、安装权限与断电恢复需独立硬件验证和操作授权。
