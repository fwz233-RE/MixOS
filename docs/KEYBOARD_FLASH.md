# STM32 键盘刷写

`tools/flash_keyboard_on_pi.py` 是针对 STM32F042G6U6 键盘的单次部署工具，要求已审阅的 `dfu-util 0.11`。它不刷写 ESP32、不切换 USB Hub、不停止 `mixosd`，也不负责自动让正在运行的键盘进入 ROM。

## 固定目标约束

当前实现固定了板卡拓扑：键盘位于 Linux USB 路径 `5-1.1`，ESP32 位于 `5-1.2`。这些是程序的安全策略，不是所有设备的通用地址；拓扑不同需要独立审阅适配，不能仅放宽命令参数。

目标为 STM32F042G6U6，内部 Flash 32 KiB、SRAM 6 KiB，ROM DFU 标识为 `0483:df11`。工具核对 sysfs、二进制 USB 描述符、精确 ROM 序列号、设备号和内部 Flash 布局，并要求系统中目标无歧义。

VID/PID 和序列号不是密码学认证，MCU 型号还需板卡与构建来源佐证。`--mcu` 记录这一前置事实，不会探测芯片封装或证明器件真伪。

## 进入 ROM

MixOS 键盘支持上电 Tab 与本地三秒救援组合，见 [键盘实现](keyboard.md)。实际安装旧固件时行为可能不同，必须以真实 ROM 枚举为准。

已在正确 ROM DFU 中的设备不需要再次按键；仍在应用中的设备需要受支持的本地进入方式。主机不会模拟物理组合键，不操作 BOOT0，不自动执行解除读保护或共享电源循环。

## 权限与检查

无参数运行仅查看库存：

```sh
python3 tools/flash_keyboard_on_pi.py
```

完整检查／写入需要经过审阅、由 root 所有的运行器与可信目录，以隔离 Python 启动。不可为普通用户可修改的脚本授予免密码 root 执行权限。每次使用不存在的新工作目录，保留前次记录。

下面是参数示例，摘要、路径和 ROM 序列号需独立核实；未加 `--execute` 时不会写入：

```sh
sudo -n /usr/bin/python3 -I /opt/mixos-keyboard/flash_keyboard_on_pi.py \
  --image /opt/mixos-keyboard/keyboard.bin \
  --sha256 APPROVED_64_HEX_IMAGE_SHA256 \
  --serial ACTUAL_12_HEX_ROM_SERIAL \
  --usb-path 5-1.1 --mcu STM32F042G6U6 \
  --workdir /var/lib/mixos/keyboard-flash/UNIQUE_ID
```

实际刷写需明确追加 `--execute`。只有写后读回核验通过才可按需使用 `--leave` 退出 ROM；省略它会保持 ROM 状态。退出成功不是应用启动或按键可用的证明。

## 单次部署步骤

1. 验证原始 BIN、可信 SHA-256、长度、向量表及目标 RAM／Flash 范围。ELF、HEX 和 DFU 容器不能代替原始 BIN。
2. 获取键盘维护锁，独占创建工作目录和本地记录。
3. 核对 USB 描述符、串号、物理路径、设备号、DFU 能力和工具版本。
4. 保存全部 32 KiB Flash 备份并持久化，读失败时不写入。
5. 只按 1 KiB 页对齐候选实际覆盖区，记录写入意图后发送一次写入命令。
6. 完整读回 32 KiB，分别核对候选内容、页填充和未覆盖区域。
7. 保存结果；如已授权，再执行一次不携带下载文件的离开 ROM 操作。

工具对每次 dfu-util 调用使用精确选择器，不使用广泛匹配、整片擦除、选项字节写入或自动解除保护。候选覆盖范围内的数据会被替换，因此构建来源必须证明该范围由应用拥有。

## 失败与限制

断连、超时、身份变化、磁盘持久化失败或读回不一致都会停止。工具没有自动回滚、循环复位或自动重写功能；错误可能发生在部分编程之后，先保留并检查备份和原记录。

主机 fsync 不能保证故障存储介质的数据完整性，也不能阻止绕过锁的其他程序。设备需要稳定供电，并排除其他烧录器同时访问。

## 离线测试

```sh
python3 -m unittest discover -s tests -p test_flash_keyboard.py -v
```

测试覆盖选择器、镜像／向量、页边界、备份／读回、失败与离开顺序，使用模拟 Flash 和 USB 描述符。真实 ROM 可达性、应用启动、I²C 输入和本地救援仍需在设备上分别核实。

工具语义参考 [dfu-util 手册](https://dfu-util.sourceforge.net/dfu-util.1.html)。
