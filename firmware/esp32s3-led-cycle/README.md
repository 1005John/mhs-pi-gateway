# ESP32-S3 LED 固件（MSP-1 参考实现）v1.1

网关串口协议 MSP-1 的设备端参考实现：Arduino 框架，单文件，无第三方依赖。板子为 ESP32-S3-DevKitC-1（N16R8：16MB Flash / 8MB PSRAM / 板载 WS2812 RGB）。

## 功能

- 上电默认流水灯（cycle，2000ms 全周期）
- 串口受控：MSP-1 行协议，115200 baud，硬件 CDC（USB 口即串口，无需外接 UART）

## MSP-1 命令速查

```
CMD PING               → ACK PING {"uptime_ms":...,"fw":"1.1"}
CMD GET chip_temp_c    → ACK GET chip_temp_c {"chip_temp_c":..}
CMD GET led            → ACK GET led {"mode":..,"color":..,"period_ms":..}
CMD SET led <json>     → ACK SET led {"ok":true}     json 例:
                         {"mode":"blink","color":"#ff0000","period_ms":300}
CMD RESET              → ACK RESET {"ok":true}，1s 内重启
错误                   → ERR <name> <code> <msg>
                         01 unknown_cmd / 02 bad_args / 03 busy
```

- 行以 `\n` 结尾，行最大长度受 `MSP_LINE_MAX` 约束（防溢出）
- **blink 语义**：`period_ms` = 全周期，翻相周期 = `periodMs / 2`（与描述符/规则联动口径一致）

## 构建与烧录

```bash
arduino-cli compile --fqbn "esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi,USBMode=hwcdc,CDCOnBoot=cdc" esp32s3-led-cycle
arduino-cli upload  --fqbn "esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi,USBMode=hwcdc,CDCOnBoot=cdc" -p /dev/ttyACM0 esp32s3-led-cycle
```

- `USBMode=hwcdc` + `CDCOnBoot=cdc` 是关键：固件 CDC 串口支持自动复位进下载模式，**烧录无需按 BOOT 键**
- `firmware/binaries/` 有编译好的 v1.1 产物（app + bootloader + partitions），可直接 `esptool.py write_flash` 烧录
- 硬件注意：本参考硬件存在已知 R1 问题——GPIO0(BOOT) 间歇性被拉低（BOOT 按键触点半卡），表现为上电偶发进不了应用。复位类失败先按硬件问题排查（快按 BOOT 约 10 次→按 RST→拔插 USB），不计固件 bug

## 验证

```bash
# 串口直发（115200）
CMD PING
# 期望：ACK PING {"uptime_ms":...,"fw":"1.1"}
```

完整链路验证（描述符 → 护栏 → 串口 → 回读）见仓库根 README 与 `gateway/` 文档。
