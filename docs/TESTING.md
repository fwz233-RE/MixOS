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

## 录音边界与翻译完整分页

`test_audio_capture.py` 使用合成 PCM 和子进程验证停止后读完尾块、自然退出、异常退出码、容量精确截断、独立超时升级、错误流后段诊断、取消及重复录音。POSIX 信号用例应在 Linux／WSL 执行，Windows 跳过相应项；不访问麦克风。

`test_translator_pages.py` 覆盖默认与紧凑字符格的长中文原文／译文逐页可达、完成后原文保留、归档保页、历史切换、清空、录音就绪提示及错误退出。`test_translator_streaming.py` 继续检查识别结果传递、流式翻译、合成／播放顺序和取消隔离。

`test_speech_decoder_options.py` 验证中文独立解码预算、原模型身份、其他语言不变、两模型缓存淘汰、并发单次构造、失败不缓存及服务启动前接入；使用模型替身，不下载或加载真实模型。运行 `python -m unittest discover -s tests -p test_speech_decoder_options.py -v`。

录音与分页运行入口为 `python -m unittest discover -s tests -p test_audio_capture.py -v` 和 `python -m unittest discover -s tests -p 'test_translator*.py' -v`。录音、分页与解码配置测试允许用 `MIXOS_TEST_APPS` 指向另一份应用目录，便于在设备上检查已安装文件；默认仍使用检出的 `linux/apps`。这些检查不能替代真实麦克风录音、声学清晰度及识别准确率验收。

## Host FreeType

`tests/test_ttf_render.py` 编译生产 `ttf_font.c` 的测试桩，使用真实 FreeType 栅格化；它要求 `build/host-freetype/libfreetyped.a`。仅安装 Pillow 不满足这项依赖。

按[字体渲染与测试](ESP_FONT_BUILD.md)准备 FreeType 2.14.3，再运行：

```sh
python -m unittest discover -s tests -p test_ttf_render.py -v
python -m unittest discover -s tests -p test_build_font.py -v
python -m unittest discover -s tests -p test_preview_ui.py -v
```

## 真实界面像素预览

`python tests/render_ui_feedback_r6.py --strict-layout` 直接编译生产界面、生产字体渲染器与真实 FreeType，生成首页、锁屏动画和五组设置的 18 张主机 PNG。默认开启 ASan／UBSan，并检查真实字形边界、覆盖、缺字、固定区域像素、不可见内容跳过及预热后的零字形加载。另执行五组设置 × 中英双语 × 12 主题的真实触摸连续滚动矩阵，逐帧将优化结果和模拟面板已提交像素与完整重绘比较，覆盖小幅位移、往返、大位移及动态失效。`--skip-images` 仅关闭图片写出，不跳过这些检查。详细入口、测试专用参考缓冲及与实机的区别见 [UI_FEEDBACK_REAL.md](../tests/UI_FEEDBACK_REAL.md)。该工具单独执行，不包含在普通 unittest 自动发现中。

## 锁屏触控与恢复回归

```sh
python -m unittest discover -s tests -p test_mix_local_controls.py -v
python -m unittest discover -s tests -p test_gt911.py -v
```

上述主机故障注入覆盖真实主循环触控轮询、GT911 驱动和 AW9523 复位函数，包括无 ready 帧、状态清零失败、连续错误、重新探测、复位失败清理与重试限速。`test_preview_ui.py` 的锁屏场景覆盖从屏幕底部灰显导航区／状态区上滑、正文上部起滑、触摸动画与随手势移动／回弹、正文仅标志且锁定提示仅出现于底栏、局部刷新及计时回绕、触控活动刷新息屏计时、按住时锁屏、息屏取消滑动和真实释放后重新接收手势。`test_mix_local_controls.py` 同时覆盖静止按住超过 500 毫秒没有新帧、随后继续移动的序列，空闲帧不得误取消或伪造释放。`test_preview_ui.py` 另外提供 `nav-icons`、`lock-animation` 与 `settings-budget` 场景：它们逐像素核对官方 A8 导航资源、检查锁屏动画只更新合并脏区且不触碰底栏，验证锁屏提示不重复、取消回弹和 32 位时钟回绕，并统计设置滚动的局部提交、不可见文本跳过、固定侧栏／底栏保护与通知浮层。这些测试使用硬件桩，不等同于实体触摸屏验收。

## 手势调度、卡片一致性与显示提交

