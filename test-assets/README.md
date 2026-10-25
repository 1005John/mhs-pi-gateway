# test-assets — 回归脚本与证据

## 脚本

| 文件 | 用途 |
|---|---|
| `reg_rounds.py` | 触发批次回归：周期预置触发源 → 引擎自动 fire → 读审计对账（零漏触发检查） |
| `reg_fixture.py` + `reg-fixture-*.yaml` | 独立 fixture 引擎进程（不依赖主网关），用于跨进程对照复现 |
| `mcp_load.py` | MCP 并发负载发生器（读资源/调动作，制造串口争用窗口） |

复抓 BUG-001 的标准姿势（三路证据对账）：

```bash
# 1. 触发批次 + 审计对账
python3 reg_rounds.py
# 2. 引擎侧证据：logs/gateway.log（慢 tick >2s 常开 WARN；MHS_RULE_DEBUG=1 有逐 tick 日志）
# 3. 现场栈：py-spy dump --pid <gateway-pid>（发作瞬间抓）
```

## evidence-20261025/ — BUG-001 复抓战役存档

- `dev-analysis-slow-tick-20261025.md` — 开发归因分析（**先读这个**）：慢 tick ~0.9s 量子 = 适配器 800ms 单命令超时+自动重试指纹；负载是放大器不是开关
- `pi-logs/audit-20261025-regression.jsonl` — 回归窗口全部审计事件（12 字段）
- `pi-logs/gateway.log.gz` — 引擎逐 tick 日志（4.1M 原始，gzip 存档；38320 ticks 零静默的证据）
- `pi-tmp/` — 当时的 py-spy 栈 dump 与现场脚本副本

## 结论快照（2026-10-25）

- BUG-001（检测段整窗静默）：38320 ticks 零复现，维持未复现、原因未知单独挂案
- 观察项（不阻塞）：串口命令间歇性首试丢失 + 800ms 超时重试兜底；空载约每 2 次 fire 慢一次 verify 读，带载叠加至 14.6s
- 若立项优化：先补三样仪表（verify 逐读日志 / attempt 级计时 / 重开事件）再复抓定界「主机 USB/CDC vs 固件响应」
