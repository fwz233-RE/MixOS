# AI 应用与后端配置

MixOS 由 ESP32-S3 绘制本地界面，Compute Module 5（CM5）运行 Linux 终端应用和 AI 后端。
应用通过 USB 传回终端文本，CM5 不接管屏幕，也不向固件传送桌面画面。
本文介绍仓库中的实现与配置入口；模型、输入法和设备端依赖需要另行部署。

## 功能范围

- **实时翻译**：录音、语音识别、文本翻译和语音合成，界面位于 [translator/app.py](../linux/apps/translator/app.py)。
- **笔记**：本地文本编辑、保存和语音转文字，界面位于 [notes/app.py](../linux/apps/notes/app.py)。
- **Agent 入口**：目前仅显示占位页，没有接入自动执行任务的 AI 功能。
- **设置**：由 ESP32-S3 绘制；Wi-Fi 操作通过 Linux 侧 NetworkManager 完成。
- 普通终端与手动编辑笔记不要求 AI 模型；语音与翻译功能依赖对应服务。

“实时翻译”采用分段录音后处理，并非持续监听或边说边转写。
默认界面提供中文与英文；菜单中出现一种语言，不等于设备已经安装其模型。

## 组件与数据流

1. ESP32-S3 发送结构化的应用启动请求。
2. [mixosd.py](../linux/mixosd.py) 将允许的应用名映射到固定启动程序，并创建伪终端（PTY）。
3. [launchers](../linux/launchers/) 启动 Linux 文本界面；输出由固件的终端解析器绘制。
4. 麦克风和扬声器通过 USB Audio Class（UAC）作为 Linux 声卡使用。
5. 语音请求进入本地语音服务；翻译请求直接进入语言模型服务。

应用白名单为 `shell`、`translate`、`notes`、`agent`。
启动请求不能提供任意程序路径或附加命令行；普通 shell 会话仍可执行用户输入的命令。
终端输出与控制请求分开传输，协议见 [USB_V1.md](../protocol/USB_V1.md)。

每条连接同一时间只保留一个应用终端会话，切换应用或离开应用页面会关闭旧会话。
关闭时先发送 `SIGHUP`，给应用清理录音、播放和保存笔记的机会，再处理未退出的进程。
这不是多应用后台运行机制，也不替代用户对重要笔记的备份。

## 终端界面与输入

[终端解析器](../firmware/esp32s3/main/mix_terminal.c) 支持备用屏幕、滚动、光标操作和扩展颜色。
[共享界面工具](../linux/apps/tui.py) 按字符格绘制、处理中文宽字符，只发送变化部分，减少 USB 流量。
固件提供默认大字 `64×20` 与紧凑 `80×27` 两档，正文位于底部 120 像素状态导航栏之上；切换会清空终端网格与滚动历史。
应用读取实际终端尺寸；输入法占用候选栏时，子应用可用高度会少一行。

- 翻译界面：空格开始或停止录音，`Tab` 换向，`l` / `L` 切换两侧语言，`Esc` 中止，`Q` 退出。
- 翻译原文与译文使用固定分页；默认显示最新一条的原文开头，左右方向键或 `PageUp` / `PageDown` 翻页，到达条目边界后可查看前后条目。译文增长及识别完成归档不会自动挤掉原文或跳到末尾，底部显示条目数、页码与翻页提示。
- 开启麦克风期间明确显示等待状态，收到首批录音数据后才显示“录音中”；录音意外结束或达到时长上限时，翻译界面自动退出录音状态并检查错误，不继续误示正在监听。
- 笔记列表：`Ctrl+N` 新建，`Ctrl+D` 删除，`Ctrl+Q` 退出。
- 笔记编辑：`Ctrl+S` 保存，`Ctrl+R` 开始或停止录音，`Ctrl+T` 切换识别语言，`Ctrl+Q` 返回。
- 录音采用再次按键停止，而非松开按键停止；单段录音上限为 120 秒。

笔记以 UTF-8 `.md` 文件存储，默认目录遵循 `XDG_DATA_HOME`，否则使用 `~/.local/share/mixos/notes`。
`MIXOS_NOTES_DIR` 可覆盖目录；写入采用同目录临时文件、同步文件内容后替换目标文件。
存储逻辑见 [notes/store.py](../linux/apps/notes/store.py)。

## 依赖分层

- Linux 文本界面使用系统 Python 与标准库，不需要在界面进程中加载模型框架。
- 录音、播放需要 `alsa-utils` 提供的 `arecord`、`aplay`，以及声卡访问权限。
- AI 服务使用独立 Python 虚拟环境，核心依赖包括 `numpy`、`moonshine_voice` 和 `litert-lm`。
- 完整锁定依赖见 [requirements.txt](../linux/apps/translator/vendor/requirements.txt)，需核对目标 Python 版本和 CPU 架构。
- 拼音输入依赖可选的 `term-ime` 与 Rime 数据；缺少它们时笔记启动器直接运行编辑器，不提供该拼音输入层。
- Wi-Fi 设置另需 NetworkManager、`nmcli` 和适用的授权；参见 [网络授权规则](../linux/50-mixos-network.rules)。

