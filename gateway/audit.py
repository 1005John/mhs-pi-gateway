#!/usr/bin/env python3
"""audit.py — MHS 网关审计日志（规范 §4：每条动作一行 JSON，AC-4 三要素 + 耗时）。

字段（与规范 §4 一致，不增不减）：
  ts, event_id, actor, rule_id, device_id, action, params,
  risk, decision(allowed|denied|deferred), reason(whitelist|rate-limit|busy|
  kill-switch|confirm-required|precondition-fail|schema-invalid),
  result(ok|timeout|error|denied),
  latency_ms

断网补传：本地文件始终可写；云端推送失败的事件进内存缓存，恢复后 flush() 重推
（一期云端订阅未接，缓存接口先行，行为与「恢复补传」口径对齐）。
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TZ = timezone(timedelta(hours=8))  # +08:00

REASONS = {"whitelist", "rate-limit", "busy", "kill-switch",
           "confirm-required", "precondition-fail", "schema-invalid"}
DECISIONS = {"allowed", "denied", "deferred"}
RESULTS = {"ok", "timeout", "error", "denied"}


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


class Audit:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cloud_cache: list[dict] = []   # 云端不可达时的事件缓存

    def log(self, *, actor: str, rule_id: str | None, device_id: str,
            action: str, params: dict, risk: str, decision: str, reason: str,
            result: str, latency_ms: int) -> dict:
        event = {
            "ts": now_iso(),
            "event_id": uuid.uuid4().hex[:8],
            "actor": actor,                 # local-rule | cloud-agent | manual
            "rule_id": rule_id,
            "device_id": device_id,
            "action": action,
            "params": params or {},
            "risk": risk,
            "decision": decision,
            "reason": reason,
            "result": result,
            "latency_ms": latency_ms,
        }
        if decision not in DECISIONS:
            raise ValueError(f"decision 非法：{decision}")
        if reason not in REASONS:
            raise ValueError(f"reason 非法：{reason}")
        if result not in RESULTS:
            raise ValueError(f"result 非法：{result}")
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            self._cloud_cache.append(event)  # 磁盘异常也不丢事件
        return event

    # ---- 云端补传接口（一期云端订阅未接，先留桩） ----
    def push_to_cloud(self, events: list[dict]) -> int:
        """把缓存事件推给云端；成功返回条数。一期返回 0 表示云端未接。"""
        return 0  # TODO(M4 公网/隧道接通后替换为真实推送)

    def flush_cloud_cache(self) -> int:
        if not self._cloud_cache:
            return 0
        n = self.push_to_cloud(list(self._cloud_cache))
        if n:
            del self._cloud_cache[:n]
        return len(self._cloud_cache)

    def tail(self, n: int = 20) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as f:
            lines = f.readlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]

    def query(self, **filt: Any) -> list[dict]:
        """按字段过滤审计（如 device_id=... action=...）。"""
        out = []
        if not self.path.exists():
            return out
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                ev = json.loads(line)
                if all(ev.get(k) == v for k, v in filt.items()):
                    out.append(ev)
        return out
