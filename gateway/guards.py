#!/usr/bin/env python3
"""guards.py — MHS 网关护栏（规范 §2 分级与护栏基线 + 禁止清单）。

判定顺序（每次动作执行前）：
  1 白名单/禁止清单   1b 参数 schema   2 急停   3 L2 确认   4 前置条件   5 限速/冷却   6 去重/互斥

reason 枚举与审计 §4 对齐：whitelist | schema-invalid | rate-limit | busy |
kill-switch | confirm-required | precondition-fail。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import deque
from typing import Any

# 禁止清单：永不自动执行，写死在网关，不接受描述符覆盖（§2）
FORBIDDEN_ACTIONS = {
    "power_off", "factory_reset", "flash_firmware", "delete_storage",
    "poweroff", "reboot_gateway", "format",
}

DEDUP_WINDOW_MS = 100  # 同设备同一动作 100ms 内去重（§2 并发仲裁）


class Decision:
    __slots__ = ("allowed", "decision", "reason", "detail")

    def __init__(self, decision: str, reason: str, detail: str = ""):
        self.decision = decision          # allowed | denied | deferred
        self.reason = reason              # 审计 reason 枚举
        self.detail = detail
        self.allowed = decision == "allowed"

    def __repr__(self):
        return f"Decision({self.decision}/{self.reason} {self.detail})"


class Guard:
    def __init__(self, descriptors: dict[str, dict]):
        self.descriptors = descriptors
        self._kill = False
        self._rate: dict[tuple, deque] = {}        # (device, action) → 时间戳队列
        self._cooldown: dict[tuple, float] = {}    # (device, action) → 上次执行时刻
        self._dedup: dict[tuple, float] = {}       # (device, action, params_hash) → 时刻
        self._locks: dict[str, threading.Lock] = {}
        self._lk = threading.Lock()
        self._preauth: set[tuple] = set()          # 预授权白名单 {(device, action)}

    # ---- 注册表 ----
    def action_def(self, device_id: str, action: str) -> dict | None:
        d = self.descriptors.get(device_id)
        if not d:
            return None
        for a in d["actions"]:
            if a["name"] == action:
                return a
        return None

    def risk_of(self, device_id: str, action: str) -> str | None:
        a = self.action_def(device_id, action)
        return a["risk"] if a else None

    def _lock_for(self, device_id: str) -> threading.Lock:
        with self._lk:
            if device_id not in self._locks:
                self._locks[device_id] = threading.Lock()
            return self._locks[device_id]

    # ---- 执行成功后记账：限速窗口 + 冷却计时（被拒请求不占配额） ----
    def mark_executed(self, device_id: str, action: str) -> None:
        key = (device_id, action)
        now = time.monotonic()
        self._cooldown[key] = now
        self._rate.setdefault(key, deque()).append(now)

    # ---- 急停 ----
    def set_kill_switch(self, on: bool) -> None:
        self._kill = on

    @property
    def kill_switch(self) -> bool:
        return self._kill

    # ---- L2 预授权 ----
    def set_preauth(self, device_id: str, action: str, on: bool) -> None:
        key = (device_id, action)
        if on:
            self._preauth.add(key)
        else:
            self._preauth.discard(key)

    # ---- 1b 参数 schema 校验（BUG-002：CLI/规则路径此前不校验，非法值直达固件） ----
    def validate_params(self, adef: dict, params: dict | None) -> str | None:
        """按 DCD 参数 schema 校验。返回 None=通过，否则错误描述。

        覆盖：enum / int / float / bool / string+pattern / min / max /
        required / required_if（条件必填）/ 未知参数拒绝。
        confirm 是网关控制参数（L2 确认标记），不属于设备参数，不校验。"""
        ps = adef.get("params") or {}
        got = {k: v for k, v in (params or {}).items() if k != "confirm"}
        for name, spec in ps.items():
            v = got.get(name)
            if v is None:
                if spec.get("required"):
                    return f"缺少必填参数 {name}"
                continue
            t = spec.get("type", "string")
            if t == "enum":
                if v not in (spec.get("values") or []):
                    return f"参数 {name}={v!r} 不在枚举 {spec.get('values')}"
            elif t == "int":
                if isinstance(v, bool) or not isinstance(v, int):
                    return f"参数 {name}={v!r} 应为 int"
            elif t == "float":
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    return f"参数 {name}={v!r} 应为 float"
            elif t == "bool":
                if not isinstance(v, bool):
                    return f"参数 {name}={v!r} 应为 bool"
            else:  # string
                if not isinstance(v, str):
                    return f"参数 {name}={v!r} 应为 string"
                pat = spec.get("pattern")
                if pat and not re.search(pat, v):
                    return f"参数 {name}={v!r} 不匹配 pattern {pat}"
            if t in ("int", "float"):
                if "min" in spec and v < spec["min"]:
                    return f"参数 {name}={v} 低于最小值 {spec['min']}"
                if "max" in spec and v > spec["max"]:
                    return f"参数 {name}={v} 超过最大值 {spec['max']}"
        # required_if：{依赖参数名: [触发值...]} —— 依赖参数取到触发值时本参数必填
        for name, spec in ps.items():
            rif = spec.get("required_if")
            if not rif or got.get(name) is not None:
                continue
            for dep, vals in rif.items():
                if got.get(dep) is not None and got[dep] in vals:
                    return f"参数 {name} 在 {dep}={got[dep]!r} 时必填"
        extras = sorted(set(got) - set(ps))
        if extras:
            return f"未知参数 {extras}"
        return None

    # ---- 第一段：校验（不拿锁；前置条件由 gateway 在此之后、拿锁之前跑） ----
    def check_pre(self, device_id: str, action: str, params: dict | None) -> Decision:
        # 1 白名单 + 禁止清单
        adef = self.action_def(device_id, action)
        if adef is None or action in FORBIDDEN_ACTIONS:
            return Decision("denied", "whitelist",
                            "动作未注册或命中禁止清单" if adef is None else "命中禁止清单")

        # 1b 参数 schema 校验（拒在网关层，不触达设备）
        err = self.validate_params(adef, params)
        if err:
            return Decision("denied", "schema-invalid", err)

        risk = adef["risk"]

        # 2 急停：挂起全部 L1/L2，仅 L0 可用
        if self._kill and risk != "L0":
            return Decision("denied", "kill-switch", "急停生效中")

        # 3 L2：默认需人工确认；预授权白名单放行后才可自动
        if risk == "L2":
            aconfirm = adef.get("confirm", "manual")
            if (device_id, action) not in self._preauth and aconfirm != "preauth":
                if not (params or {}).get("confirm"):
                    return Decision("deferred", "confirm-required",
                                    "L2 动作需人工确认（confirm=true 或预授权）")

        # 5 限速 / 冷却
        rl = adef.get("rate_limit") or {}
        now = time.monotonic()
        key = (device_id, action)
        maxpm = rl.get("max_per_min")
        if maxpm:
            q = self._rate.setdefault(key, deque())
            while q and now - q[0] > 60.0:
                q.popleft()
            if len(q) >= maxpm:
                return Decision("denied", "rate-limit",
                                f"超过 {maxpm} 次/分钟限速")
        cd = rl.get("cooldown_ms")
        if cd:
            last = self._cooldown.get(key)
            if last is not None and (now - last) * 1000 < cd:
                return Decision("denied", "rate-limit",
                                f"冷却中（{cd}ms）")

        # 6a 去重：同设备同动作同参数 100ms 内 → busy
        ph = hashlib.sha256(
            json.dumps(params or {}, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
        dkey = (device_id, action, ph)
        last = self._dedup.get(dkey)
        if last is not None and (now - last) * 1000 < DEDUP_WINDOW_MS:
            return Decision("denied", "busy", "100ms 内重复请求")
        self._dedup[dkey] = now

        return Decision("allowed", "ok")

    # ---- 第二段：拿设备互斥锁（先到先得，拿不到 → busy） ----
    def acquire(self, device_id: str, action: str) -> "_HeldDecision | Decision":
        lock = self._lock_for(device_id)
        if not lock.acquire(blocking=False):
            return Decision("denied", "busy", "设备动作互斥锁被占")
        adef = self.action_def(device_id, action) or {}
        return _HeldDecision(lock, adef, adef.get("risk", "?"))


class _HeldDecision(Decision):
    """校验通过时返回：持锁，执行完必须调用 release()（gateway 在 finally 里做）。"""

    def __init__(self, lock: threading.Lock, adef: dict, risk: str):
        super().__init__("allowed", "ok")
        self._lock = lock
        self.adef = adef
        self.risk = risk

    def release(self) -> None:
        self._lock.release()