`test_preview_ui.py` 当前包含 22 项检查。`settings-reversal` 验证首次拖动阈值、按住反向穿过起点及滚动边界时的 1px 跟随、拖动后不误点击及延后同步接口。`settings-budget` 验证越过初始阈值后的直接操控不额外等待 35ms，同时覆盖局部提交、不可见文本跳过、固定侧栏／底栏保护与通知浮层。源码契约禁止滚动行复用回退到 ESP32-S3 ROM 的逐字节 `memmove`。`gesture-scheduling` 在模拟 35ms 面板等待的条件下验证短间隔调度、拖动期间不拦截输入、锁屏区域像素预算，以及动画／滚动与底栏更新同帧时只有一次显示提交；`card-style` 遍历 12 主题、中英双语与按压状态，检查四张卡片使用同一字号、坐标偏移、留白和颜色，并检查“智能体／编程与工具”文案。模拟时钟结果是调度契约检查，不是 MCU 帧率测量。

```sh
python -m unittest discover -s tests -p test_mix_present.py -v
```

显示提交测试共 79 项。54 个原有场景、30 个延后同步场景和 4 个等待回调场景分别在 1000／100／128Hz RTOS tick 设置下运行，另有驱动契约检查。普通批量脏区必须先全部校验，再复制到空闲缓冲，只提交一次并等待后续帧完成，最后同步原前台缓冲；覆盖非法矩形／别名／数量、重叠区域、首次全帧要求、过期信号、超时锁定，以及与渐变取消交互。延后同步测试另外用独立完整画布逐像素检查扫描图像，非扫描缓冲仅允许最后一批区域内暂时存在旧像素；覆盖旧区域与新区域合计 16 个、交替分离区域、待修复时拒绝渐变、普通提交恢复一致和各类故障后的永久锁定。真实设置正文每次复制 964,224 字节，带底栏及其补齐帧为 1,209,984 字节，而非扩为全屏；固定侧栏保持。上述是确定性拷贝工作量，不是实际耗时或帧率。它们不替代 ESP-IDF 目标版本与真实面板的所有权／时序验证。

## 换帧等待期间的触控采样

主任务通过 `mix_present_set_wait_hook()` 在帧等待期间继续读取触控。回调只入队，不调用 UI、显示提交或触控复位；主循环按顺序分发按下、移动、真实抬起和取消事件。队列满时取消不完整手势，不伪造抬起。主循环在处理新键盘锁屏事件前先分发之前采到的触控，避免旧滑动跨越锁屏键生效。

GT911 快速读取使用零等待总线锁，最多三次 4ms 事务，总线忙则留待下次采样；无新帧仍不等于抬起。显示等待保持原来的 150ms 绝对截止时间和缓冲所有权检查，只在剩余至少 20ms 时允许采样。实际调度延迟和总线电气行为仍需实机检查。

新增主机回归覆盖：等待期间采样时 UI 不重入、短滑动和连续点击的事件顺序、错误延后恢复、队列溢出、绘图反馈中再采样、GT911 总线忙及快速事务失败、采样期间到达帧完成中断、取消回调和等待超时。在真实生产 UI 的 `wait-input` 场景中，整段滑动在无绘图期间入队，随后仍可正确解锁和滚动，拖动不会误点主题。设置滚动碰到遥测更新时仍使用延后同步，锁屏跟手反馈不再额外等待软件 35ms。主机模拟通过不代表实体屏幕帧率已验收。

参考：[ESP-IDF 5.4.2 RGB 双缓冲与 PSRAM 带宽](https://docs.espressif.com/projects/esp-idf/en/v5.4.2/esp32s3/api-reference/peripherals/lcd/rgb_lcd.html)、[LVGL 9.2 显示等待回调与局部刷新](https://docs.lvgl.io/9.2/porting/display.html)。当前 UI 为自有绘制实现，采用相同的输入/显示解耦原则，而非切换为 LVGL。

## ESP32 A/B 更新离线测试

下列用例使用本地生产 C 代码测试桩、协议模拟和文件／服务模拟，不打开设备或操作实际 systemd 服务：

```sh
python -m pytest tests/test_ota_firmware.py tests/test_ota_firmware_wire.py tests/test_link_update.py tests/test_mix_health.py -q
python -m pytest tests/test_ota_esp.py tests/test_ota_v2.py tests/test_mixos_esp_update.py tests/test_ota_supervisor.py tests/test_ota_bootstrap.py tests/test_deploy_ota.py tests/test_esp_release.py -q
```

覆盖状态机、字节协议、摘要、故障注入、受保护槽、备份／读回规则、独占锁及服务恢复等。部分用例依赖编译器、操作系统或本地证据，仍须检查跳过项。

离线 A→B→A 模拟不证明设备已经交替启动。交叉构建见[构建指南](BUILD.md)；实际 LCD、USB 重枚举、看门狗、回滚、安装权限与断电恢复需独立硬件验证和操作授权。
