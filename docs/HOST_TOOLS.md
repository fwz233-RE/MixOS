# 主机工具与环境

所有命令从仓库根目录执行。示例中的 `/path/to/...`、发行版、用户和设备地址应替换为自己的配置。构建入口见[构建指南](BUILD.md)，检查范围见[测试指南](TESTING.md)。

## 主机测试依赖

建议使用 Python 3.12 和隔离虚拟环境，安装 CMake、make、C 编译器、Clang 与 Node.js。部分测试直接查找 `clang`，并不使用通用编译器设置。

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r tools/requirements-host.txt
python -m pip install pyflakes pytest
```

`tools/requirements-host.txt` 固定 fontTools 4.60.1、Pillow 11.2.1。`pyflakes` 用于静态检查，`pytest` 是可选测试入口；该文件没有固定它们的版本，复现时应记录实际环境。Windows 可用 `py -3.12 -m venv .venv` 创建环境，再激活 `.venv\Scripts\Activate.ps1`。

## Windows 与 WSL

采用 `tests/_support.py` 的测试和 ESP 构建驱动支持以下覆盖项：

- `MIXOS_WSL_DISTRO`：WSL 发行版；未设置时取 `wsl.exe --list --quiet` 返回的首个名称。
- `MIXOS_WSL_USER`：发行版内用户；未设置时使用该发行版默认用户。
- `MIXOS_HOST_CC`：POSIX C 编译器名称或路径，默认 `cc`；不是带参数的整条命令。
- `MIXOS_PROJECT_ROOT`：这些辅助模块使用的仓库根目录，默认从自身位置推导，建议使用绝对路径。

```powershell
$env:MIXOS_WSL_DISTRO = "YOUR_DISTRO"
$env:MIXOS_WSL_USER = "YOUR_LINUX_USER"
$env:MIXOS_HOST_CC = "cc"
py -3.12 tools/idf_env.py
```

盘符路径按 `/mnt/<盘符>/...` 转换；自定义 WSL 挂载布局需单独适配。上述变量不是全仓库统一配置：`run_checks.py` 的 CMake 阶段在当前系统运行，部分测试直接使用本机 Clang，其他工具可能保留自己的目录和设备默认值。

## ESP-IDF 布局

`tools/idf_env.py` 描述以下本地依赖，不负责安装或下载：

- ESP-IDF 5.4.2：`.tools/esp-idf-clean`。
- IDF 工具目录：`.tools/idf-tools`。
- Python 环境：工具目录下的 `python_env/idf5.4_py3.10_env`。
- Xtensa GCC 14.2.0：`esp-14.2.0_20241119` 工具包。
- CMake 3.30.2、Ninja 1.12.1、ESP ROM ELF 20241011。

准备依赖后运行 `python tools/idf_env.py` 检查派生路径。它不检测安装完整性；工具包目录名和 Python 环境名是源码常量，没有分别覆盖各目录的命令行选项。

驱动显式设置 IDF 环境，不调用 `export.sh`，并设置 `IDF_SKIP_CHECK_SUBMODULES=1`。这只跳过 IDF 的子模块检查，不会补齐缺失内容；新环境仍须准备目标构建所需文件和 Python 依赖。

Linux／WSL 脚本 `tools/build_esp_local.sh` 可用 `MIXOS_PYTHON` 指定读取环境配置的 Python。该脚本的 `--clean` 会删除 ESP 默认构建目录；它不保存恢复文件，也不生成发布来源记录。正式构建前提和记录流程见[构建指南](BUILD.md)。

## QMK 与 ARM 工具链

使用 QMK 0.28.0，完整 commit 见[来源说明](SOURCES.md)。默认目录为 `.tools/qmk-0.28.0`，可由 `tools/build_keyboard.py --qmk-root /path/to/qmk` 更换；显式指定时使用绝对路径。

准备固定版本的 ChibiOS、ChibiOS-Contrib、printf、LUFA 子模块，以及 QMK 的 Python 依赖、make、ARM newlib 和 `arm-none-eabi` 工具。参考工具版本为 ARM GCC 10.3.1（Ubuntu 包 `15:10.3-2021.07-4`）、binutils 2.38；驱动记录实际版本，但不强制这些编译器版本。

QMK 使用独立虚拟环境并在 Linux／WSL 中执行。完整构建还需本机 C 编译器；这一驱动使用 `CC`，而非 `MIXOS_HOST_CC`。`--jobs` 控制并行数，默认 4。驱动会替换指定 QMK 目录中的目标键盘副本，应使用专门构建工作区。

## 字体与渲染依赖

源字体和图标字体需要单独取得并核对授权；安装 Python 包不会自动获得字体。构建器支持 `--icons` 指定图标字体，默认查找 `build/icons/MaterialSymbolsRounded.ttf`。

`MIXOS_TEST_FONT` 开启固定 MiSans 输入的集成测试；`MIXOS_TEST_RENDER_FONT` 开启真实字体渲染测试。它们不替代字体构建的输入参数。接口、固定输入限制和原生 FreeType 依赖见[字体构建](FONT_BUILD.md)及[渲染测试](ESP_FONT_BUILD.md)。

## 设备侧工具

这些工具与离线检查分开使用，执行前先查看各自 `--help`，核对 `--host`、`--user`、远端目录和操作权限：

- `tools/inventory_pi.py` 通过 SSH 查询设备；默认还写本地文档，`--print-only` 可避免这一步，但仍连接设备。
- `tools/stage_models.py`、`tools/stage_speech.py` 在本机下载模型；对应的 `deploy_models.py`、`deploy_speech.py` 负责传输和校验。
- `tools/deploy_apps.py --print-script` 离线显示安装脚本；`--check-only` 连接设备检查现状；安装和启用服务需单独授权。
- `tools/usb_gadget.py` 涉及 USB Host／Gadget 模式；切换可能影响内部屏幕、键盘和设备访问，不能当作普通网络加速选项。

SSH 工具使用 OpenSSH 主机密钥校验，部分支持 `MIXOS_SSH_PASSWORD` 和临时 askpass。保持主机身份校验，不把凭据写入文档或发布清单。

部分部署脚本仍绑定远端账户目录、服务安装位置或设备默认地址，不能仅替换 `--host` 就视为通用安装器。实际使用见[部署指南](DEPLOYMENT.md)和[应用说明](AI_DECK.md)；本页不提供自动刷写流程。
