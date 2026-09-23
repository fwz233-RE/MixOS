# 官方底栏导航图标资源

`firmware/esp32s3/main/mix_nav_icons.h` 提供返回、主页和设置的官方 Material Symbols Rounded A8 位图。A8 表示每个像素用一个字节保存灰度覆盖率，供界面的 `icon_alpha()` 按当前颜色混合。资源随应用编译，不依赖已安装文本字体是否含有对应码点，不需要更新字体分区。

## 公开接口与尺寸

统一字号宏为 `MIX_NAV_ICON_SIZE`，值为 **60 px**。这是字体 em 字号，不是把每个图标的可见笔画强行缩放成 60×60。

- 返回：`mix_nav_arrow_back_alpha`，`MIX_NAV_ARROW_BACK_WIDTH=39`，`MIX_NAV_ARROW_BACK_HEIGHT=38`；官方 `arrow_back`，U+E5C4，原字体 glyph ID 3129。A8 大小为 1482 字节。
- 主页：`mix_nav_home_alpha`，`MIX_NAV_HOME_WIDTH=40`，`MIX_NAV_HOME_HEIGHT=45`；官方 `home`，U+E88A，原字体 glyph ID 3743。A8 大小为 1800 字节。
- 设置：`mix_nav_settings_alpha`，`MIX_NAV_SETTINGS_WIDTH=48`，`MIX_NAV_SETTINGS_HEIGHT=52`；官方 `settings`，U+E8B8，原字体 glyph ID 5937。A8 大小为 2496 字节。

三个数组都是 `static const uint8_t`，按行从上到下、从左到右存储；行跨度等于对应 WIDTH，无填充。总像素数据为 5778 字节。调用 `icon_alpha()` 时传入中心坐标、颜色、对应数组及其宽高即可；不需要新增运行时图标函数。

FreeType mask 内非零覆盖率边界框采用 `[left, top, right, bottom]`，右、下边界不包含在范围内：

- `arrow_back`：`[11, 0, 50, 38]`；相对于左侧 ascender 锚点的笔画起点为 `[11, 17]`。
- `home`：`[10, 0, 50, 45]`；笔画起点为 `[10, 14]`。
- `settings`：`[6, 0, 54, 52]`；笔画起点为 `[6, 10]`。

数组已经裁掉外围空白，使用 `icon_alpha()` 居中绘制时不再加上上述字体锚点偏移。边界框与偏移用于核验来源，不用于额外平移图标。

## 来源、固定轴与许可证

输入为现有 `build/icons/MaterialSymbolsRounded.ttf`，与游戏入口 `sports_esports` 使用同一个原始字体文件：

- 官方来源：[Google material-design-icons](https://github.com/google/material-design-icons)，commit `40a7a292a79d9394157e1ea24f83d52d5e17c556`。
- 字体版本：Material Symbols Rounded 2.969，Copyright 2026 Google LLC. All Rights Reserved.
- 字体 SHA-256：`f1472f172c0fc4a922be22972e4752ccc54fe795ed82564ab6f6b097782f2dbc`。
- 固定轴：`FILL=0`、`wght=400`、`GRAD=0`、`opsz=24`，完全沿用 `tools/build_material_icon.py` 与 `tools/build_font.py` 的策略。
- 处理顺序：固定所有变量轴，保留 hinting 后生成字形子集，将 UPEM 从 960 转换到 1000 并保持 em 比例，再用 Pillow/FreeType BASIC 以 60 px 光栅化。仅裁掉非零像素边界外的空白，不手画、重描、膨胀、阈值化、滤波或缩放位图。
- 原始字体和文本字体均只读；静态子集字体只保存在内存里。
- 上游许可为 [Apache-2.0](https://raw.githubusercontent.com/google/material-design-icons/40a7a292a79d9394157e1ea24f83d52d5e17c556/LICENSE)。许可 SHA-256 为 `58d1e17ffe5109a7ae296caafcadfdbe6a7d176f0bc4ab01e12a689b0499d8bd`。

生成的头文件保留版权、许可证链接、固定来源、机械派生说明、字体摘要、工具摘要、光栅器版本及每个字形的摘要。许可全文和 manifest 保存在 `build/icons/material-nav-icons/`。分发这些派生资源时应同时保留版权和许可证；构建目录不会自动纳入版本控制。

## 可复现生成与校验

在 MixOS 根目录使用 Windows PowerShell。记录的工具版本为 Python 3.12、fontTools 4.60.1、Pillow 11.2.1（FreeType 2.13.3）。安装依赖的命令是 `py -3.12 -m pip install fonttools==4.60.1 Pillow==11.2.1`。

现有游戏图标许可证可用于完全离线的首次生成：

`py -3.12 -B tools/build_material_nav_icons.py --license-source build/icons/material-game-icon/LICENSE.apache-2.0.txt`

如果没有现有许可副本，可使用 `py -3.12 -B tools/build_material_nav_icons.py --fetch-license`。该模式只下载固定 commit 的 LICENSE，不下载或替换字体。两个模式均检查许可摘要。

之后重新生成：`py -3.12 -B tools/build_material_nav_icons.py`。

只读、离线校验：`py -3.12 -B tools/build_material_nav_icons.py --check`。该命令重新实例化真实字体并运行 FreeType，在内存中生成所有输出，与头文件、三个 `.a8`、三个 `.png`、manifest 和许可证逐字节比较；发现缺失或漂移时失败，不自动修复文件。

生成目录为 `build/icons/material-nav-icons/`。三个原始 A8 摘要：

- `arrow_back-60.a8`：`6560881b2fed55c14d3666456b5f13ac6bac5cd55db28d9e69a85cbb1b4742de`。
- `home-60.a8`：`b8fc4edb33444759ee0e8a4308ea0e8e57eb42cb8963d9e2657365de6c58636c`。
- `settings-60.a8`：`70534bd70699e3a8621df0d4e0d939c177017e0ce21d561438ce98fc1d626e2f`。

## 测试

默认无设备测试：`py -3.12 -B -m unittest discover -s tests -p "test_material_nav_icons.py" -v`。它检查数组接口、尺寸、字节数、覆盖率、金样摘要、来源说明、固定轴一致性和输入输出别名保护。

启用真实字体重生成测试：先设置 `$env:MIXOS_TEST_MATERIAL_ICONS='1'`，再运行 `py -3.12 -B -m unittest discover -s tests -p "test_material*.py" -v`。也可以将环境变量设为同一官方字体的其他路径。

真实测试检查 cmap 字形名和 glyph ID，比较头文件、A8、PNG、manifest 和许可证，验证只读检查、损坏输出检测、时钟无关性以及原始字体、文本字体、现有游戏资源不变。测试还临时在内存中选择三个导航码点与 60 px 字号，通过未修改的游戏图标生成器逐个生成，对比两套流程的 A8 与字形边界框完全一致。

2026-09-20 本机结果：默认导航测试 7 项通过，9 项真实来源测试按设计跳过；启用真实来源后，导航 16 项与原有游戏图标 15 项共 **31 项全部通过，无跳过**。这一结果是资源与生成工具验证，不代替界面接入后的主机测试或设备验证。
