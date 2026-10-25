#!/usr/bin/env python3
"""gateway.py — MHS 树莓派网关主编排 + CLI。

流水线（每次动作）：白名单/禁止清单 → 急停 → L2 确认 → 前置条件 →
限速/冷却 → 去重/互斥 → 适配器执行 → 审计（§4 三要素 + 耗时）。

用法：
  python3 gateway.py serve-virtual                      # 起虚拟执行器 :9999
  python3 gateway.py exec esp32s3-led-01 set_led \
      --params '{"mode":"static","color":"#00ff00"}'    # 手动触发（带护栏+审计）
  python3 gateway.py read esp32s3-led-01 chip_temp_c
  python3 gateway.py killswitch on|off                  # 急停（L1/L2 全挂起）
  python3 gateway.py audit --tail 20                    # 查审计
  python3 gateway.py health --once                      # 探活一轮
  python3 gateway.py rules-run --file rules.yaml --once # 规则引擎单步/常驻
  python3 gateway.py mcp --transport stdio              # MCP server（云端调试入口）
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

from adapters import AdapterError, build_adapter
from audit import Audit
from dcd_loader import load_dir
from guards import Decision, Guard

BASE = Path(__file__).resolve().parent
DEFAULT_DESCRIPTORS = BASE / "descriptors"
DEFAULT_AUDIT = BASE / "audit.jsonl"
KILL_FILE = BASE / ".killswitch"


class Gateway:
    def __init__(self, descriptors_dir: str | Path = DEFAULT_DESCRIPTORS,
                 audit_path: str | Path = DEFAULT_AUDIT, start_health: bool = True):
        self.descriptors = load_dir(descriptors_dir)          # 任一非法即抛（AC-0）
        self.guard = Guard(self.descriptors)
        self.audit = Audit(audit_path)
        self.devices: dict[str, dict] = {}
        for dev_id, dcd in self.descriptors.items():
            self.devices[dev_id] = {"dcd": dcd, "adapter": build_adapter(dcd)}
        self.health_state: dict[str, str] = {k: "unknown" for k in self.devices}
        self._health_stop = threading.Event()
        if KILL_FILE.exists() and KILL_FILE.read_text().strip() == "1":
            self.guard.set_kill_switch(True)                   # 急停状态跨重启保持
        if start_health:
            self._health_thread = threading.Thread(target=self._health_loop, daemon=True)
            self._health_thread.start()
        # 急停文件监视（跨进程同步：CLI/MCP 写 .killswitch → 常驻进程 0.5s 内生效）
        self._kill_thread = threading.Thread(target=self._kill_file_loop, daemon=True)
        self._kill_thread.start()

    def close(self):
        self._health_stop.set()
        for d in self.devices.values():
            try:
                d["adapter"]._close()
            except Exception:
                pass

    # ---------------- 动作执行（含护栏 + 审计） ----------------
    def execute(self, device_id: str, action: str, params: dict | None = None,
                actor: str = "manual", rule_id: str | None = None) -> dict:
        t0 = time.monotonic()
        params = dict(params or {})

        def latency() -> int:
            return int((time.monotonic() - t0) * 1000)

        def audit(decision: str, reason: str, result: str, risk: str = "?",
                  event_id: str | None = None) -> dict:
            ev = self.audit.log(actor=actor, rule_id=rule_id, device_id=device_id,
                                action=action, params=params, risk=risk,
                                decision=decision, reason=reason, result=result,
                                latency_ms=latency())
            return {"decision": decision, "reason": reason, "result": result,
                    "latency_ms": ev["latency_ms"], "event_id": ev["event_id"]}

        adef = self.guard.action_def(device_id, action)
        if adef is None:
            return audit("denied", "whitelist", "denied")     # 未注册动作（AC-3）
        risk = adef["risk"]

        d: Decision = self.guard.check_pre(device_id, action, params)
        if not d.allowed:
            return audit(d.decision, d.reason, "denied", risk)

        # 前置条件（preconditions: [ping]）——拿锁前先跑（L0 探活），失败 → precondition-fail
        # 内部探活直连适配器（L0、无副作用），不落审计（§4 actor 枚举仅 local-rule|cloud-agent|manual）
        for pre in adef.get("preconditions", []):
            predef = self.guard.action_def(device_id, pre) or {}
            try:
                self.devices[device_id]["adapter"].execute(
                    pre, {}, predef.get("timeout_ms", 800))
            except AdapterError:
                return audit("denied", "precondition-fail", "denied", risk)

        d = self.guard.acquire(device_id, action)   # 每设备互斥锁，先到先得
        if not d.allowed:
            return audit(d.decision, d.reason, "denied", risk)

        # L2 人工确认标记：作为参数传入但不下发设备
        send_params = {k: v for k, v in params.items() if k != "confirm"}
        try:
            try:
                res = self.devices[device_id]["adapter"].execute(
                    action, send_params, adef.get("timeout_ms", 800))
                result = "ok"
            except AdapterError as e:
                res = {"error": str(e)}
                result = e.kind  # timeout | error
        finally:
            d.release()          # 互斥锁必释放

        if result == "ok":
            self.guard.mark_executed(device_id, action)
        ev = audit("allowed", "whitelist", result, risk)  # 允许依据=白名单放行
        ev["result_data"] = res
        return ev

    # ---------------- 资源读取（L0，不入动作审计） ----------------
    def read_resource(self, device_id: str, name: str):
        if device_id not in self.devices:
            raise AdapterError(f"未知设备：{device_id}")
        return self.devices[device_id]["adapter"].read_resource(name)

    # ---------------- 急停 ----------------
    def _kill_file_loop(self):
        last = None
        while not self._health_stop.wait(0.5):
            try:
                val = KILL_FILE.read_text().strip() if KILL_FILE.exists() else "0"
            except OSError:
                continue
            if val != last:
                self.guard.set_kill_switch(val == "1")
                last = val

    def set_kill_switch(self, on: bool) -> None:
        self.guard.set_kill_switch(on)
        KILL_FILE.write_text("1" if on else "0")

    @property
    def kill_switch(self) -> bool:
        return self.guard.kill_switch

    # ---------------- 探活 ----------------
    def _health_loop(self):
        while not self._health_stop.is_set():
            for dev_id, d in self.devices.items():
                probe = d["dcd"].get("health", {}).get("probe", {})
                interval = probe.get("interval_s", 5)
                try:
                    d["adapter"].ping(timeout_ms=500)
                    self.health_state[dev_id] = "online"
                    d["_miss"] = 0
                except AdapterError:
                    d["_miss"] = d.get("_miss", 0) + 1
                    if d["_miss"] >= probe.get("miss_threshold", 3):
                        self.health_state[dev_id] = "offline"
                base = min(interval, 1.0) if len(self.devices) > 1 else interval
                # 失联退避：连续失败时 1s→2s→4s→5s 封顶，掐掉 reopen 风暴
                # （BUG 2026-10-24：失联设备被每秒 ping 轰炸，适配器反复 invalidate/
                #   reopen 拖累同进程其它线程；健康设备节奏不变）
                miss = d["_miss"]
                wait_s = min(5.0, base * (2 ** min(miss, 3))) if miss else base
                self._health_stop.wait(wait_s)
                if self._health_stop.is_set():
                    break

    def health_once(self) -> dict:
        for dev_id, d in self.devices.items():
            try:
                d["adapter"].ping(timeout_ms=500)
                self.health_state[dev_id] = "online"
                d["_miss"] = 0
            except AdapterError:
                self.health_state[dev_id] = "offline"
                d["_miss"] = d.get("_miss", 0) + 1
        return dict(self.health_state)


# ---------------- CLI ----------------

def _load_gateway(a: argparse.Namespace) -> Gateway:
    return Gateway(descriptors_dir=a.descriptors, audit_path=a.audit)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gateway")
    ap.add_argument("--descriptors", default=str(DEFAULT_DESCRIPTORS))
    ap.add_argument("--audit", default=str(DEFAULT_AUDIT))
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve-virtual")
    p = sub.add_parser("exec")
    p.add_argument("device")
    p.add_argument("action")
    p.add_argument("--params", default="{}")
    p.add_argument("--actor", default="manual",
                   choices=["manual", "cloud-agent", "local-rule"])
    p.add_argument("--rule-id", default=None)
    p.add_argument("--confirm", action="store_true", help="L2 动作人工确认标记")
    p = sub.add_parser("read")
    p.add_argument("device")
    p.add_argument("resource")
    p = sub.add_parser("killswitch")
    p.add_argument("state", choices=["on", "off"])
    p = sub.add_parser("audit")
    p.add_argument("--tail", type=int, default=20)
    p = sub.add_parser("health")
    p.add_argument("--once", action="store_true")
    p = sub.add_parser("rules-run")
    p.add_argument("--file", required=True)
    p.add_argument("--once", action="store_true", help="只评估一轮（测试用）")
    p = sub.add_parser("mcp")
    p.add_argument("--transport", default="stdio", choices=["stdio", "http"])
    p.add_argument("--port", type=int, default=8808)
    p.add_argument("--rules", default=None,
                   help="同进程起规则引擎线程（推荐部署形态，护栏/急停共享）")

    a = ap.parse_args(argv)

    if a.cmd == "serve-virtual":
        import virtual_executor
        virtual_executor.start()
        return 0

    if a.cmd == "killswitch":
        # 轻量：只写急停文件 + 一条审计；不 build Gateway（避免误开串口）。
        # 常驻进程（mcp --rules / rules-run）由 _kill_file_loop 0.5s 内同步生效。
        KILL_FILE.parent.mkdir(parents=True, exist_ok=True)
        KILL_FILE.write_text("1" if a.state == "on" else "0")
        Audit(a.audit).log(
            actor="manual", rule_id=None, device_id="gateway", action="kill_switch",
            params={"state": a.state}, risk="L1", decision="allowed",
            reason="whitelist", result="ok", latency_ms=0)
        print(f"kill-switch = {'ON（仅 L0 可用）' if a.state == 'on' else 'OFF'}")
        return 0

    if a.cmd == "audit":
        for ev in Audit(a.audit).tail(a.tail):
            print(json.dumps(ev, ensure_ascii=False))
        return 0

    if a.cmd == "mcp":
        from mcp_server import serve
        gw = _load_gateway(a)
        engine = None
        if a.rules:
            from rules import RuleEngine, load_rules
            engine = RuleEngine(gw, load_rules(a.rules))
            engine.start_background()
        serve(gw, transport=a.transport, port=a.port, engine=engine)
        return 0

    gw = _load_gateway(a)
    try:
        if a.cmd == "exec":
            params = json.loads(a.params)
            if a.confirm:
                params["confirm"] = True
            r = gw.execute(a.device, a.action, params, actor=a.actor, rule_id=a.rule_id)
            print(json.dumps(r, ensure_ascii=False))
            return 0 if r["result"] == "ok" else 1
        if a.cmd == "read":
            print(json.dumps(gw.read_resource(a.device, a.resource), ensure_ascii=False))
            return 0
        if a.cmd == "health":
            print(json.dumps(gw.health_once(), ensure_ascii=False))
            return 0
        if a.cmd == "rules-run":
            from rules import RuleEngine, load_rules
            eng = RuleEngine(gw, load_rules(a.file))
            if a.once:
                fired = eng.evaluate_once()
                print(json.dumps({"fired": fired, "health": gw.health_state},
                                 ensure_ascii=False))
                return 0
            print("规则引擎常驻中，Ctrl-C 退出")
            eng.watch()
            return 0
    except AdapterError as e:
        print(json.dumps({"error": str(e), "kind": e.kind}, ensure_ascii=False))
        return 2
    finally:
        gw.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
