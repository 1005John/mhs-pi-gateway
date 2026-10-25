#!/usr/bin/env python3
"""rules.py — 本地联动规则引擎（规范 §3）。

四态：draft → testing → active ⇄ disabled（只允许云端改状态，本地只执行 active）。
触发→动作→校验全程本地执行（AC-2 断云可用）；断网期间云端通知进 outbox，恢复补传。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import yaml

OPS = {
    ">":  lambda a, b: a > b,
    "<":  lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


class RuleError(Exception):
    pass


def load_rules(path: str | Path) -> list[dict]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    rules = data.get("rules", data if isinstance(data, list) else [])
    if not isinstance(rules, list):
        raise RuleError("规则文件顶层应为 {rules: [...]} 或列表")
    for r in rules:
        _validate_rule(r)
    return rules


def _validate_rule(r: dict) -> None:
    for k in ("id", "status", "trigger", "actions"):
        if k not in r:
            raise RuleError(f"规则缺字段 {k}: {r.get('id', '?')}")
    if r["status"] not in ("draft", "testing", "active", "disabled"):
        raise RuleError(f"规则状态非法：{r['status']}")
    trig = r["trigger"]
    for k in ("resource", "op", "value"):
        if k not in trig:
            raise RuleError(f"规则 {r['id']} trigger 缺 {k}")
    if trig["op"] not in OPS:
        raise RuleError(f"规则 {r['id']} op 非法：{trig['op']}")
    if not isinstance(r["actions"], list) or not r["actions"]:
        raise RuleError(f"规则 {r['id']} actions 不能为空")
    for a in r["actions"]:
        if "device" not in a or "action" not in a:
            raise RuleError(f"规则 {r['id']} 动作缺 device/action")
    v = r.get("verify")
    if v and (v.get("op") not in OPS or "resource" not in v):
        raise RuleError(f"规则 {r['id']} verify 非法")


class RuleEngine:
    STATUS_VALUES = ("draft", "testing", "active", "disabled")

    def __init__(self, gateway, rules: list[dict], poll_s: float = 0.5,
                 rules_path: str | Path | None = None):
        self.gw = gateway
        self.poll_s = poll_s
        self.rules_path = Path(rules_path) if rules_path else None
        # overlay 文件：<rules stem>.status.json（如 rules.yaml → rules.status.json）
        # 只存「与 rules.yaml 不同的状态」，yaml 里的注释不会被 yaml.dump 冲掉
        self.overlay_path = (self.rules_path.with_name(self.rules_path.stem + ".status.json")
                             if self.rules_path else None)
        self._overlay: dict[str, str] = {}
        self._mtimes: dict = {}
        self._file_status: dict[str, str] = {}
        self.all_rules = rules
        self._true_since: dict[str, float | None] = {}
        self._last_fire: dict[str, float] = {}
        self._rebuild_active()
        self.outbox: list[dict] = []   # 断网期间云端通知缓存（恢复补传）
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None
        # 观测计数（BUG 2026-10-24 引擎间歇静默排查）：逐 tick 日志走 env 开关，
        # 慢 tick 告警常开（stderr），现场证据与 audit.jsonl 时间戳对账用
        self.debug = os.environ.get("MHS_RULE_DEBUG", "") == "1"
        self.stats = {"ticks": 0, "read_errors": 0, "fires": 0,
                      "slow_ticks": 0, "max_tick_ms": 0.0}
        if self.rules_path:
            self.reload()              # 首次即读 overlay 合并状态

    def _dlog(self, msg: str) -> None:
        if self.debug:
            print(f"[rule-engine {time.strftime('%H:%M:%S')}] {msg}",
                  file=sys.stderr, flush=True)

    # ---------------- 热重载（rules.yaml / overlay mtime 驱动） ----------------
    def _snapshot_mtimes(self) -> dict:
        try:
            m = {"rules": self.rules_path.stat().st_mtime_ns}
        except OSError:
            return {}
        try:
            m["overlay"] = (self.overlay_path.stat().st_mtime_ns
                            if self.overlay_path.exists() else None)
        except OSError:
            m["overlay"] = None
        return m

    def _read_overlay(self) -> dict[str, str]:
        if not self.overlay_path or not self.overlay_path.exists():
            return {}
        try:
            data = json.loads(self.overlay_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        st = data.get("statuses", data)
        return {k: v for k, v in st.items() if v in self.STATUS_VALUES}

    def reload(self) -> bool:
        """rules.yaml 或 overlay 变化 → 重载并合并状态。返回是否有变化。"""
        if not self.rules_path:
            return False
        mt = self._snapshot_mtimes()
        if mt and mt == self._mtimes:
            return False
        try:
            fresh = load_rules(self.rules_path)
        except Exception:              # noqa: BLE001 半写/非法：保留现状，下轮再试
            return False
        self._mtimes = mt
        self._file_status = {r["id"]: r["status"] for r in fresh}
        overlay = self._read_overlay()
        for r in fresh:
            st = overlay.get(r["id"])
            if st in self.STATUS_VALUES:
                r["status"] = st
        self._overlay = overlay
        self.all_rules = fresh
        self._rebuild_active()
        return True

    def _rebuild_active(self) -> None:
        self.rules = [r for r in self.all_rules if r["status"] == "active"]
        ids = {r["id"] for r in self.rules}
        for rid in list(self._true_since):
            if rid not in ids:
                del self._true_since[rid]
        for rid in list(self._last_fire):
            if rid not in ids:
                del self._last_fire[rid]
        for r in self.rules:
            self._true_since.setdefault(r["id"], None)

    # ---------------- 规则状态管理（云端经 MCP gateway.rule_set_status） ----------------
    def set_rule_status(self, rule_id: str, status: str) -> dict:
        if status not in self.STATUS_VALUES:
            raise RuleError(f"状态非法：{status}（应为 {'/'.join(self.STATUS_VALUES)}）")
        rule = next((r for r in self.all_rules if r["id"] == rule_id), None)
        if rule is None:
            raise RuleError(f"规则不存在：{rule_id}")
        if not self.rules_path:
            rule["status"] = status
            self._rebuild_active()
            return {"rule_id": rule_id, "status": status, "persisted": False}
        overlay = dict(self._overlay)
        if status == self._file_status.get(rule_id):
            overlay.pop(rule_id, None)     # 与 yaml 一致就不留 overlay 条目
        else:
            overlay[rule_id] = status
        tmp = self.overlay_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"statuses": overlay}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.overlay_path)     # 原子替换，避免引擎读到半写文件
        self.reload()
        return {"rule_id": rule_id, "status": status, "persisted": True}

    def _compare(self, op: str, actual: Any, value: Any) -> bool:
        try:
            return OPS[op](actual, value)
        except TypeError:
            return False

    def _read(self, resource_full: str) -> Any:
        device_id, name = resource_full.split("/", 1)
        return self.gw.read_resource(device_id, name)

    def evaluate_once(self) -> list[dict]:
        """跑一轮评估（测试友好：可单步）；返回本轮触发过的规则 id。"""
        fired = []
        now = time.monotonic()
        for r in self.rules:
            rid = r["id"]
            trig = r["trigger"]
            t_read = time.monotonic()
            try:
                actual = self._read(trig["resource"])
            except Exception as e:  # noqa: BLE001 设备离线/读失败 → 条件不成立
                self.stats["read_errors"] += 1
                self._true_since[rid] = None
                self._dlog(f"tick rid={rid} read ERROR after "
                           f"{(time.monotonic() - t_read) * 1000:.0f}ms: "
                           f"{type(e).__name__}: {e}")
                continue
            rlat = (time.monotonic() - t_read) * 1000
            cond = self._compare(trig["op"], actual, trig["value"])
            for_s = float(trig.get("for_s", 0) or 0)
            self._dlog(f"tick rid={rid} read={actual!r} ({rlat:.0f}ms) cond={cond} "
                       f"true_since={self._true_since[rid]} "
                       f"last_fire={self._last_fire.get(rid)}")
            if not cond:
                self._true_since[rid] = None
                continue
            if self._true_since[rid] is None:
                self._true_since[rid] = now
                if for_s > 0:
                    continue   # 需持续 for_s 秒才触发：先记时，下轮再判
                # for_s=0：首次成立立即触发（不要求两轮评估）
            if now - self._true_since[rid] < for_s:
                continue
            cooldown = float(trig.get("retrigger_cooldown_s", 0) or 0)
            if now - self._last_fire.get(rid, -1e9) < cooldown:
                self._dlog(f"tick rid={rid} 条件成立但冷却中"
                           f"（剩 {cooldown - (now - self._last_fire.get(rid, -1e9)):.1f}s）")
                continue
            self._dlog(f"tick rid={rid} → FIRE")
            ok = self._fire(r)
            if ok:
                self.stats["fires"] += 1
                self._last_fire[rid] = now
                self._true_since[rid] = None
                fired.append(rid)
        return fired

    def _fire(self, rule: dict) -> bool:
        """顺序执行动作 + 校验；带 retry。返回最终是否判过。"""
        attempts = 1 + int((rule.get("verify") or {}).get("on_fail", {}).get("retry", 0))
        verified = False
        for i in range(attempts):
            for a in rule["actions"]:
                self.gw.execute(a["device"], a["action"], a.get("params") or {},
                                actor="local-rule", rule_id=rule["id"])
            verify = rule.get("verify")
            if not verify:
                verified = True
                break
            deadline = time.monotonic() + verify.get("timeout_ms", 3000) / 1000.0
            while time.monotonic() < deadline:
                try:
                    actual = self._read(verify["resource"])
                except Exception:  # noqa: BLE001
                    actual = None
                if self._compare(verify["op"], actual, verify["value"]):
                    verified = True
                    break
                time.sleep(min(0.2, max(0.05, self.poll_s)))
            if verified:
                break
        # 校验结果通知云端（断网进 outbox，恢复补传）
        self.outbox.append({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "rule_id": rule["id"],
            "event": "rule-verified" if verified else "rule-verify-failed",
        })
        return verified

    def flush_outbox(self) -> int:
        """恢复联网后补传；一期云端订阅未接，返回剩余条数。"""
        return len(self.outbox)

    def watch(self, stop: threading.Event | None = None):
        stop = stop or threading.Event()
        slow_s = max(2.0, 4 * self.poll_s)   # 单 tick 预算上限（读 ≤1.6s×2 + 余量）
        while not stop.is_set():
            t0 = time.monotonic()
            self.reload()              # 规则/状态文件热重载（含云端 overlay 变更）
            fired = self.evaluate_once()
            dt_ms = (time.monotonic() - t0) * 1000
            st = self.stats
            st["ticks"] += 1
            st["max_tick_ms"] = max(st["max_tick_ms"], dt_ms)
            if dt_ms / 1000.0 > slow_s:
                st["slow_ticks"] += 1
                # 常开告警（不经 env 开关）：静默类缺陷的现场证据，写 stderr
                print(f"[rule-engine WARN] slow tick {dt_ms:.0f}ms > {slow_s:.1f}s "
                      f"fired={fired} stats={st}", file=sys.stderr, flush=True)
            stop.wait(self.poll_s)

    def start_background(self) -> threading.Thread:
        """同进程后台线程跑 watch()（gateway.py mcp --rules 单进程形态用）。"""
        if self._thread and self._thread.is_alive():
            return self._thread
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self.watch, kwargs={"stop": self._stop},
            name="rule-engine", daemon=True)
        self._thread.start()
        return self._thread
