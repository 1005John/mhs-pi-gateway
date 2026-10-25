# MHS 网关首版验证 — 测试用例 v0.9

> 依据：《MHS-网关能力规范-v0.9.md》§8 验收清单（AC-0~AC-4）。
> 测试执行人：测试 Bot。被测物：`mhs-gateway/` + `esp32s3-led-cycle/` v1.1 固件。
> 版本：v0.9.1 —— 已对照 mhs-gateway 实现（guards/audit/gateway/mcp_server/dcd_loader/rules）逐条核对口径，2026-10-24。
> 用例总数 **33**：前置 3 / AC-0×5 / AC-1×7 / AC-2×6 / AC-3×8 / AC-4×4。

---

## 0. 通用约定

- **结果口径**：PASS / FAIL（正式 bug，按模板报开发）/ BLOCKED（被依赖卡住）/ 豁免-R1（硬件问题，见下）。
- **时延测量**：以审计事件/日志时间戳差为准（触发条件满足时刻 → verify 通过时刻），测试脚本同时记录本地时钟差做交叉核对。
- **R1 硬件豁免规则**（GPIO0 间歇拉低，已知硬件问题）：适用于 `reset`、烧录、任何导致芯片复位的用例。失败时先按以下顺序恢复：① 串口/lsusb 确认 303a:1001 是否仍枚举、/dev/ttyACM0 是否在位；② 快按 BOOT 键约 10 次活动触点 → 按 RST（可多试几次）→ 拔插 USB。**能恢复 = 该次失败记「豁免-R1」，恢复后重跑该用例一次：重跑 PASS 计 PASS（备注豁免）；重跑仍 FAIL 才记正式 bug。** 证据要求：记录 lsusb 与串口横幅（`MSP1 READY fw=…`）状态。
- **AC-2 断网模拟口径**：`sudo ip route del default` + **同时断开云端 MCP 会话**（双条件）。原因：一期云端客户端在同局域网，仅删默认路由证明不了「云端不可达」。恢复 = 路由加回 + MCP 重连。
- **实现口径备注（已对照代码确认）**：
  - 部署形态 = 3 个常驻进程：虚拟执行器(:9999) / 网关 MCP(:8808) / 规则引擎 `rules-run`（MCP 进程不含规则引擎）。
  - **急停、限速、去重、互斥锁均为进程内存态**，仅急停落盘 `.killswitch` 且只在进程启动时读取 → 跨进程不实时同步，相关用例按此口径设计并在结果中如实记录。
  - 审计 reason 枚举实现含 `precondition-fail`（规范 §4 文本待补）；禁止清单实名为 `flash_firmware` 等 7 个。
  - **正式执行前**：归档现有 `audit.jsonl`（含自测事件）后清空，确认 `.killswitch` = 0。

## 1. 环境前置（P）

| 用例 | 操作 | 期望 |
|---|---|---|
| P1 | SSH `pi@<PI-LAN-IP>` 登录；`lsusb`、`ls /dev/ttyACM0` | 登录成功；见 `303a:1001`；ttyACM0 在位。✅ 公钥已配（~/.ssh/id_ed25519），免密可用，2026-10-24 实测通过（hostname=pi） |
| P2 | 以 <PI-USER> 直读直写 `/dev/ttyACM0`（发 `CMD PING`） | 无权限报错；返回 `ACK PING …`（权限来自 plugdev 组/ACL） |
| P3 | 直连串口 `CMD PING` | `ACK PING {"uptime_ms":…,"fw":"1.1"}`。若 fw≠1.1 → BLOCKED，等开发从 Pi 侧烧录（开发负责，测试不烧录） |

## 2. AC-0 描述符加载（T0）

