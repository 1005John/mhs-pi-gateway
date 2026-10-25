# MHS-Pi-Gateway — 树莓派设备网关（MHS 模型硬件标准先行实现）

树莓派上的设备控制网关：以**设备能力描述符（DCD）**纳管外设，云端 agent 经 **MCP** 远程操控，本地**规则引擎**在断网时离线自主闭环。附 ESP32-S3 LED 端到端参考实现（固件 + 串口协议 + 全套验收证据）。

## 架构

```
云端 Agent ──MCP(streamable-http :8808)──┐
本地 CLI ────────────────────────────────┤
本地规则引擎(断网自闭环)─────────────────┤
                                         ▼
                              ┌────────────────────┐
                              │   mhs-gateway 网关  │
                              │  护栏链（七道闸）    │
                              │  白名单→参数schema校验│
                              │  →急停→L2确认→限速   │
                              │  →去重互斥→前置探活  │
                              └──────┬──────┬──────┘
                          串口 MSP-1 │      │ TCP JSON 行协议
                                   ▼      ▼
                          ESP32-S3 LED   虚拟执行器
                          (/dev/ttyACM0)  (127.0.0.1:9999)
```

- **双通道控制**：云端 MCP（远程操控/审计查询/一键急停）+ 本地规则引擎（WAN 全断时同秒闭环，实测 0.2~0.6s）
- **护栏纵深**：L0/L1/L2 三级风险分级 + 禁止清单（永不自动执行）；执行态由设备自持，链路断电不停摆
- **全链路审计**：12 字段 JSONL（who/when/what/risk/decision/reason/latency），三 actor（cloud-agent / local-rule / manual）全覆盖

## 目录

| 路径 | 内容 |
|---|---|
| `gateway/` | 网关源码（描述符加载/护栏/审计/MSP-1 适配器/规则引擎/MCP server/虚拟执行器/自测） |
| `firmware/esp32s3-led-cycle/` | ESP32-S3 固件 v1.1 源码（Arduino，MSP-1 串口协议） |
| `firmware/binaries/` | 可直接烧录的 v1.1 固件 bin（16MB Flash / hwcdc 配置） |
| `docs/` | 能力规范 v0.9b、测试用例（33 条）、两份测试报告、硬件摸底 |
| `test-assets/` | 回归脚本 + BUG-001 复抓证据（审计/引擎日志/py-spy 栈三路对账） |

## 文档导航

- **[docs/](docs/README.md)** — 索引页。核心三篇：[能力规范 v0.9b](docs/MHS-网关能力规范-v0.9.md)（描述符/护栏/规则/审计/协议的唯一事实源）、[设备接入指南](docs/设备接入指南.md)（三步接新硬件，不改网关代码）、[测试报告](docs/MHS-网关-测试报告-20261024.md)
- **[gateway/README.md](gateway/README.md)** — 网关部署运维（单进程形态、CLI 全命令、急停与规则运维）
- **[firmware/esp32s3-led-cycle/README.md](firmware/esp32s3-led-cycle/README.md)** — 固件构建烧录 + MSP-1 命令速查
- **[test-assets/README.md](test-assets/README.md)** — 回归脚本用法 + BUG-001 证据档案导读

## 快速开始（网关）

```bash
cd gateway
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# 单进程形态：MCP server + 本地规则引擎同进程（护栏跨进程语义见规范 §2）
.venv/bin/python gateway.py mcp --transport http --port 8808 --rules rules.yaml
# 另一个终端起虚拟执行器（无真实硬件也能跑通全链路）
.venv/bin/python virtual_executor.py
# 自测（32 项）
.venv/bin/python selftest.py
```

MCP 端点：`http://<host>:8808/mcp`（streamable-http）

## 快速开始（固件）

- 硬件：ESP32-S3-DevKitC-1（N16R8：16MB Flash / 8MB PSRAM / 板载 RGB）
- FQBN：`esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi,USBMode=hwcdc,CDCOnBoot=cdc`
- 或直接烧 `firmware/binaries/` 下的 v1.1 产物
- 协议：MSP-1 行协议（`CMD PING` / `GET chip_temp_c` / `GET led` / `SET led mode=... color=... period_ms=...` / `RESET`），详见规范 §6

## 验收结论（2026-10-24/25）

- 33 条用例全部通过（含拔插 USB 离线拒绝/自动恢复、断网演练本地同秒闭环、护栏 8/8、审计零缺失）
- 已知遗留：并发负载下规则引擎线程间歇静默（BUG-001，38320 ticks 未复现，挂案）；串口命令间歇性首试丢失由 800ms 超时重试兜底（观察项，详见 `test-assets/evidence-20261025/dev-analysis-slow-tick-20261025.md`）

## 环境脱敏说明

文档与证据中的树莓派内网 IP / 用户名已替换为 `<PI-LAN-IP>` / `<PI-USER>`。
