# MHS 网关能力规范 v0.9（影子规范 · 首版）

> 状态：**v0.9（Chief 设计稿，待 ESP32-S3 实测验证后升 1.0）**
> 日期：2026-10-24 · 作者：Chief（受用户指派，标准/协议先行，硬件最小验证）
> 定位：自定义的「MHS 对标层」。MHS（Anthropic, 2026-08-27）规范未公开，本规范按其公开思想（能力声明 + 动作分级 + 安全边界 + 模型无关）落地，公布后经 §5 映射层无缝迁移。
> 通信层：**不自造**，复用 MCP（MHS 官方接入机制之一）。

## 0. 设计四铁律

1. **薄**：只管 MHS 管的那层（能力声明/分级/护栏/审计），不管底层连线。
2. **一切是数据**：MCP tools/resources 命名与参数 schema 由描述符生成，描述符是唯一事实源。
3. **可映射**：字段保持与 MHS 公开概念一对一映射潜力，不搞深层嵌套。
4. **单点替换**：迁移时只加一个 schema 转换层；适配器、MCP 层、护栏逻辑零改动。

## 1. 设备能力描述符（DCD，Device Capability Descriptor）

每设备一份，YAML 纯数据。加载时强校验，**缺字段/非法枚举 → 拒载并报错**（对应 AC-0）。

```yaml
spec_version: "0.9"
device:
  id: esp32s3-led-01          # 全局唯一：小写字母数字连字符
  name: ESP32-S3 RGB LED 板
  category: usb-serial        # usb-serial | network | virtual
  adapter: serial-msp1        # 网关侧适配器注册名
  transport:
    serial: { port: /dev/ttyACM0, baud: 115200 }
    # network: { host: 192.168.3.x, port: 9999, proto: tcp }
  identity:                   # 可选；USB 热插拔重枚举时用于自动匹配
    usb: { vid: "303a", pid: "1001" }

resources:                    # 可读状态 = MCP resources
  - { name: chip_temp_c, type: float, unit: "°C", access: ro, poll_s: 10,
      desc: "S3 片内温度传感器" }
  - { name: led_mode,   type: enum, values: [static, blink, cycle, off], access: rw }
  - { name: led_color,  type: string, pattern: "^#[0-9a-fA-F]{6}$", access: rw }
  - { name: fw_version, type: string, access: ro }

actions:                      # 可执行动作 = MCP tools
  - name: ping
    risk: L0
    params: {}
    timeout_ms: 500
    desc: 探活，返回 uptime 与固件版本
  - name: read_temp
    risk: L0
    params: {}
    timeout_ms: 500
    rate_limit: { max_per_min: 30 }
  - name: set_led
    risk: L1
    params:
      mode:      { type: enum, values: [static, blink, cycle, off], required: true }
      color:     { type: string, pattern: "^#[0-9a-fA-F]{6}$",
                   required_if: { mode: [static, blink] } }
      period_ms: { type: int, min: 100, max: 10000, default: 2000 }
    preconditions: [ping]     # 前置：先探活成功
    timeout_ms: 800
    rate_limit: { max_per_min: 10, cooldown_ms: 500 }
  - name: reset
    risk: L2
    params: {}
    timeout_ms: 5000
    confirm: manual           # 一期：云端触发需云端会话内人工确认
    rate_limit: { max_per_min: 2, cooldown_ms: 10000 }

health:
  probe: { method: ping, interval_s: 5, miss_threshold: 3 }   # 连 miss 次 → offline

meta:
  descriptor_version: 1
  notes: "首台实机：ESP32-S3 LED 板（LED v1.0 项目遗留硬件 R1：GPIO0 间歇拉低，复位类失败先查硬件）
"
```

**字段规则**
- `resources[].access`：`ro` 只读 / `rw` 可读写；`rw` 必须有对应 action 或 GET 类命令支撑。
- `actions[].params`：仅允许 `type: enum|string|int|float|bool` + `values/pattern/min/max/required/required_if/default`，深度 ≤ 2（铁律 3）。
- `actions[].risk`：必填，取值 `L0|L1|L2`（判定见 §2）。
- MCP 映射：tool 名 = `<device.id>.<action.name>`；resource 名 = `<device.id>/<resource.name>`。

## 2. 动作分级与护栏基线

| 级 | 定义 | 基线策略 |
|---|---|---|
| **L0** | 只读（读状态、读数、探活） | 自动执行；限速 |
| **L1** | 低风险写（可控、可逆、影响单设备表现） | 白名单内自动执行 + 审计 + 限速/冷却 |
| **L2** | 高风险（断电、复位、改关键配置、不可逆） | 默认仅干跑/需人工确认；预授权白名单放行后才可自动 |