| 用例 | 操作 | 期望 |
|---|---|---|
| T0-1 | 以两份合法描述符（`esp32s3-led-01`、`virtual-actuator-01`）启动网关 | 均加载成功；启动日志/接口可见 2 设备注册 |
| T0-2 | 拷贝 esp32s3-led-01 描述符，删除 `actions[set_led].risk` 后加载 | 拒载，报错指明缺失字段；网关进程不崩溃，其它合法描述符不受影响 |
| T0-3 | 非法枚举两连测：`risk: L5`；`device.category: foobar` | 均拒载并报错 |
| T0-4 | `device.id` 含大写（如 `Esp32s3-led-01`） | 拒载（id 限小写字母数字连字符） |
| T0-5 | 负例组：① L2 动作缺 `confirm` 声明；② `period_ms` 写成 `min>max`；③ preconditions 引用未定义动作 | 三例均拒载并报错（对应 dcd_loader 校验项） |

## 3. AC-1 云端调试（T1，MCP 局域网口径）

| 用例 | 操作 | 期望 |
|---|---|---|
| T1-1 | MCP 客户端（局域网/SSH 隧道）list tools / list resources | tools = `esp32s3-led-01.{ping,read_temp,set_led,reset}` + `virtual-actuator-01.set_output`；resources = `esp32s3-led-01/{chip_temp_c,led_mode,led_color,fw_version}` + `virtual-actuator-01/output`；与描述符 1:1 |
| T1-2 | 读 `esp32s3-led-01/chip_temp_c` | float，合理范围（约 10~60°C） |
| T1-3 | 读 `esp32s3-led-01/led_mode` | 枚举值 ∈ [static,blink,cycle,off]，初始 = cycle（v1.0 默认保留） |
| T1-4 | 调 `esp32s3-led-01.set_led {mode:static, color:"#00ff00"}` | 返回 ok；**板上 LED 变绿色常亮（目视确认）** |
| T1-5 | 读回 `led_mode`/`led_color` | = static / #00ff00；随后恢复 cycle |
| T1-6 | 调 `virtual-actuator-01.set_output {state:on}` → 读 `output` | ok；读回 on；再置 off 读回 off |
| T1-7 | 设备离线边界：拔 USB → 等 health 判 offline（两设备场景探活间隔压到 1s，约 3 次 miss ≈ 3s）→ 调 set_led → 插回 | set_led 被前置校验拒绝（denied/precondition-fail）且留审计；插回后自动重枚举、探活恢复、动作可再次执行 |

## 4. AC-2 本地闭环（T2，等价触发口径，Chief 2026-10-24 批）

前置：`rule-20261024-01` active 且 `rules-run` 常驻（poll 0.5s）；触发源 = `virtual-actuator-01/output == "on"`（for_s=0，冷却 30s），动作 = ESP32 红闪，verify = led_mode==blink。预置 output=on 属调试预置，**不计入闭环时延**。时延测法：测试脚本时间戳（预置动作发出 → 读回 led_mode==blink）为主，审计 ts 差为辅。

| 用例 | 操作 | 期望 |
|---|---|---|
| T2-1 | 在线基线：MCP 预置 output=off、LED 置 cycle → MCP 置 output=on | 规则触发 → LED 红闪；verify 通过；端到端时延记录在案（目标 ≤2s） |
| T2-2 | 断网演练：删默认路由 + 断开云端 MCP 会话（验证云端确实不可达）→ 经本地 CLI 预置 output=on → 引擎本地完成触发→执行→校验 | 全程本地完成，端到端 **≤2s**；动作审计 actor=local-rule |
| T2-3 | 防抖 for_s（主规则 for_s=0 不含防抖，用临时 fixture 规则验证字段语义）：fixture 规则 for_s=5 → output=on 持 2s 后回 off | 不触发；再置 on 持续 ≥5s → 触发执行 |
| T2-4 | 重触发冷却（retrigger_cooldown_s=30）：触发成功后 output 置 off，30s 内再次置 on | 不重复执行（无新 local-rule allowed 审计）；30s 后再置 on → 正常再触发 |
| T2-5 | 恢复联网：路由加回 + MCP 重连 → 查事件留存与 outbox | 断网期间事件**本地全量留存**（audit.jsonl 无缺）；outbox 含 rule-verified 事件、`flush_outbox()` 接口可用。⚠️ 真实云端补传为留桩（push_to_cloud 返回 0），本轮按「本地不丢+接口在」验收，真实补传记 M4（口径见依赖 6） |
| T2-6 | verify 失败路径（fixture：verify 判据改为不可能值，on_fail retry:1） | 动作执行 2 次（首跑+重试）→ rule-verify-failed 事件进 outbox；两次动作均留审计 |

