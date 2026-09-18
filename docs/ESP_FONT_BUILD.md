# ESP32-S3 字体渲染与测试

本页说明当前生产字体加载器、终端单元渲染及主机测试方法。字体文件生成接口见[字体构建](FONT_BUILD.md)，应用交叉构建见[构建指南](BUILD.md)。所有命令从仓库根目录执行。

## 初始化与失败处理

`firmware/esp32s3/main/main.c` 在 `mix_ui_init()` 之前调用 `ttf_font_init()`。字体失败时进入启动故障处理：待确认的 OTA 镜像复位以触发回滚流程，其他情况转入 USB 维护模式，而非忽略错误后继续正常 UI 启动。

`firmware/esp32s3/main/ttf_font.c` 查找名为 `font` 的分区，将完整区域映射到内存并交给 FreeType。加载器检查分区长度、raw sfnt 签名、可缩放轮廓和 Unicode charmap；字符覆盖及完整格式约束还需[字体构建清单](FONT_BUILD.md)验证。

初始化成功后重复调用不重新映射或分配。失败时逆序释放缓存、字体面、FreeType 库和映射；`ttf_font_deinit()` 可重复调用。字体接口为单任务设计，不能据此假定可从任意并发任务调用。

## 终端渲染

`mix_ui.c` 直接调用 `ttf_draw_cell()` 绘制 RGB565 单元，按当前几何配置分配宽度：

- 紧凑模式：80×28，单列 12×24 px，字体 20 px。
- 大字模式：64×22，单列 16×32 px，字体 26 px。
- 宽字符占两列；终端宽度规则仍由 `mix_terminal.c` 决定。

渲染以字体 ascent／descent 建立共享基线，将行高适配到单元内部。横向边界同时包含 advance、实际位图边界和粗体扩展，保留负左边距及超出自然 advance 的轮廓。

缩小时采用区域最大 alpha 采样，以保留细笔画，视觉上可能比灰度插值偏重。所有写入同时受单元与帧缓冲边界限制；缓存命中也恢复所需字号，避免其他文字改变 FreeType 当前尺寸后影响基线。

这些规则并不提供完整 Unicode 排版、组合字形、emoji 或自动字体回退。缺失码点可能由 FreeType 显示 `.notdef`，所以加载成功不能替代字符覆盖检查。

## Host FreeType

主机渲染测试使用组件锁定的 FreeType 2.14.3，与 Pillow 自带的引擎独立。先按[主机工具](HOST_TOOLS.md)准备依赖；公开检出若没有受管理组件源码，需先取得锁定版本，而非改用任意系统库。

在 Linux／WSL 中构建 Debug 静态库：

```sh
cmake -S firmware/esp32s3/managed_components/espressif__freetype/freetype -B build/host-freetype -DBUILD_SHARED_LIBS=OFF -DFT_DISABLE_ZLIB=ON -DFT_DISABLE_BZIP2=ON -DFT_DISABLE_PNG=ON -DFT_DISABLE_HARFBUZZ=ON -DFT_DISABLE_BROTLI=ON -DCMAKE_BUILD_TYPE=Debug
cmake --build build/host-freetype -j4
```

测试当前固定查找 `build/host-freetype/libfreetyped.a`，没有独立的库路径参数。若生成器或平台产物名称不同，需要审阅测试适配，不能将其他库改名后当作相同依赖。

## 运行渲染测试

使用安装了 fontTools 的 Python，及支持 AddressSanitizer／UndefinedBehaviorSanitizer 的 POSIX C 编译器：

```sh
python -m unittest discover -s tests -p test_ttf_render.py -v
python -m unittest discover -s tests -p test_preview_ui.py -v
```

Windows 可用 `py -3.12` 运行测试。渲染测试通过 `tests/_support.py` 调用 WSL 编译器，发行版、用户和编译器配置见[测试指南](TESTING.md)；UI 测试中的 Clang 检查仍使用当前系统工具。

普通渲染用例生成包含负边距、宽轮廓和超出名义下降线的合成 TTF。真实字体用例另行开启：

```sh
MIXOS_TEST_RENDER_FONT="/path/to/generated-font.ttf" python -m unittest discover -s tests -p test_ttf_render.py -v
```

Windows 先设置 `$env:MIXOS_TEST_RENDER_FONT = "C:/path/to/generated-font.ttf"`。该字体需包含测试字符；生成的 MixOS 子集通常是合适输入，任意字体未必满足轮廓断言。

## 检查内容与边界

`tests/ttf_render_harness.c` 直接编译生产 `ttf_font.c`，只模拟 ESP 的分区与分配接口，实际调用 FreeType。测试包括：

- 分区查找、映射、FreeType 初始化、字体面、charmap 和缓存分配失败，以及失败后重试。
- 损坏输入、重复初始化／释放、字形分配失败、无效字号和空白字形。
- 普通／粗体单元、部分离屏绘制、帧缓冲保护区和单元外哨兵。
- 共享基线、字号切换后的缓存行为及逐像素笔画保留。
- 启动顺序和 UI 调用路径的源码集成检查。

原生逐像素测试集中在 20 px 字体、12／24 px 宽且 24 px 高的单元，不等于穷尽所有 UI 字号或大字模式。缺失编译器、fontTools 或静态库时用例会跳过；测试结果需同时记录跳过原因。

这些测试验证生产渲染逻辑在主机上的行为，不证明实体 LCD 观感、目标 FreeType 耗时、PSRAM 压力、整机并发或回滚已通过。发布时应分别核对应用与字体来源，并按[部署指南](DEPLOYMENT.md)另行安排硬件验证。