第三方语音后端保持原始副本，项目适配集中在 [translator/service.py](../linux/apps/translator/service.py)。
增加适配时应优先修改项目包装层，而非直接修改第三方副本。

## 两个本地 AI 服务

[语音服务](../linux/mixos-aiserver.service) 默认使用 `127.0.0.1:3000`：

- `POST /api/stt` 接收 Base64 编码的 16 kHz、单声道、小端 float32 音频与语言代码，返回识别文本。
- `GET /api/tts` 接收文本与语言，返回完整 WAV 音频。
- 包装层默认不预加载语音模型；`--prewarm LANG` 可将加载提前到启动阶段，但会增加启动时资源占用。
- `--check` 仅检查关键 Python 包是否可发现，不检验模型文件、音频设备或完整推理链路。

[语言模型服务](../linux/mixos-litertlm.service) 由 `litert-lm serve` 提供，客户端默认连接 `127.0.0.1:9379`。
客户端使用兼容 OpenAI 的聊天接口，并通过服务器发送事件接收流式文本。
直接访问模型服务是为了保留流式输出：第三方语音后端的 `/proxy` 会先读取完整响应。

默认服务单元设置离线环境变量，并以 IP 访问规则限制为本机通信。
离线运行的前提是所有依赖和模型均已就位；缺失资源不能依靠首次请求自动下载来补齐。
修改客户端地址不会改变服务监听地址或安全限制；远端模型不是开箱即用的部署方式。

## 配置入口

[backend.py](../linux/apps/backend.py) 在导入时读取以下环境变量：

- `MIXOS_SPEECH_HOST` / `MIXOS_SPEECH_PORT`：语音服务地址，默认 `127.0.0.1` / `3000`。
- `MIXOS_MODEL_HOST` / `MIXOS_MODEL_PORT`：模型服务地址，默认 `127.0.0.1` / `9379`。
- `MIXOS_MODEL_PATH`：聊天路径，默认 `/v1/chat/completions`。
- `MIXOS_MODEL_LIST_PATH`：模型列表路径，默认 `/v1/models`。
- `MIXOS_MODEL_NAME`：模型标识；未指定时尝试查询列表，查询不到则使用 `default`。
- `MIXOS_STT_TIMEOUT` / `MIXOS_TTS_TIMEOUT`：默认各 180 秒；`MIXOS_MODEL_TIMEOUT` 默认 300 秒。

这些是应用客户端配置，应进入启动应用的进程环境，例如 `mixosd` 的服务环境；修改后需重启相关进程。
模型标识应与实际服务匹配；自动查询不是任意模型都能运行的保证。
启动器另支持 `MIXOS_APP_LIB`、`MIXOS_PYTHON`，用于调整应用位置和解释器。

音频配置位于 [audio.py](../linux/apps/audio.py)：

- `MIXOS_AUDIO_DEVICE` 默认 `plughw:CARD=UACCDC,DEV=0`，用声卡名称避免设备编号变化。
- `MIXOS_VOICE_CHANNEL` 取 `0` 或 `1`，默认保留左声道；它选择声道，不是混音或自动择优。
- ALSA 负责采样率转换，应用从双声道采集中抽取一路，再转换为识别服务所需格式。
- 无信号、音量过低与识别为空分别处理；阈值与硬件增益有关，参见 [音频硬件说明](AUDIO_HARDWARE_NOTES.md)。

## 语言与模型资源

[stage_speech.py](../tools/stage_speech.py) 当前声明中文、英文的识别和合成资源。
英文识别使用 `small-streaming-en`，中文使用 `base-zh`；包装层的模型选择需与部署资源一致。

中文识别器通过 `service.py` 的 `STT_OPTIONS` 单独设置 `max_tokens_per_second=16`。这里的 token 是模型的子词单元，不等于一个汉字；默认输出预算在设备对照试验中会让中文句子尚未解码完就停止。同一合成音频在旧服务下缺少“步吧”“票”“家做饭”等结尾，加前后静音无改善；配置 16 后恢复结尾，包括 1.25 倍速样本。保持原模型、分段参数、英文配置和两模型缓存上限，第三方副本不修改。不使用翻译模型补写识别原文，也不靠后处理猜测缺失文字。

- `MIXOS_TRANSLATE_LANGUAGES` 用逗号分隔翻译界面的语言列表，默认 `zh,en`。
- `MIXOS_TRANSLATE_FROM` / `MIXOS_TRANSLATE_TO` 设置初始方向，默认 `zh` / `en`。
- `MIXOS_NOTES_LANG` 只选择笔记的默认识别语言；当前轮换列表仍固定为中文、英文。
- 新增语言需要同步准备识别、合成资产及后端映射；单独设置环境变量不会安装模型。

语言模型资源由 [stage_models.py](../tools/stage_models.py) 的清单声明，下载端点可通过 `--endpoint` 或 `HF_ENDPOINT` 指定。
模型许可、下载来源和运行时兼容性应在部署前核对，不能由文件下载成功推断推理可用。

