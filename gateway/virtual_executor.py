#!/usr/bin/env python3
"""virtual_executor.py — 虚拟网口执行器（需求 1 的 B 侧替身，规范 §7 实例 2）。

TCP 127.0.0.1:9999，行协议 CMD/ACK 同 MSP-1 风格：
    CMD PING                 → ACK PING {"uptime_ms":..,"fw":"1.0-virtual"}
    CMD GET output           → ACK GET output {"state":"off"}
    CMD SET output <json>    → ACK SET output {"ok":true}
真实网口执行器到位后停掉本进程、换真实设备即可（适配器零改动）。

用法：python3 virtual_executor.py [--port 9999] [--bind 127.0.0.1]
"""
from __future__ import annotations

import argparse
import json
import socket
import socketserver
import threading
import time

STATE = {"output": "off"}
BOOT_TS = time.time()


class Handler(socketserver.StreamRequestHandler):
    def _send(self, s: str):
        self.wfile.write((s + "\r\n").encode())

    def handle(self):
        self._send("MSP1 READY fw=1.0-virtual")
        for raw in self.rfile:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            t0 = time.monotonic()
            try:
                resp = self._dispatch(line)
            except Exception as e:  # noqa: BLE001
                resp = f"ERR ? 04 internal_{type(e).__name__}"
            # 人可读延迟观察（规范响应 ≤500ms）
            latency = int((time.monotonic() - t0) * 1000)
            _ = latency
            self._send(resp)

    def _dispatch(self, line: str) -> str:
        if not line.startswith("CMD"):
            return "ERR ? 01 unknown_cmd"
        rest = line[3:].strip()
        if not rest:
            return "ERR ? 01 unknown_cmd"
        # 两段式命令名（同 MSP-1：GET/SET <资源>）
        if rest.startswith("GET ") or rest.startswith("SET "):
            verb, _, tail = rest.partition(" ")
            target, _, args = tail.partition(" ")
            name = f"{verb} {target}"
        else:
            name, _, args = rest.partition(" ")
        if name == "PING":
            if args.strip():
                return "ERR PING 02 bad_args"
            return f'ACK PING {{"uptime_ms":{int((time.time() - BOOT_TS) * 1000)},"fw":"1.0-virtual"}}'
        if name == "GET output":
            if args.strip():
                return "ERR GET output 02 bad_args"
            return f'ACK GET output {json.dumps({"state": STATE["output"]})}'
        if name == "SET output":
            try:
                p = json.loads(args) if args else {}
            except json.JSONDecodeError:
                return "ERR SET output 02 bad_args"
            state = p.get("state")
            if state not in ("on", "off"):
                return "ERR SET output 02 bad_args"
            STATE["output"] = state
            return 'ACK SET output {"ok":true}'
        return f"ERR {name} 01 unknown_cmd"


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start(port: int = 9999, bind: str = "127.0.0.1", background: bool = False):
    srv = Server((bind, port), Handler)
    if background:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv
    print(f"virtual-actuator-01 listening on {bind}:{port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--bind", default="127.0.0.1")
    a = ap.parse_args()
    # standalone 时挡住重复监听
    try:
        probe = socket.create_connection((a.bind, a.port), timeout=0.3)
        probe.close()
        print(f"端口 {a.port} 已有实例在跑，退出", file=__import__('sys').stderr)
        raise SystemExit(1)
    except OSError:
        pass
    start(a.port, a.bind, background=False)