## 5. AC-3 护栏（T3）

| 用例 | 操作 | 期望 |
|---|---|---|
| T3-1 | MCP 直调未注册动作 `esp32s3-led-01.reboot` | denied（reason=whitelist）+ 审计；设备无任何反应 |
| T3-2 | 1 分钟内连调 read_temp 31 次（max_per_min=30） | 第 31 次 denied（reason=rate-limit）+ 审计 |
| T3-3 | 间隔 <500ms 连调两次 set_led（cooldown_ms=500） | 第二次 denied + 审计 |
| T3-4a | 急停（CLI 入口）：`gateway.py killswitch on` → **新起** CLI 进程调 set_led / reset / ping | L1/L2 均 denied（reason=kill-switch），L0 正常（仅 L0 可用）；`killswitch off` 后恢复。注：急停状态仅在新启动的进程生效（.killswitch 启动时读取） |
| T3-4b | 急停（云端入口）：经 MCP 调急停 | **预期 FAIL/缺口**：MCP 层未暴露急停 tool（tools 仅设备动作），云端一键急停本期无入口 → 报 Chief 定口径（补 tool 或降级为 CLI-only 运行约定） |
| T3-5 | L2 确认流：MCP 调 `reset`（不带 confirm → 带 `confirm: true`） | 未确认 → decision=deferred（reason=confirm-required，result=denied），设备不重启；确认后执行 → 1s 内重启、PING 恢复、fw 仍 1.1；失败走 R1 豁免 |
| T3-6 | 并发仲裁（**同进程口径**）：单一 MCP 会话并发发起两个相同 set_led（100ms 内） | 先到先得 allowed，后到 denied（reason=busy），两条均留审计。⚠️ 跨进程（云端 MCP 进程 vs 本地规则引擎进程）互斥/仲裁未实现（内存态不共享），跨进程口径见依赖 5 |
| T3-7 | 禁止清单：临时在描述符加 `flash_firmware`（L1）动作并请求执行 | 仍被拒绝（reason=whitelist，detail=命中禁止清单；禁止清单写死不接受描述符覆盖） |

## 6. AC-4 审计（T4，贯穿复检 + 终检）

| 用例 | 操作 | 期望 |
|---|---|---|
| T4-1 | 抽 5 条不同动作审计（allowed/denied 各半） | 12 字段齐全：ts/event_id/actor/rule_id/device_id/action/params/risk/decision/reason/result/latency_ms |
| T4-2 | 三种来源各抽 1 条：cloud-agent / local-rule / manual | 均可还原 触发源(actor) / 决策依据(decision+reason) / 结果+耗时(result+latency_ms) |
| T4-3 | 核对 AC-3 全部 denied 用例 | 每条拒绝都有审计记录，decision=denied |
| T4-4 | 云端查审计（一期局域网口径）：从局域网读取 `audit.jsonl`（模拟云端拉取）+ `gateway.py audit --tail` | 可拉取且与动作一致。⚠️ MCP 层无审计资源/tool，「云端可查」走局域网文件路径；FR-4.1「云端看日志」的 MCP 直查留 M4（见依赖 6） |

## 7. 依赖与开放项

**已解决：**
1. ~~AC-2 触发手段~~：等价触发口径已获批并落进 `rules.yaml`（rule-20261024-01，触发源=虚拟执行器 output）。
2. ~~审计 reason 枚举缺前置失败值~~：实现已含 `precondition-fail`（guards.py/audit.py）；规范 §4 文本建议补一个枚举值。
3. L2 确认形式已定：MCP 形参 `confirm: true`（README 明确），按此执行。
4. ~~SSH 环境~~：公钥免密已配好并实测通过（2026-10-24），环境侧无待办。