## 2026-09-22 中文识别漏字修复部署

已备份并安装 `audio.py`、`translator/app.py`、`translator/service.py`，未重刷 ESP32-S3、修改屏幕频率、覆盖模型文件或用户笔记。录音／界面备份在 `/home/pi/mixos-speech-words-20260922/backup`，语音服务包装层备份为该目录同级 `decoder-fix/service-before.py`。

用户重启设备后，正式 `mixos-aiserver.service` 于 23:06:36 CST 启动，在线核对三文件 SHA-256 一致、设备通信／语音／语言模型服务均 active。主机相关 207 项测试通过；设备直接导入已安装文件运行 29 项针对性测试通过。正式 HTTP 识别服务完成四组中文（含加速样本）、一组英文及静音验证，预期句尾均保留，静音返回空文本。证据保存在 `build/deploy/speech-words-20260922/verify_installed-result.json`；实际语音均由固定测试文本合成，没有采集用户麦克风或使用扬声器。

此验证证明配置已在正式服务生效和指定样本的输出截断被修复，不证明真实麦克风的所有漏字原因已经消除。测试仍观察到“明天／名天”“超市／潮社”等模型识别错误；提高解码输出预算解决截断，不能保证噪声、口音、同音字或所有长句的识别准确率。

## 可选中文输入法

[笔记启动器](../linux/launchers/notes) 通过独立配置启动 `term-ime`，将其子程序固定为笔记编辑器。
`MIXOS_TERM_IME` 和 `MIXOS_RIME_DATA` 分别覆盖可执行文件及 Rime 数据目录。
启动器要求输入法、编辑器可执行文件和数据目录均存在，否则退回直接运行编辑器。

[stage_ime.py](../tools/stage_ime.py) 准备固定版本源码及项目集成修正，
包括关闭软件流控、转发拼音组合中的控制键、同步子终端尺寸。
组合中按控制键会取消未提交拼音，再将命令交给编辑器；语音识别结果直接进入编辑器。
[build_ime_remote.py](../tools/build_ime_remote.py) 提供远端构建入口；安装输入法不等于中文输入已完成端到端验证。

## 部署与检查

部署工具按“先准备资源，再传输安装”组织：

1. [stage_wheels.py](../tools/stage_wheels.py) 与 [deploy_venv.py](../tools/deploy_venv.py) 准备服务虚拟环境。
2. [stage_models.py](../tools/stage_models.py) 与 [deploy_models.py](../tools/deploy_models.py) 准备并导入语言模型。
3. [stage_speech.py](../tools/stage_speech.py) 与 [deploy_speech.py](../tools/deploy_speech.py) 准备语音资源。
4. [deploy_apps.py](../tools/deploy_apps.py) 安装界面、启动器与服务单元；`--enable` 才启用两个 AI 服务。

工具含默认主机、用户和安装路径。执行前应阅读 `--help` 与安装脚本，并按目标设备核对配置。
远端部署工具使用 OpenSSH 密码认证；未设置 `MIXOS_SSH_PASSWORD` 时会交互询问密码。
当前服务模板和部分部署路径以 `pi` 用户为前提，单独修改 `--user` 不能完成全部路径迁移。
默认应用部署会更新并重启 `mixosd`，中断当前终端；`--no-daemon` 可跳过该部分。
输入法远端构建可能安装构建依赖，并临时停止 AI 服务；应安排维护窗口。

以下在仓库根目录执行，只展示脚本或检查安装状态；`device.example` 是必须替换的设备地址：

```sh
python3 tools/deploy_apps.py --print-script
python3 tools/deploy_apps.py --host device.example --user pi --check-only
```

依赖检查应使用 AI 服务虚拟环境中的 Python，在仓库根目录执行 `python3 linux/apps/translator/service.py --check`。
该检查不启动服务，也不证明识别、合成或翻译已经可用。

[设备库存工具](../tools/inventory_pi.py) 对远端执行只读查询，但默认会将实际设备摘要追加或更新到 `docs/AI_DECK.md`。
推荐使用 `--print-only`，并显式指定目标设备的 `--host`：该选项仅打印摘要、不改文档，仍会在本地 `build/inventory` 写入原始 JSON。
原始 JSON、打印摘要及相关日志可能包含主机、网络和设备信息，应留作本地诊断材料，勿公开提交。

## 资源与运行限制

- 模型权重、语音缓存和运行时缓存都需要存储空间；语言模型缓存目录须符合服务单元的可写路径设置。
- 首次请求可能加载模型或建立缓存，耗时取决于模型、存储和设备资源，不承诺固定响应时间。
- 当前服务单元未设置 `MemoryMax`，使用 `Restart=on-failure` 与 `OOMPolicy=continue`；这不保证任意内存容量均足够。
- 更换模型、增添语言或同时编译程序前，应重新评估可用内存、交换空间及磁盘容量。
- 应分别验证终端连接、录音播放、识别、翻译、合成和中文输入；服务处于运行状态不能替代这些检查。