**判定规则**（写描述符时对照）：影响范围跨设备或涉及供电/固件/网络配置 → L2；单设备表现层且可随时改回 → L1；纯读 → L0。

**禁止清单**（永不自动执行，写死在网关，不接受描述符覆盖）：整机断电、恢复出厂、固件刷写、删除存储数据。

**护栏**：
- 白名单：仅描述符注册过的动作可被任何来源触发（AC-3 注入口：MCP 直调未注册动作名 → denied）。
- 参数 schema 校验（v0.9b 起）：白名单之后按描述符 DCD 的参数 schema 校验（enum / 数值范围 / string pattern / required_if 条件必填 / 未知参数拒绝），非法 → denied(reason=schema-invalid)，不触达设备；CLI、MCP、规则引擎三条执行路径统一过此校验（`confirm` 为网关控制参数不在校验范围）。
- 限速/冷却：按 `rate_limit`，超限 → denied(reason=rate-limit)，记审计。
- 急停：CLI 一键 + 云端一键；挂起全部 L1/L2，仅 L0 可用（AC-3）。
- **并发仲裁（一期语义）**：每设备动作互斥锁；同设备同一动作 100ms 内去重；云端与本地冲突时**先到先得**，后到者 denied(reason=busy) 并记审计。

## 3. 联动规则 schema 与生命周期

```yaml
rule:
  id: rule-20261024-01
  name: 高温红灯警示
  status: draft            # draft → testing → active ⇄ disabled（只允许云端改状态）
  trigger:
    resource: esp32s3-led-01/chip_temp_c
    op: ">"                # > < >= <= == !=
    value: 45
    for_s: 5               # 条件持续时长（防抖）
    retrigger_cooldown_s: 60
  actions:                 # 顺序执行
    - { device: esp32s3-led-01, action: set_led,
        params: { mode: blink, color: "#ff0000", period_ms: 500 } }
  verify:                  # 校验判据（闭环的「校验」步）
    resource: esp32s3-led-01/led_mode
    op: "=="
    value: blink
    timeout_ms: 3000
    on_fail: { notify: cloud, retry: 1 }
```

- 触发→动作→校验全程本地执行（断云可用，AC-2）；断网期间事件缓存，恢复补传。
- 复杂决策（云端大模型实时判断）不进规则引擎；云端不可用时仅执行已激活规则。

## 4. 审计事件格式

每条动作一行 JSON（本地留存 + 云端可查最近 N 天），字段即 AC-4 三要素 + 耗时：

```json
{"ts":"2026-10-24T12:40:00+08:00","event_id":"a1b2c3","actor":"local-rule|cloud-agent|manual",
 "rule_id":null,"device_id":"esp32s3-led-01","action":"set_led","params":{"mode":"blink"},
 "risk":"L1","decision":"allowed|denied|deferred","reason":"whitelist|rate-limit|busy|kill-switch|confirm-required|precondition-fail|schema-invalid",
 "result":"ok|timeout|error|denied","latency_ms":123}
```

## 5. 版本与 MHS 映射

- `spec_version` 与 `meta.descriptor_version` 双版本号；本规范变更走 changelog（文档尾注）。
- 迁移点唯一：描述符 → MHS 官方 schema 的转换层（未来新增模块，输入本规范 DCD，输出 MHS 描述）。
- 映射假设（待 MHS 公布后核对）：DCD.resources ↔ MHS 能力/遥测声明；DCD.actions.risk ↔ MHS 安全分级；审计事件 ↔ MHS 可审计要求。

## 6. MSP-1 串口控制协议（ESP32-S3 验证用）

选型理由：ASCII 行协议 + JSON 参数——人可读、易调试、与描述符参数 1:1 对齐；二进制帧留 v1.x 按需加。

- **物理层**：USB-CDC（303a:1001），115200 8N1，`\n` 结尾，单行 ≤256B。
- **启动横幅**：`MSP1 READY fw=<ver>`（网关据此识别设备上线）。
- **请求**：`CMD <name> <json-args>`；**响应**：`ACK <name> <json-result>` 或 `ERR <name> <code> <msg>`。
- **命令集**：

| 命令 | 参数 | 返回 | 风险 |
|---|---|---|---|
| `PING` | — | `{"uptime_ms":12345,"fw":"1.1"}` | L0 |
| `GET chip_temp_c` | — | `{"chip_temp_c":32.5}` | L0 |
| `GET led` | — | `{"mode":"cycle","color":null,"period_ms":2000}` | L0 |
| `SET led` | `{"mode":"static","color":"#00ff00","period_ms":2000}` | `{"ok":true}` | L1 |
| `RESET` | — | `{"ok":true}` 后 1s 内重启 | L2 |