**待 Chief 拍板（读实现后新发现，影响验收口径）：**

5. **跨进程护栏状态不共享**：急停/限速/去重/互斥锁均为进程内存态（急停仅落盘、启动时读取）。云端 MCP 与本地规则引擎是两个进程，规范 §2「云端 vs 本地先到先得」跨进程未仲裁。T3-6 按同进程口径验收；跨进程建议一期定运行约定（调试与闭环不并发动同设备）或加文件锁，记 M4。
6. **断云补传为留桩**：`push_to_cloud` 返回 0、outbox 只进不出（内存态）。建议 AC-2「恢复补传」本轮口径 = 「事件本地全量不丢 + outbox/flush 接口在」，真实云端补传记 M4（与公网穿透同期）。
7. **云端急停/审计无 MCP 入口**：MCP tools 仅设备动作，无急停 tool、无审计资源 → AC-3「云端一键急停」本期无路径（T3-4b 预期 FAIL）、FR-4.1「云端看日志」走局域网文件。建议定口径：补 tool（小改动）或降级为 CLI-only 约定。

**小笔误（找 Chief 顺手修）：** README 部署命令 `--transport streamable-http` 与 CLI 取值不符（实际为 `--transport http`），照抄会报 argparse 错误。

## 8. 执行顺序与产出

- 顺序：P → AC-0 → AC-1 → AC-3 → AC-2 → AC-4 终检（AC-4 同时复检全程审计）。
- 产出：《MHS-网关-测试报告》——通过项 / 未通过项（含 bug 单：标题+复现步骤+期望+实际+相关文件）/ 遗留风险 / R1 豁免统计。

## 9. 执行结果快照（2026-10-24 真机执行，详见《MHS-网关-测试报告-20261024.md》）

| 用例 | 结果 | 备注 |
|---|---|---|
| P1/P2/P3 | PASS | P2 改走网关路径（裸开串口触发 CDC 复位瞬态） |
| T0-1~T0-5（含 8 负例） | PASS | 全拒载，报错带字段路径 |
| T1-1~T1-6 | PASS | T1-4 绿灯目视留用户终验 |
| T1-7 | PASS | 2026-10-25 用户配合拔插 USB 完成终验（e798d232：2ms 明确拒绝+审计完整；插回自动恢复） |
| T3-1~T3-7 | PASS | T3-4b 急停 MCP 入口已补齐实测过；T3-6 按同进程口径 |
| T2-1~T2-6 | PASS | 断网演练同秒闭环；⚠️ 时延判定改为**审计为主**（MCP 读路径饱和会污染客户端计时，实测 0.2~0.6s） |
| T4-1~T4-4 | PASS | T4-4 升级为 MCP 审计资源直查（mhs://gateway/audit/recent） |

缺陷 1 个：BUG-001 引擎间歇静默（详见报告 §三），已转开发，py-spy 抓捕环境就绪。

### 2026-10-25 补充（BUG-001 修复后复抓 + BUG-002 + 终验，详见《MHS-BUG001复抓与终验报告-20261025.md》）

| 项 | 结果 | 备注 |
|---|---|---|
| BUG-001 复抓 13 批次 | **未复现** | 探针 3 + 空载 6 + 带载 6 全触发；昨日 3/10 批次签名零出现 |
| 带载新观察项 | ⚠️ 记录 | 2×50ms 泛洪下慢 tick 告警 11 条、max 14.6s；触发时延 1.1~3.42s（1 次超 ≤2s 预算）；零漏触发 |
| BUG-002（新报） | 已修复回归全绿 | CLI 放行 enum 外参数→负例 4/4 拒绝 schema-invalid、合法不误伤；三路径全覆盖（guards.py 1b） |
| T1-7 终验 | PASS | 用户拔插 USB：离线 2ms 拒绝 + 插回自动恢复（fw=1.1） |
| T1-4 目视 | PASS | 17:14 用户确认绿灯常亮；验收后恢复默认 cycle（d13357fa） |
