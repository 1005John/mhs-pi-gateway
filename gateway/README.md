# MHS 网关（影子规范 v0.9 实现）

树莓派侧的 MHS 网关原型：把外设（当前 1 台 ESP32-S3 LED 板 + 1 个虚拟执行器）
纳管为 MCP tools/resources，带分级护栏（L0/L1/L2）、审计、限速、急停、断云本地规则闭环。

规范物见《MHS-网关能力规范-v0.9.md》；本文只讲怎么跑。

## 结构

```
mhs-gateway/
├── gateway.py            # 编排 + CLI 入口（serve-virtual / exec / read / killswitch / audit / health / rules-run / mcp）
├── dcd_loader.py         # DCD 描述符加载校验（AC-0）
├── guards.py             # 护栏：白名单/禁止清单/急停/L2 confirm/限速/去重/互斥（check_pre + acquire 两段式）
├── audit.py              # 审计 JSONL（§4 十二字段），断云 outbox 缓存留桩
├── adapters.py           # 设备适配器：SerialMSP1Adapter（/dev/ttyACM0）、NetTcpJsonAdapter
├── virtual_executor.py   # 虚拟执行器（TCP 127.0.0.1:9999，MSP1 行协议）
├── rules.py              # 本地规则引擎（draft/testing/active/disabled，断云闭环）
├── mcp_server.py         # MCP server：tools/resources 由描述符生成，模型无关
├── selftest.py           # 本地自测（32 项，不含真实硬件）
├── rules.yaml            # 本地规则（含 AC-2 等价触发规则）
└── descriptors/          # 设备能力描述符（DCD，唯一事实源）
    ├── esp32s3-led-01.yaml
    └── virtual-actuator-01.yaml
```

## 快速开始（Mac 本机自测）

```bash
python3 -m venv .venv-mhs && .venv-mhs/bin/pip install -r mhs-gateway/requirements.txt
.venv-mhs/bin/python mhs-gateway/selftest.py          # 32 项全绿
.venv-mhs/bin/python mhs-gateway/mcp_smoke.py         # MCP stdio 冒烟（自动起虚拟执行器）
```

## 树莓派上跑（M3 部署形态）

**单进程形态（推荐）**：MCP server 与规则引擎同进程，护栏（互斥/去重/限速/急停）
天然共享，`/dev/ttyACM0` 只开一次：

```bash
# 虚拟执行器（后台）
python3 mhs-gateway/virtual_executor.py --port 9999 &

# 网关 MCP + 本地规则引擎（--rules 同进程起引擎线程，热重载 rules.yaml / overlay）
python3 mhs-gateway/gateway.py mcp --transport http --port 8808 --rules mhs-gateway/rules.yaml
```

> 不要再单独跑 `rules-run` 常驻进程：两个进程会各自持有一份护栏状态、并且
> 串口双开会互吃字节流。`rules-run` 仅留作调试（`--once` 单步评估）。

```bash
# 真实 ESP32-S3（/dev/ttyACM0，需固件 v1.1=MSP-1）
python3 mhs-gateway/gateway.py health                 # 探活
python3 mhs-gateway/gateway.py exec esp32s3-led-01 set_led --params '{"mode":"blink","color":"#ff0000","period_ms":500}'
python3 mhs-gateway/gateway.py read esp32s3-led-01 led_mode

# 护栏 / 审计 / 规则
python3 mhs-gateway/gateway.py killswitch on          # 急停：仅 L0 可用，状态跨重启
python3 mhs-gateway/gateway.py audit --tail 10        # 审计 JSONL（§4 字段）
```

## 约定

- **tool 名** `<device.id>.<action.name>`，**resource URI** `mhs://<device.id>/<name>`，均由描述符生成。
- **管理工具/资源**（前缀 `gateway.*`，审计 actor=cloud-agent / device_id=gateway / risk=L1）：
  - `gateway.kill_switch`（tool，state: on/off）——急停（FR-6.3），写 `.killswitch`，常驻进程 0.5s 内生效
  - `gateway.rule_set_status`（tool，rule_id + status）——改规则生命周期（FR-6.2），状态写 `<rules>.status.json` overlay 文件热生效，不动 rules.yaml
  - `mhs://gateway/audit/recent`（resource）——审计最近 50 条，云端核查入口（AC-4）
- **L2 动作**必须带 `confirm: true`（MCP 形参），否则 deferred/confirm-required。
- **blink/cycle 的 period_ms 语义 = 全周期**（亮灭各半，翻转间隔 = period_ms/2），描述符、规则与固件三处一致。
- 审计落 `mhs-gateway/audit.jsonl`；急停状态落 `mhs-gateway/.killswitch`；规则状态 overlay 落 `mhs-gateway/rules.status.json`。
- 断云：本地规则照常触发执行，事件进 outbox，网络恢复补传（`audit.py` 预留接口）。
