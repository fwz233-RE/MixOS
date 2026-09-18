# 字体构建

`tools/build_font.py` 将文本字体与界面所需图标合并为可放入 ESP32-S3 `font` 分区的 TrueType 子集。它只操作本地文件，不编译固件、不连接设备。所有命令从仓库根目录执行。

字体文件需自行取得，并保留授权和来源记录；见[来源说明](SOURCES.md)。依赖安装见[主机工具](HOST_TOOLS.md)，固件加载与渲染测试见[字体渲染](ESP_FONT_BUILD.md)。

## 输入与接口

使用 Python 3.12、fontTools 4.60.1、Pillow 11.2.1：

```sh
python -m pip install -r tools/requirements-host.txt
python tools/build_font.py "/path/to/MiSans-Normal.ttf" build/font/MiSans-Normal-gb2312.ttf --icons "/path/to/MaterialSymbolsRounded.ttf"
```

将示例字体路径替换为本机文件；Windows 也可使用带引号的盘符路径。构建器接受两个位置参数：只读的 `source` 和生成的 `output`。

可选参数：

- `--manifest`：清单输出路径，默认在输出文件名后追加 `.manifest.json`。
- `--ui-source`：提取字符的 UTF-8 C 文件，默认 `firmware/esp32s3/main/mix_ui.c`。
- `--icons`：提供界面私用区图标的字体；未指定时，仅在默认的 `build/icons/MaterialSymbolsRounded.ttf` 存在时自动使用它。

当前 UI 使用私用区图标，缺少图标字体或必要映射会失败。工具不会下载素材，也没有环境变量可替代这三个参数。ESP 构建报告仍按上例固定的 TTF 和清单名称读取，修改输出路径需另行适配消费者。

文本输入只接受未压缩、单面、非变量的 raw sfnt TrueType，签名 `00010000`，包含 `glyf`／`loca`；WOFF、CFF、TTC 不受支持。图标输入可为具有明确实例策略的 TrueType 变量字体。

## 字符与图标策略

- 必需集合包括可打印 ASCII、UI 字符串、源码中的 `EXTRA_TEXT` 及非法 UTF-8 的替代字符 U+FFFD。
- Latin-1 与 GB2312 的其他字符按源字体可用映射加入；缺失项写入 `omitted_optional`，不宣称完整覆盖。
- U+E000–U+F8FF 的 UI 码点由图标字体提供。只合并实际需要的图标及复合轮廓依赖，不带入图标名连字或其拉丁字符。
- Material Symbols 实例固定为 `FILL=0`、`wght=400`、`GRAD=0`、`opsz=24`，并按文本字体的 em 单位缩放。未定义策略的额外变量轴会失败。
- 源字体缺少 U+FFFD 时，可按明确记录的策略映射到 `?`。这不是标准替代字符轮廓；其他必需字符不得静默替换。

UI 提取涵盖各分支、数组、转义和行拼接后的可打印 C 字符串，排除注释。它是保守超集，不是 C 预处理器，不扩展外部宏或覆盖任意终端输出。非 ASCII 宏、额外文字来源或新字号需要审阅提取和检查范围。

## 验证与产物

构建器重新打开输出，检查 Unicode 映射，并通过 Pillow 的 FreeType 引擎栅格化必需字符。当前检查字号为 14、17、18、20、22、23、24、26、30、32、64 px；实际 Pillow／FreeType 版本写入清单。

当前布局的字体容量为 `0x400000`（4 MiB），偏移为 `0x210000`。输出仍是未填充的 TTF；验证另行检查补齐到分区长度后能否加载。超出容量直接失败，不能截断字体。

清单包含输入与 UI 摘要、图标来源、工具版本、字符覆盖、替代映射、输出长度和 SHA-256。消费者须要求 `status=verified`，核对实际 TTF 的长度与摘要，并确认 `ui_source_sha256` 对应要发布的 UI。

字体清单和应用构建报告是不同记录。`tests/esp_font_build.py` 会记录字体／UI 摘要的比较结果，报告存在本身不代表这些字段全部匹配；发布前仍须核对字体关联和授权。

## 可复现性与文件安全

TTF 的 `head.created`／`head.modified` 固定为 `2000-01-01T00:00:00Z`，使用稳定排序。固定输入、图标、UI 和工具版本时可核对重复构建结果；不同工具版本不保证字节一致。

清单保存绝对路径，因此跨目录清单可能不同；分享前检查路径信息，但不要修改用于发布校验的原始清单。Pillow 的 FreeType 引擎版本由实际安装包决定，不等同于固件的 FreeType 2.14.3。

输入、输出、清单、UI 和图标路径会检查别名、硬链接、符号链接及父子冲突。安全检查通过后会删除指定的旧输出／清单；这些路径应专供生成物使用，每个输出仅允许一个构建进程。

临时文件通过后先发布 TTF、最后发布清单；一般异常和 Ctrl-C 会清理产物。断电或强制终止不保证双文件事务，因此不能仅凭 `.ttf` 存在判断成功，也不能在构建期间并发替换路径。

## 测试

```sh
python -m unittest discover -s tests -p test_build_font.py -v
```

普通测试生成小型合成字体，不需要用户原始字体。真实 MiSans 用例需要显式开启：

```sh
MIXOS_TEST_FONT="/path/to/MiSans-Normal.ttf" python -m unittest discover -s tests -p test_build_font.py -v
```

Windows 用 `$env:MIXOS_TEST_FONT = "C:/path/to/MiSans-Normal.ttf"` 后执行测试命令。该用例固定检查 MiSans 4.003 的大小与摘要，输入要求见[来源说明](SOURCES.md)；当前 UI 的真实构建还需要默认位置的图标字体。

Pillow 包围盒诊断不等同于目标 RGB565 渲染结果。实际写入前还需核对设备分区、恢复备份、允许变化区域和读回；字体更新与应用更新分别授权，见[部署指南](DEPLOYMENT.md)。
