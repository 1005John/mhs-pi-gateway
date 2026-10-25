#!/usr/bin/env python3
"""mcp_server.py — MHS 网关 MCP server（规范 FR-3：tools/resources 由描述符生成）。

  tool 名      = <device.id>.<action.name>     （§1 MCP 映射）
  resource URI = mhs://<device.id>/<resource.name>

tools 的输入 schema 直接由描述符 params 生成（enum→Literal、pattern/min/max→
Field 约束、default→默认值）——「一切是数据」，客户端看到的 schema 与描述符
一字不差。L2 动作额外暴露 confirm: bool（人工确认标记）。

传输：stdio（默认，客户端拉起，适合 SSH 隧道）或 streamable-http --port
（局域网调试；AC-1 口径）。模型无关：任何 MCP 客户端调用结果一致。
"""
from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import Field
from mcp.server.mcpserver import MCPServer

from rules import RuleError

SERVER_NAME = "mhs-gateway"


def _py_type(pspec: dict) -> Any:
    """描述符参数定义 → Python 类型注解（pydantic 可生成 JSON schema）。"""
    t = pspec.get("type", "string")
    if t is bool or t == "bool":
        return bool
    if t == "enum":
        vals = tuple(pspec.get("values", []))
        return Literal[vals] if vals else str  # type: ignore[valid-type]
    base = {"int": int, "float": float, "bool": bool}.get(t, str)
    meta: dict[str, Any] = {}
    if "pattern" in pspec:
        meta["pattern"] = pspec["pattern"]
    if "min" in pspec:
        meta["ge"] = pspec["min"]
    if "max" in pspec:
        meta["le"] = pspec["max"]
    return Annotated[base, Field(**meta)] if meta else base


def make_tool_function(tname: str, params_spec: dict, extra_params: dict,
                       run: Any) -> Any:
    """按描述符参数动态生成带真实签名的 tool 函数（pydantic 可内省）。

    params_spec：描述符里的 params；extra_params：附加形参（L2 的 confirm）。
    run：callable(**kwargs) -> str，真正调用网关。
    """
    spec = dict(params_spec)
    spec.update(extra_params)
    ns: dict[str, Any] = {"__builtins__": {}, "_run": run,
                          "Annotated": Annotated, "Field": Field,
                          "Literal": Literal}
    args_src = []
    for i, (pname, ps) in enumerate(spec.items()):
        ns[f"_t{i}"] = _py_type(ps)
        if ps.get("required"):
            args_src.append(f"{pname}: _t{i}")
        elif "default" in ps:
            ns[f"_d{i}"] = ps["default"]
            args_src.append(f"{pname}: _t{i} = _d{i}")
        else:
            args_src.append(f"{pname}: _t{i} | None = None")
    kw = ", ".join(f"'{p}': {p}" for p in spec)
    src = f"def _fn({', '.join(args_src)}):\n    return _run(**{{{kw}}})"
    exec(src, ns)  # noqa: S102 —— 参数名来自描述符（NAME_RE 已约束），无注入面
    fn = ns["_fn"]
    fn.__name__ = tname.replace(".", "_").replace("-", "_")
    return fn


