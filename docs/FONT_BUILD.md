# MixOS 字体构建与验证

## 已完成

`tools/build_font.py` 现在从实际的 `firmware/esp32s3/main/mix_ui.c` 提取全部 UTF-8 C 字符串字面量，覆盖数组、条件分支、相邻字符串、转义和行拼接；注释不会计入。ASCII、界面实际文字、额外标点、GB2312 字符集以及终端非法 UTF-8 使用的 U+FFFD 都有明确策略。必要字形缺失、空轮廓、错误格式或超出分区都会失败，并清理旧产物。

构建器还具备以下保护：

- 输入原字体只读；source、output、manifest、UI 源文件拒绝相同路径、规范化路径、父子路径、硬链接和符号链接别名。
- 使用临时文件和最后发布 manifest 的顺序；失败不留下看似合格的 TTF 或 manifest。
- 固定 `head.created` / `head.modified` 为 `2000-01-01T00:00:00Z`，并启用稳定表排序和固定配置，重复构建产生相同字节。
- 只接受未压缩的单面 raw sfnt TrueType：sfnt 签名 `00010000`，包含 `glyf`/`loca`，不接受 WOFF、CFF、TTC 或变量字体。
- 用 Pillow 11.2.1 的本地 FreeType 2.13.3 在 14–64 px 多个字号加载并栅格化所有必需字形，也验证填充到完整 4 MiB 分区长度后可加载。

## 当前产物

- 文件：`build/font/MiSans-Normal-gb2312.ttf`
- Manifest：`build/font/MiSans-Normal-gb2312.ttf.manifest.json`
- 格式：raw sfnt TrueType，`glyf`/`loca`，非 WOFF
- 分区：offset `0x210000`，容量 `0x400000`（4,194,304 bytes）
- 大小：`1,724,276` bytes；剩余 `2,470,028` bytes
- SHA-256：`859ec555afa5dfbd234fc80e7058aba243d85e3f32551f266d0457ea326d8bfc`
- 输入已核对：`D:\AI\MiSans-Normal.ttf` 为 `8,092,724` bytes，SHA-256 `1a5f4112daaa9473747c6834041646cc9b2c338cb40ab5dbb2f0161f8968ca10`
- UI 字符：279 个不同码点，中文和其他非 ASCII 字符 210 个；必需覆盖缺失为 0
- 请求码点：7,623；实际选中请求码点：7,622；输出映射：7,623（fontTools 的子集闭包额外保留 U+223C `∼`）；GB2312 请求 7,445，实际包含 7,444
- 可选缺失：U+30FB `・`，只来自 GB2312 可选集合，不是当前 `mix_ui.c` 必需字形，已在 manifest 中明确记录
- U+FFFD：MiSans 原字体没有该映射；构建器将它显式映射到 U+003F `?`，并在 manifest 说明这是非法 UTF-8 的可见降级，不伪称为标准 replacement diamond

## 测试

执行：

```text
python -m unittest discover -s tests -p test_build_font.py -v
```

普通测试使用 fontTools 临时生成小型测试 TTF，不读取用户字体。25 项普通测试通过，真实字体测试默认跳过；设置 `MIXOS_TEST_FONT=D:\AI\MiSans-Normal.ttf` 后 26 项全部通过（含真实字体重复构建、FreeType/Pillow 加载、实际 UI 覆盖、路径别名、分区边界、失败及中断清理和源字体不变验证）。真实集成测试使用两次完整构建并模拟不同生成时间，TTF 和 manifest 都逐字节相同。

## 复现构建与使用约束

在项目根目录使用安装了 fontTools / Pillow 的 Python 执行：

```text
python tools/build_font.py D:\AI\MiSans-Normal.ttf build/font/MiSans-Normal-gb2312.ttf
```

本次工具版本为 Python 3.12、fontTools 4.60.1、Pillow 11.2.1、FreeType 2.13.3。固定输入和这些工具版本时可重现 TTF hash；不同版本不保证字节一致。Manifest 保存绝对路径，所以跨目录构建的 manifest 内容不同，但 TTF 不含路径。时间字段固定，不依赖墙上时钟或 `SOURCE_DATE_EPOCH`。

每个输出路径只允许一个构建进程使用。路径别名检查失败时不触碰任何已有文件；安全检查通过后，重建会主动删除指定的旧 output / manifest，因此不要将这两个参数指向需保留的无关文件。正常异常和 Ctrl-C 会清理临时文件与最终文件。系统断电或进程被强制终止无法保证双文件事务；消费者必须同时要求 `status=verified` 的 manifest，核对实际 TTF 的大小和 SHA-256，并确认 `ui_source_sha256` 对应要发布的 UI，不能仅凭 `.ttf` 扩展名判断构建成功。本任务没有并发修改路径、没有执行刷写。

提取策略是当前 `mix_ui.c` 全部可打印字符串字面量的保守超集，包含少量头文件名/断言，故 247 条不是“247 个界面标签”。它不是 C 预处理器，不扩展外部宏（当前 `MIX_VERSION` 为 ASCII 范围），也不声称覆盖远端任意终端输出。代码检查显示外部 `mix_ui_notice()` 仅保留 ASCII，已由必需 ASCII 集合覆盖。将来若宏产生非 ASCII 文本，应纳入 UI 源或扩展提取输入；宽字符串、非法/不支持的转义或独立字面量中不完整的 UTF-8 会直接失败，不会静默漏掉。新增字号仍需更新本地主机字号检查集合。

源文件中真实 UI 必需字符及 `EXTRA_TEXT` 均保留；未为了让构建成功删掉 `✓`、`✕` 等符号。唯一合成映射是有记录的 U+FFFD → `?`。缺少的 U+30FB 没有冒充覆盖，也未擅自借用视觉相似但宽度/语义不同的中文间隔号。若将来 UI 使用它，构建将失败；需提供真实字形或制定新的明确替代策略。

## 固件调用与限制

当前 `firmware/esp32s3/main/main.c` 的 `app_main()` 没有调用 `ttf_font_init()`；虽然 `ttf_font.c` 实现了 font 分区 mmap 和 FreeType 加载，构建系统也编译了它，但仅替换字体分区不能使当前启动流程启用中文 TTF。字体文件本身足以提供当前 UI 所需字形，但还存在这个固件初始化风险；本次按范围没有编辑固件或键盘代码。

终端渲染使用 80×28、12×24 单元，CJK 按两个单元宽度保存，单元内使用 20 px TTF。主机检查发现 10 个必需字符存在潜在裁剪：`% X Y _ g j p q r y`；其中 `g j p q y` 的下界为 26 px，超过 24 px 暂存高度，`j` 左边界为 -2 px。固件按自然 advance 采样并限制单元宽度，横向超出 advance 的轮廓也可能被截掉。这属于已有的终端适配风险，不是通过换字体即可完全消除的问题；构建器已把诊断写入 manifest，主机 BASIC 布局的包围盒仅是证据，不等于目标 RGB565 渲染测试。

终端 UTF-8 状态机本身支持跨包中文并将常用 CJK 分配两列，但宽度表是有限范围判断，组合附加符号/变体选择符被丢弃，不支持完整 Unicode 字素组合、emoji、全 Unicode 字体回退或任意 shell 文本。`ttf_font.c` 对未映射码点直接调用 `FT_Load_Char`，FreeType 可能绘制 `.notdef` 而不报错，所以主机 cmap 非零校验和 manifest 的未覆盖列表是必要的。该子集不是完整 MiSans，也没有更改 PSRAM 字形缓存或 FreeType 初始化失败的资源处理。没有进行硬件、SSH、刷机或实际屏幕验收。
