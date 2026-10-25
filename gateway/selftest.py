#!/usr/bin/env python3
"""selftest.py — MHS 网关本地自测（无需 ESP32 硬件，Mac/Pi 通用）。

覆盖：AC-0 描述符校验（正/负）· 虚拟执行器+TCP 适配器往返 · 护栏（白名单/
急停/L2 确认/限速/去重/前置条件）· 规则引擎端到端（触发→动作→校验→审计）。
硬件相关项（MSP-1 串口、真实闭环）由测试组 32 条用例在 Pi 上跑。
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
import time
import time
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    tag = "PASS" if cond else "FAIL"
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print(f"[{tag}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    import adapters
    from audit import Audit
    from dcd_loader import load_dir, validate, DcdError
    from guards import Guard
    from rules import RuleEngine, load_rules

    # ---------- AC-0 描述符 ----------
    dcds = load_dir(BASE / "descriptors")
    check("AC-0 合法描述符加载", set(dcds) == {"esp32s3-led-01", "virtual-actuator-01"})
    base = yaml.safe_load((BASE / "descriptors/virtual-actuator-01.yaml").read_text())

    def rejects(name, mutate):
        d = copy.deepcopy(base)
        mutate(d)
        try:
            validate(d)
            check(f"AC-0 拒载 {name}", False)
        except DcdError:
            check(f"AC-0 拒载 {name}", True)

    rejects("缺 device.id", lambda d: d["device"].pop("id"))
    rejects("非法 risk", lambda d: d["actions"][0].__setitem__("risk", "L3"))
    rejects("非法 access", lambda d: d["resources"][0].__setitem__("access", "x"))
    rejects("L2 无 confirm", lambda d: d["actions"].append(
        {"name": "hard_reset", "risk": "L2", "params": {}, "timeout_ms": 5000}))

    # ---------- 虚拟执行器 + TCP 适配器 ----------
    import virtual_executor
    srv = virtual_executor.start(9999, "127.0.0.1", background=True)
    time.sleep(0.2)
    ad = adapters.NetTcpJsonAdapter("127.0.0.1", 9999)
    t0 = time.monotonic()
    p = ad.ping()
    check("虚拟执行器 PING", "uptime_ms" in p and p.get("fw") == "1.0-virtual",
          f"{int((time.monotonic() - t0) * 1000)}ms")
    r = ad.execute("set_output", {"state": "on"}, 500)
    check("set_output on", r == {"ok": True})
    check("读回 output=on", ad.read_resource("output") == "on")
    try:
        ad.execute("set_output", {"state": "bogus"}, 500)
        check("非法 state → ERR 02", False)
    except adapters.AdapterError as e:
        check("非法 state → ERR 02", "02" in str(e), str(e))
    try:
        ad.command("BOGUS", None, 500)
        check("未知动作 → ERR 01", False)
    except adapters.AdapterError as e:
        check("未知动作 → ERR 01", "01" in str(e), str(e))

    # ---------- 网关 + 护栏 ----------
    tmp = tempfile.mkdtemp()
    from gateway import Gateway
    gw = Gateway(descriptors_dir=BASE / "descriptors",
                 audit_path=Path(tmp) / "audit.jsonl", start_health=False)
    gw.devices["virtual-actuator-01"]["adapter"] = ad  # 复用上面的适配器

    r = gw.execute("no-such-device", "ping", {})
    check("未注册设备 → whitelist", r["decision"] == "denied" and r["reason"] == "whitelist")
    r = gw.execute("virtual-actuator-01", "not_an_action", {})
    check("未注册动作 → whitelist", r["decision"] == "denied" and r["reason"] == "whitelist")
    r = gw.execute("esp32s3-led-01", "flash_firmware", {})
    check("禁止清单 → whitelist", r["decision"] == "denied" and r["reason"] == "whitelist")

    # 参数 schema 校验（BUG-002：非法值在网关层拒，不触达设备）
    r = gw.execute("esp32s3-led-01", "set_led", {"mode": "on", "color": "#00ff00"})
    check("参数 enum 外 → schema-invalid（BUG-002）",
          r["decision"] == "denied" and r["reason"] == "schema-invalid", str(r))
    r = gw.execute("esp32s3-led-01", "set_led", {"mode": "blink", "period_ms": 5})
    check("参数越界 min → schema-invalid",
          r["decision"] == "denied" and r["reason"] == "schema-invalid", str(r))
    r = gw.execute("esp32s3-led-01", "set_led", {"mode": "static"})
    check("required_if 缺 color → schema-invalid",
          r["decision"] == "denied" and r["reason"] == "schema-invalid", str(r))
    r = gw.execute("esp32s3-led-01", "set_led", {"mode": "cycle", "bogus": 1})
    check("未知参数 → schema-invalid",
          r["decision"] == "denied" and r["reason"] == "schema-invalid", str(r))

    r = gw.execute("esp32s3-led-01", "reset", {})
    check("L2 无确认 → deferred/confirm-required",
          r["decision"] == "deferred" and r["reason"] == "confirm-required")

    # 限速：set_output max 20/min → 第 21 次 denied/rate-limit
    # （合规参数交替 on/off 避开去重；同值两发间隔 ≥124ms > 去重窗口 100ms）
    codes = []
    for i in range(21):
        codes.append(gw.execute("virtual-actuator-01", "set_output",
                                {"state": "on" if i % 2 == 0 else "off"}))
        time.sleep(0.06)
    check("限速 21 次 → 第 21 次 rate-limit",
          codes[19]["result"] == "ok" and codes[20]["reason"] == "rate-limit",
          f"19th={codes[19]['reason']}/{codes[19]['result']} 20th={codes[20]['reason']}")

    # 去重：同参数 100ms 内第二发 busy（用虚拟执行器 ping：TCP 本地往返快、
    # 且 L0 不占限速配额。真机串口往返慢，两发会超出 100ms 窗口导致假阴性）
    r = gw.execute("virtual-actuator-01", "ping", {})
    r2 = gw.execute("virtual-actuator-01", "ping", {})
    check("100ms 去重 → busy", r2["reason"] == "busy", r2["reason"])

    # 急停：L1 挂起，L0 仍可用（先让上面的去重窗口过期，避免 L0 用例撞去重误报）
    time.sleep(0.15)
    gw.set_kill_switch(True)
    r = gw.execute("virtual-actuator-01", "set_output", {"state": "on"})
    check("急停 → L1 denied/kill-switch",
          r["decision"] == "denied" and r["reason"] == "kill-switch", str(r))
    r = gw.execute("virtual-actuator-01", "ping", {})
    check("急停 → L0 仍可用", r["result"] == "ok", str(r))
    gw.set_kill_switch(False)

    # 前置条件：set_led 有 [ping]，esp32 不在本机 → ping 超时 → precondition-fail
    r = gw.execute("esp32s3-led-01", "set_led", {"mode": "static", "color": "#00ff00"})
    check("前置条件失败 → precondition-fail",
          r["decision"] == "denied" and r["reason"] == "precondition-fail", r["reason"])

    # 审计三要素
    evs = Audit(Path(tmp) / "audit.jsonl").query(device_id="esp32s3-led-01",
                                                 action="set_led")
    ok3 = bool(evs) and all(k in evs[-1] for k in
                            ("actor", "reason", "result", "latency_ms", "ts"))
    check("审计三要素+耗时齐备", ok3, json.dumps(evs[-1], ensure_ascii=False)[:120])

    # ---------- 规则引擎端到端（自复位规则，无需硬件；新 Gateway=干净护栏状态） ----------
    gw2 = Gateway(descriptors_dir=BASE / "descriptors",
                  audit_path=Path(tmp) / "audit2.jsonl", start_health=False)
    gw2.devices["virtual-actuator-01"]["adapter"] = ad
    rules = load_rules(BASE / "rules.yaml")
    selftest_rule = copy.deepcopy(rules[1])
    selftest_rule["status"] = "active"
    eng = RuleEngine(gw2, [selftest_rule], poll_s=0.05)
    ad.execute("set_output", {"state": "on"}, 500)
    fired = eng.evaluate_once()              # for_s=0：首轮立即触发→动作→校验
    check("规则引擎 触发→动作→校验", fired == ["rule-selftest-01"], str(fired))
    check("规则动作生效 output=off", ad.read_resource("output") == "off")
    check("断云通知进 outbox（待补传）", len(eng.outbox) >= 1, str(eng.outbox))
    st = eng.stats
    check("引擎观测计数可用（BUG 排查仪表）",
          st["fires"] >= 1 and st["read_errors"] == 0,
          str(st))

    # AC-2 演练规则（rule-20261024-01）存在且为 active（硬件在 Pi 侧执行）
    check("AC-2 规则就位", rules[0]["status"] == "active"
          and rules[0]["trigger"]["resource"] == "virtual-actuator-01/output")

    # ---------- MCP server 构建（tools/resources 由描述符生成） ----------
    try:
        from mcp_server import build
        mcp = build(gw)
        import asyncio
        tools = asyncio.get_event_loop().run_until_complete(mcp.list_tools())
        names = {t.name for t in tools}
        check("MCP tools 与描述符一一对应",
              {"esp32s3-led-01.ping", "esp32s3-led-01.set_led",
               "esp32s3-led-01.reset", "virtual-actuator-01.set_output"} <= names,
              str(sorted(names)))
        res = asyncio.get_event_loop().run_until_complete(mcp.list_resources())
        uris = {str(x.uri) for x in res}
        check("MCP resources 与描述符一一对应",
              {"mhs://esp32s3-led-01/chip_temp_c",
               "mhs://virtual-actuator-01/output"} <= uris, str(sorted(uris)))
        check("MCP 管理工具在位（FR-6.3 急停）",
              {"gateway.kill_switch", "gateway.rule_set_status"} <= names
              and "mhs://gateway/audit/recent" in uris,
              str(sorted(names)))
    except Exception as e:  # noqa: BLE001
        check("MCP server 构建", False, repr(e))

    gw.close()
    gw2.close()
    srv.shutdown()
    print(f"\n自测结果：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
