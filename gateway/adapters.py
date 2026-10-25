#!/usr/bin/env python3
"""adapters.py — 设备适配器层（铁律「薄」：只做协议↔动作的翻译，无业务逻辑）。

  serial-msp1   → ESP32-S3 固件 v1.1（USB CDC，MSP-1 行协议，规范 §6）
  net-tcp-json  → 虚拟网口执行器（TCP 行协议 CMD/ACK 同 MSP-1 风格，规范 §7）

所有方法超时必返回（明确失败），不挂死——FR-1/FR-2 的可测性要求。
"""
from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any


class AdapterError(Exception):
    def __init__(self, msg: str, kind: str = "error"):  # kind: timeout | error
        super().__init__(msg)
        self.kind = kind


class _LineClient:
    """CMD/ACK/ERR 行协议公共实现（串口与 TCP 共用）。"""

    def __init__(self, timeout_ms: int = 800):
        self.timeout_ms = timeout_ms
        self._io = None
        self._lk = threading.Lock()

    # ---- 由子类实现 ----
    def _open(self):  # noqa: ANN202
        raise NotImplementedError

    def _close(self):  # noqa: ANN202
        raise NotImplementedError

    def _write_line(self, line: str):  # noqa: ANN202
        raise NotImplementedError

    def _read_line(self, timeout_s: float) -> str:  # noqa: ANN202
        raise NotImplementedError

    # ---- 公共 ----
    def _ensure_open(self):
        if self._io is None:
            self._open()
        return self._io

    def _invalidate(self):
        # 竞态安全：先换下引用再关旧连接（可能被并发 invalidate/reopen 抢先换掉）
        io, self._io = self._io, None
        self._buf = b""
        if io is not None:
            try:
                self._shutdown(io)
            except Exception:  # noqa: BLE001
                pass

    def _shutdown(self, io):  # noqa: ANN202 由子类实现：关指定连接
        raise NotImplementedError

    def command(self, name: str, args: dict | None = None,
                timeout_ms: int | None = None) -> dict:
        """发送 CMD，成功返回 ACK 的 json-result；ERR/超时抛 AdapterError。

        超时自动重试一次（重开连接）：吸收串口/CDC 打开瞬态（ESP32-S3 原生
        USB 打开后首命令易丢）。设备明确回 ERR 不重试——那是协议层拒绝。"""
        try:
            return self._command_once(name, args, timeout_ms)
        except AdapterError as e:
            if e.kind != "timeout":
                raise
            self._invalidate()
            return self._command_once(name, args, timeout_ms)

    def _command_once(self, name: str, args: dict | None = None,
                      timeout_ms: int | None = None) -> dict:
        tmo = (timeout_ms or self.timeout_ms) / 1000.0
        line = f"CMD {name}" + (f" {json.dumps(args, separators=(',', ':'))}" if args else "")
        with self._lk:
            try:
                self._ensure_open()
                self._write_line(line)
                deadline = time.monotonic() + tmo
                while True:
                    remain = deadline - time.monotonic()
                    if remain <= 0:
                        self._invalidate()
                        raise AdapterError(f"{name}: 响应超时（{int(tmo * 1000)}ms）", "timeout")
                    resp = self._read_line(remain).strip()
                    if not resp:
                        continue
                    if resp.startswith("MSP1 READY") or resp.startswith("#"):
                        continue  # 横幅/注释行跳过
                    if resp.startswith("ACK "):
                        rest = resp[4:]
                        if rest == name or rest.startswith(name + " "):
                            payload = rest[len(name):].strip()
                            return json.loads(payload) if payload else {}
                        raise AdapterError(f"{name}: ACK 名不匹配：{resp}", "error")
                    if resp.startswith("ERR "):
                        # ERR <name(可含空格)> <两位码> <msg...>：找第一个两位数字码切分
                        parts = resp.split(" ")
                        code_i = next((i for i, t in enumerate(parts[1:], 1)
                                       if len(t) == 2 and t.isdigit()), None)
                        if code_i is None:
                            raise AdapterError(f"{name}: ERR {resp}", "error")
                        ename = " ".join(parts[1:code_i])
                        code = parts[code_i]
                        msg = " ".join(parts[code_i + 1:])
                        raise AdapterError(f"{ename}: ERR {code} {msg}")
                    # 非协议行（设备调试输出），忽略继续读
            except AdapterError:
                raise
            except Exception as e:  # noqa: BLE001 跨线程 invalidate 竞态（AttributeError/None io）等
                self._invalidate()  # 一律按可重试通信失败，不让异常逃出护栏层
                raise AdapterError(f"{name}: 通信失败（{type(e).__name__}: {e}）") from e

    def ping(self, timeout_ms: int = 500) -> dict:
        return self.command("PING", None, timeout_ms)


