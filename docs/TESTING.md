# 测试与验证

本仓库的全部检查由一个命令驱动：

```
python tools/run_checks.py
```

它依次运行五个阶段，并**把跳过数和失败数同等显眼地打印出来**。这一点是刻意的：
在引入这个入口之前，"全部通过"可能意味着 83 个测试根本没有执行。

| 阶段 | 检查什么 | 需要什么 |
|---|---|---|
| `lint` | 每个 Python 文件是合法 UTF-8、能被解析、没有未使用的导入 | `pyflakes` |
| `ctest` | host 侧 C 单元测试（协议编解码、终端模型、输入映射） | `cmake`、C 编译器 |
| `python` | 全部 Python 测试 | Python 3.12 |
| `preview` | UI 预览渲染器 | `node` |
| `docs` | 每篇文档里提到的文件路径都真实存在 | 无 |

常用开关：

```
python tools/run_checks.py --quick     # 跳过需要 C 工具链的阶段
python tools/run_checks.py --strict    # 有任何跳过就以非零码退出（适合 CI）
python tools/run_checks.py --staging   # 额外核对设备上最后一次部署的包与当前源码一致
```

## 在 Windows 上运行

需要 C 工具链的测试通过 WSL 执行。测试代码**不再写死**任何发行版名或用户名，
而是自动发现，并可以用环境变量覆盖：

| 变量 | 作用 | 默认值 |
|---|---|---|
| `MIXOS_WSL_DISTRO` | 使用哪个 WSL 发行版 | `wsl --list` 的第一个 |
| `MIXOS_WSL_USER` | 发行版内的用户 | 该发行版自己的默认用户 |
| `MIXOS_HOST_CC` | 编译器名或路径 | `cc` |
| `MIXOS_PROJECT_ROOT` | 仓库根目录 | 由 `tests/_support.py` 自身位置推导 |

这些逻辑集中在 `tests/_support.py`。历史上它们散落在各个测试文件里，且写死了
一台机器的 WSL 用户名，导致整套测试只有一个人能跑。

## 可选的、需要显式开启的检查

| 环境变量 | 开启什么 |
|---|---|
| `MIXOS_CHECK_STAGING=1` | 核对最后一次上传到设备的包是否由当前工作树构建。开发期间答案通常是"否"，所以默认不跑。 |
| `MIXOS_TEST_FONT=<path>` | 用真实 MiSans 字体做字体构建集成测试 |
| `MIXOS_TEST_RENDER_FONT=<path>` | 用真实字体做渲染器测试 |

## 哪些测试会在开发机上跳过，为什么

跳过不是坏事，前提是原因清楚且不可消除。当前剩余的跳过全部属于这一类：

- **Linux 专有系统调用**：PTY、原始串口、`fcntl` 真实加锁、`SIGALRM` 定时器。
  这些是被测代码本身的运行环境要求，不是测试的缺陷。
- **ctypes 加载 ELF 共享库**（`test_cross_protocol.py`）：Windows 上的 Python
  进程无法加载 Linux `.so`。需要在 Linux 上跑。
- **上面那张表里的三个可选开关**。

如果你看到别的跳过原因，那是个问题，不是现状。

## 单独运行某一部分

```
python -m unittest discover -s tests -v                 # 全部 Python 测试
python -m unittest discover -s tests -p test_flash.py   # 单个文件
python -m pytest tests                                  # pytest 也可以
cmake -S tests -B build/host && cmake --build build/host && ctest --test-dir build/host
node tests/test_preview.cjs
```

## Host FreeType

`tests/test_ttf_render.py` 需要先构建 vendored FreeType 静态库，否则会跳过。
构建方式见 `docs/ESP_FONT_BUILD.md`。

## ESP-IDF 工具链

ESP32-S3 的构建环境由 `tools/idf_env.py` 单独描述——这是仓库里唯一一处记录
IDF 路径与工具链版本的地方。查看当前配置：

```
python tools/idf_env.py
```

构建：

```
bash tools/build_esp_local.sh          # Linux / WSL
py -3.12 tests/esp_font_build.py build # 从 Windows 驱动，内部走 WSL
```