- **错误码**：01 unknown_cmd · 02 bad_args · 03 busy · 04 internal · 05 unsupported。
- **时序**：默认响应 ≤500ms（RESET ≤1s）；网关侧超时 800ms；限速按描述符在网关侧执行，固件不做限速。
- **固件 v1.1 改造范围**：保留 v1.0 自动循环为默认 `cycle` 模式；新增 MSP-1 命令解析（硬件 CDC）；不改动 LED 时序算法与其余逻辑。

## 7. 首批描述符实例

1. `esp32s3-led-01`：见 §1（实机验证主体）。
2. `virtual-actuator-01`（虚拟网口执行器，替需求 1 的 B 侧）：

```yaml
spec_version: "0.9"
device: { id: virtual-actuator-01, name: 虚拟网口执行器, category: virtual,
          adapter: net-tcp-json, transport: { network: { host: 127.0.0.1, port: 9999, proto: tcp } } }
resources:
  - { name: output, type: enum, values: ["on", "off"], access: rw }
actions:
  - { name: ping, risk: L0, timeout_ms: 500 }   # 探活动作（health.probe 依赖；v0.9 初版遗漏，补录）
  - { name: set_output, risk: L1,
      params: { state: { type: enum, values: ["on", "off"], required: true } },
      timeout_ms: 500, rate_limit: { max_per_min: 20 } }
health: { probe: { method: ping, interval_s: 5, miss_threshold: 3 } }
meta: { descriptor_version: 1, notes: "TCP 行协议 CMD/ACK 同 MSP-1 风格，真实设备到位后换适配器" }
```

## 8. 首版验证计划（「跑通一个版本」的口径）

**范围**：DCD 加载校验 · MSP-1 串口适配器 · 虚拟执行器适配器 · MCP server（tools/resources 自动生成）· 规则引擎 + 护栏 + 审计 · ESP32 固件 v1.1（MSP-1）· 云端局域网调试。

**验收清单**：
- **AC-0 描述符**：合法描述符加载成功；缺字段/非法 risk 拒载并报错。
- **AC-1 云端调试**：MCP 客户端（局域网/SSH 隧道）列出 tools、读 chip_temp_c、执行 L1 动作（set_led），审计可查。**公网穿透不在本期**（验收按局域网口径，M4 再开）。
- **AC-2 本地闭环**：断 WAN 后规则「触发→执行→校验」全程本地完成，端到端 **≤2s**（NFR-1 定死）；恢复联网事件补传。
  **执行口径（2026-10-24 Chief 批，等价触发链路）**：一期触发源用 `virtual-actuator-01/output`——云端经 MCP 预置触发态（属调试预置，不计入闭环），随后断 WAN，本地引擎轮询检测状态变化 → set_led 红闪 → 校验 led_mode。**不做固件资源值注入口**（守住「薄」铁律，测试钩子不进生产描述符）；真实温度阈值场景留待自然触发的后续需求。
- **AC-3 护栏**：MCP 直调未注册动作 → denied+审计；限速触发 denied；急停后仅 L0 可用。
- **AC-4 审计**：任一动作可查 触发源/决策依据/结果+耗时 三要素。

**测试环境约定**：测试可 SSH `pi@<PI-LAN-IP>` 跑用例；断网演练用软件模拟（断默认路由），物理拔线需用户配合；GPIO0/复位类用例失败先按 R1 硬件问题排查，不计固件 bug。

**明确不含**：真实网口设备、公网暴露、本地小模型、多网关、固件 OTA。

## 9. 交付分工

- **Chief**：本规范（已交付）→ 复审实现符合性。
- **开发**：按 §8 范围实现；固件从 Pi 侧 esptool 烧录（USB-Serial-JTAG 自动复位，避开 GPIO0；失败按 R1 流程）。
- **测试**：按 §8 验收清单出用例并执行。
- **用户**：终验收（看效果），确认优化方向。

---
changelog：v0.9（2026-10-24）首版：DCD/分级/规则/审计/MSP-1/验证计划；v0.9a（同日）：审计 reason 枚举补 `precondition-fail`，AC-2 定等价触发执行口径；v0.9b（2026-10-25）：护栏新增参数 schema 校验阶段（BUG-002，CLI/规则引擎/MCP 三路径统一），reason 枚举补 `schema-invalid`。