def build(gw, engine=None) -> MCPServer:
    """engine：RuleEngine 实例（gateway.py mcp --rules 单进程形态传入）。
    传入后额外暴露 gateway.rule_set_status（规则状态热切换，走 overlay 文件）。"""
    mcp = MCPServer(
        SERVER_NAME,
        instructions=(
            "MHS 树莓派网关：设备能力来自描述符（DCD），动作=tools，状态=resources。"
            "L2 动作需显式传 confirm=true 表示人工确认；参数 schema 即描述符定义。"
        ),
    )

    def _j(x) -> str:
        return json.dumps(x, ensure_ascii=False)

    # ---- tools：每个描述符动作一个 tool，schema 从描述符生成 ----
    for dev_id, dcd in gw.descriptors.items():
        for a in dcd["actions"]:
            tname = f"{dev_id}.{a['name']}"
            extra = {"confirm": {"type": bool}} if a["risk"] == "L2" else {}
            param_docs = "; ".join(
                f"{p}({s.get('type')}"
                f"{', 取值 ' + str(s['values']) if 'values' in s else ''}"
                f"{', 必填' if s.get('required') else ''})"
                for p, s in (a.get("params") or {}).items()
            ) or "无参数"
            desc = (f"[{a['risk']}] {dcd['device']['name']} · {a['name']}。"
                    f"参数：{param_docs}。"
                    + ("L2 高风险：须人工确认（confirm=true）。" if a["risk"] == "L2" else ""))

            def make_run(dev_id=dev_id, action=a["name"]):
                def run(**kwargs) -> str:
                    r = gw.execute(dev_id, action, kwargs, actor="cloud-agent")
                    return _j(r)
                return run

            fn = make_tool_function(tname, a.get("params") or {}, extra, make_run())
            fn.__doc__ = desc
            mcp.tool(name=tname, description=desc)(fn)

    # ---- resources：每个描述符资源一个只读资源 ----
    for dev_id, dcd in gw.descriptors.items():
        for r in dcd.get("resources", []):
            uri = f"mhs://{dev_id}/{r['name']}"
            rdesc = f"[{r.get('access', 'ro')}] {r.get('desc', r['name'])}"

            def make_res(dev_id=dev_id, name=r["name"]):
                def read() -> str:
                    try:
                        return _j({"device": dev_id, "resource": name,
                                   "value": gw.read_resource(dev_id, name)})
                    except Exception as e:  # noqa: BLE001 设备离线也返回结构化错误
                        return _j({"device": dev_id, "resource": name,
                                   "error": str(e)})
                read.__doc__ = rdesc
                return read

            mcp.resource(uri, name=uri, description=rdesc)(make_res())

    # ---- 网关管理 tools（前缀 gateway.*；审计 actor=cloud-agent/device_id=gateway/risk=L1） ----
    def _run_kill_switch(state: str) -> str:
        gw.set_kill_switch(state == "on")
        ev = gw.audit.log(
            actor="cloud-agent", rule_id=None, device_id="gateway",
            action="kill_switch", params={"state": state}, risk="L1",
            decision="allowed", reason="whitelist", result="ok", latency_ms=0)
        return _j({"kill_switch": state, "audit_event_id": ev["event_id"]})

    fn = make_tool_function(
        "gateway.kill_switch",
        {"state": {"type": "enum", "values": ["on", "off"], "required": True}},
        {}, _run_kill_switch)
    fn.__doc__ = ("急停开关（FR-6.3）：on=挂起全部 L1/L2 仅留 L0，off=恢复。"
                  "写 .killswitch 文件，常驻进程 0.5s 内生效（跨进程同步）。")
    mcp.tool(name="gateway.kill_switch", description=fn.__doc__)(fn)

    def _run_rule_set_status(rule_id: str, status: str) -> str:
        if engine is None:
            return _j({"error": "规则引擎未启用：请以 gateway.py mcp --rules rules.yaml 启动"})
        try:
            r = engine.set_rule_status(rule_id, status)
        except RuleError as e:
            return _j({"error": str(e)})
        gw.audit.log(
            actor="cloud-agent", rule_id=rule_id, device_id="gateway",
            action="rule_set_status", params={"status": status}, risk="L1",
            decision="allowed", reason="whitelist", result="ok", latency_ms=0)
        return _j(r)

    fn = make_tool_function(
        "gateway.rule_set_status",
        {"rule_id": {"type": "string", "required": True},
         "status": {"type": "enum",
                    "values": ["draft", "testing", "active", "disabled"],
                    "required": True}},
        {}, _run_rule_set_status)
    fn.__doc__ = ("改规则生命周期状态（FR-6.2）：draft/testing/active/disabled。"
                  "状态写 <rules>.status.json overlay 文件（热生效，不动 rules.yaml）。")
    mcp.tool(name="gateway.rule_set_status", description=fn.__doc__)(fn)

    # ---- 网关管理 resources ----
    def _read_audit_recent() -> str:
        return _j({"device": "gateway", "resource": "audit/recent",
                   "events": gw.audit.tail(50)})
    _read_audit_recent.__doc__ = "[ro] 审计日志最近 50 条（AC-4 云端核查入口）"
    mcp.resource("mhs://gateway/audit/recent", name="mhs://gateway/audit/recent",
                 description=_read_audit_recent.__doc__)(_read_audit_recent)

    return mcp


def serve(gw, transport: str = "stdio", port: int = 8808, engine=None) -> None:
    mcp = build(gw, engine=engine)
    if transport in ("http", "streamable-http"):
        # 局域网调试口径（AC-1）；公网暴露不在本期（OQ-4 定隧道，不裸暴露）
        mcp.run("streamable-http", host="0.0.0.0", port=port)
    else:
        mcp.run("stdio")