class SerialMSP1Adapter(_LineClient):
    """ESP32-S3（/dev/ttyACM0）固件 v1.1 MSP-1。"""

    ACTION_CMD = {
        "ping":       lambda p: ("PING", None),
        "read_temp":  lambda p: ("GET chip_temp_c", None),
        "set_led":    lambda p: ("SET led", p),
        "reset":      lambda p: ("RESET", None),
    }
    RESOURCE_CMD = {
        "chip_temp_c": ("GET chip_temp_c", "chip_temp_c"),
        "led_mode":    ("GET led", "mode"),
        "led_color":   ("GET led", "color"),
    }

    def __init__(self, port: str, baud: int = 115200, timeout_ms: int = 800):
        super().__init__(timeout_ms)
        self.port, self.baud = port, baud

    def _open(self):
        import serial  # pyserial
        self._io = serial.Serial(self.port, self.baud, timeout=0.1)
        # ESP32-S3 原生 USB-CDC：固定 DTR/RTS 电平，防止打开瞬间的电平漂移
        # 触发复位（团队摸底口径：主机侧需显式拉 DTR+RTS）；并留 CDC 就绪时间
        try:
            self._io.dtr = True
            self._io.rts = False
        except Exception:  # noqa: BLE001 非 CDC 串口无此属性
            pass
        time.sleep(0.08)
        self._io.reset_input_buffer()
        self._buf = b""                       # 跨调用持久缓冲

    def _close(self):
        self._buf = b""
        if self._io is not None:
            self._io.close()

    def _shutdown(self, io):
        io.close()

    def _write_line(self, line: str):
        self._io.reset_input_buffer()
        self._buf = b""
        self._io.write((line + "\n").encode())
        self._io.flush()

    def _read_line(self, timeout_s: float) -> str:
        deadline = time.monotonic() + timeout_s
        while True:
            if b"\n" in self._buf:
                line, _, self._buf = self._buf.partition(b"\n")
                return line.decode(errors="replace")
            remain = deadline - time.monotonic()
            if remain <= 0:
                self._invalidate()
                raise AdapterError("串口读取超时", "timeout")
            n = self._io.in_waiting
            if n:
                self._buf += self._io.read(n)
            else:
                time.sleep(0.005)

    # ---- 动作/资源映射 ----
    def execute(self, action: str, params: dict | None, timeout_ms: int) -> dict:
        if action not in self.ACTION_CMD:
            raise AdapterError(f"适配器不支持动作：{action}")
        name, args = self.ACTION_CMD[action](params or {})
        return self.command(name, args, timeout_ms)

    def read_resource(self, name: str) -> Any:
        if name == "fw_version":
            return self.ping().get("fw")
        if name not in self.RESOURCE_CMD:
            raise AdapterError(f"适配器不支持资源读取：{name}")
        cmd, field = self.RESOURCE_CMD[name]
        return self.command(cmd, None).get(field)


class NetTcpJsonAdapter(_LineClient):
    """虚拟网口执行器（127.0.0.1:9999）；真实网口设备到位后仅换映射。"""

    ACTION_CMD = {
        "ping":       lambda p: ("PING", None),
        "set_output": lambda p: ("SET output", p),
    }
    RESOURCE_CMD = {
        "output": ("GET output", "state"),
    }

    def __init__(self, host: str, port: int, timeout_ms: int = 800):
        super().__init__(timeout_ms)
        self.host, self.port = host, port

    def _open(self):
        self._io = socket.create_connection((self.host, self.port), timeout=1.0)
        self._io.settimeout(0.1)
        self._buf = b""                       # 跨调用持久缓冲（防整包多行被截断）

    def _close(self):
        self._buf = b""
        if self._io is not None:
            try:
                self._io.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._io.close()

    def _shutdown(self, io):
        try:
            io.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        io.close()

    def _write_line(self, line: str):
        self._io.sendall((line + "\n").encode())

    def _read_line(self, timeout_s: float) -> str:
        deadline = time.monotonic() + timeout_s
        while True:
            if b"\n" in self._buf:
                line, _, self._buf = self._buf.partition(b"\n")
                return line.decode(errors="replace")
            remain = deadline - time.monotonic()
            if remain <= 0:
                self._invalidate()
                raise AdapterError("TCP 读取超时", "timeout")
            self._io.settimeout(min(0.1, remain))
            try:
                chunk = self._io.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                self._invalidate()
                raise AdapterError("对端断开", "error")
            self._buf += chunk

    def execute(self, action: str, params: dict | None, timeout_ms: int) -> dict:
        if action not in self.ACTION_CMD:
            raise AdapterError(f"适配器不支持动作：{action}")
        name, args = self.ACTION_CMD[action](params or {})
        return self.command(name, args, timeout_ms)

    def read_resource(self, name: str) -> Any:
        if name not in self.RESOURCE_CMD:
            raise AdapterError(f"适配器不支持资源读取：{name}")
        cmd, field = self.RESOURCE_CMD[name]
        return self.command(cmd, None).get(field)


ADAPTERS = {
    "serial-msp1": SerialMSP1Adapter,
    "net-tcp-json": NetTcpJsonAdapter,
}


def build_adapter(dcd: dict) -> _LineClient:
    """按描述符 device.adapter/transport 构造适配器实例。"""
    name = dcd["device"]["adapter"]
    cls = ADAPTERS.get(name)
    if cls is None:
        raise AdapterError(f"未注册的适配器：{name}")
    tr = dcd["device"]["transport"]
    if name == "serial-msp1":
        s = tr["serial"]
        return cls(port=s["port"], baud=s.get("baud", 115200))
    n = tr["network"]
    return cls(host=n["host"], port=n["port"])
