#!/usr/bin/env python3
"""dcd_loader.py — MHS 网关设备能力描述符（DCD）加载与校验。

规范依据：《MHS-网关能力规范-v0.9》§1（字段规则）/ §7（实例）/ AC-0。
铁律「一切是数据」：描述符是唯一事实源，MCP tools/resources 由它生成；
本模块只做「加载 + 校验 + 命名映射」，不含任何设备业务逻辑。

用法：
    python3 dcd_loader.py descriptors/*.yaml        # 校验模式，全部合法 exit 0
    python3 -c "from dcd_loader import load_file; d = load_file('...')"
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

SPEC_VERSION = "0.9"
CATEGORIES = {"usb-serial", "network", "virtual"}
RISKS = {"L0", "L1", "L2"}
PARAM_TYPES = {"enum", "string", "int", "float", "bool"}
CONFIRMS = {"manual", "preauth"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


class DcdError(Exception):
    """描述符非法；message 里带字段路径，可直接打印给调用方。"""


def _fail(path: str, msg: str) -> None:
    raise DcdError(f"{path}: {msg}")


def _req(d: dict, key: str, path: str, types: type | tuple, what: str):
    if key not in d:
        _fail(path, f"缺少必填字段 {key}")
    v = d[key]
    if not isinstance(v, types):
        _fail(path, f"{key} 类型应为 {what}")
    return v


def _validate_param(pname: str, ps: Any, path: str) -> None:
    ppath = f"{path}.params.{pname}"
    if not isinstance(ps, dict):
        _fail(ppath, "参数定义应为映射")
    t = _req(ps, "type", ppath, str, "字符串")
    if t not in PARAM_TYPES:
        _fail(ppath, f"type 非法：{t}（允许 {sorted(PARAM_TYPES)}）")
    if t == "enum":
        values = _req(ps, "values", ppath, list, "数组")
        if not values:
            _fail(ppath, "enum 的 values 不能为空")
    if "pattern" in ps and t == "string":
        try:
            re.compile(ps["pattern"])
        except re.error as e:
            _fail(ppath, f"pattern 非法正则：{e}")
    if "min" in ps and "max" in ps and ps["min"] > ps["max"]:
        _fail(ppath, "min 不能大于 max")
    if "required_if" in ps and not isinstance(ps["required_if"], dict):
        _fail(ppath, "required_if 应为 {参数名: [取值]}")
    if "default" in ps and "values" in ps and ps["default"] not in ps["values"]:
        _fail(ppath, "default 不在 enum values 内")


def validate(dcd: dict, path: str = "<memory>") -> dict:
    """校验一份描述符（dict），非法抛 DcdError，合法原样返回。"""
    if not isinstance(dcd, dict):
        _fail(path, "描述符顶层应为映射")

    # -- 顶层 --
    sv = _req(dcd, "spec_version", path, str, "字符串")
    if sv != SPEC_VERSION:
        _fail(path, f"spec_version 应为 {SPEC_VERSION}，得到 {sv}")

    # -- device --
    dev = _req(dcd, "device", path, dict, "映射")
    dpath = f"{path}.device"
    dev_id = _req(dev, "id", dpath, str, "字符串")
    if not ID_RE.match(dev_id):
        _fail(dpath, f"id 非法（小写字母数字连字符）：{dev_id}")
    _req(dev, "name", dpath, str, "字符串")
    cat = _req(dev, "category", dpath, str, "字符串")
    if cat not in CATEGORIES:
        _fail(dpath, f"category 非法：{cat}（允许 {sorted(CATEGORIES)}）")
    _req(dev, "adapter", dpath, str, "字符串")
    tr = _req(dev, "transport", dpath, dict, "映射")
    if "serial" in tr:
        st = tr["serial"]
        if not isinstance(st, dict) or "port" not in st or "baud" not in st:
            _fail(f"{dpath}.transport.serial", "需要 {port, baud}")
    elif "network" in tr:
        nt = tr["network"]
        if not isinstance(nt, dict) or "host" not in nt or "port" not in nt:
            _fail(f"{dpath}.transport.network", "需要 {host, port}")
    else:
        _fail(f"{dpath}.transport", "需要 serial 或 network 之一")

    # -- resources --
    resources = dcd.get("resources", [])
    if not isinstance(resources, list) or not resources:
        _fail(f"{path}.resources", "至少要有一个资源")
    rnames = set()
    for i, r in enumerate(resources):
        rpath = f"{path}.resources[{i}]"
        if not isinstance(r, dict):
            _fail(rpath, "资源应为映射")
        rn = _req(r, "name", rpath, str, "字符串")
        if not NAME_RE.match(rn):
            _fail(rpath, f"name 非法：{rn}")
        if rn in rnames:
            _fail(rpath, f"资源名重复：{rn}")
        rnames.add(rn)
        rt = _req(r, "type", rpath, str, "字符串")
        if rt not in PARAM_TYPES:
            _fail(rpath, f"type 非法：{rt}")
        if rt == "enum" and not r.get("values"):
            _fail(rpath, "enum 资源必须有 values")
        access = _req(r, "access", rpath, str, "字符串")
        if access not in ("ro", "rw"):
            _fail(rpath, f"access 非法：{access}")

    # -- actions --
    actions = _req(dcd, "actions", path, list, "数组")
    if not actions:
        _fail(f"{path}.actions", "至少要有一个动作")
    anames = set()
    for i, a in enumerate(actions):
        apath = f"{path}.actions[{i}]"
        if not isinstance(a, dict):
            _fail(apath, "动作应为映射")
        an = _req(a, "name", apath, str, "字符串")
        if not NAME_RE.match(an):
            _fail(apath, f"name 非法：{an}")
        if an in anames:
            _fail(apath, f"动作名重复：{an}")
        anames.add(an)
        risk = _req(a, "risk", apath, str, "字符串")
        if risk not in RISKS:
            _fail(apath, f"risk 非法：{risk}（允许 {sorted(RISKS)}）")
        params = a.get("params", {})
        if not isinstance(params, dict):
            _fail(apath, "params 应为映射")
        for pname, ps in params.items():
            if not NAME_RE.match(pname):
                _fail(apath, f"参数名非法：{pname}")
            _validate_param(pname, ps, apath)
        if "timeout_ms" in a and (not isinstance(a["timeout_ms"], int) or a["timeout_ms"] <= 0):
            _fail(apath, "timeout_ms 必须为正整数")
        if "rate_limit" in a:
            rl = a["rate_limit"]
            if not isinstance(rl, dict) or not isinstance(rl.get("max_per_min"), int) \
                    or rl["max_per_min"] <= 0:
                _fail(f"{apath}.rate_limit", "需要正整数 max_per_min")
            if "cooldown_ms" in rl and (not isinstance(rl["cooldown_ms"], int)
                                        or rl["cooldown_ms"] < 0):
                _fail(f"{apath}.rate_limit.cooldown_ms", "必须为非负整数")
        if risk == "L2":
            confirm = a.get("confirm")
            if confirm not in CONFIRMS:
                _fail(apath, f"L2 动作必须声明 confirm ∈ {sorted(CONFIRMS)}（护栏基线）")
        for pre in a.get("preconditions", []):
            if pre not in anames:
                _fail(apath, f"preconditions 引用未定义动作：{pre}")

    # -- rw 资源支撑检查（§1 字段规则）--
    # 满足其一即可：① 某动作存在同名参数；② 存在 set* 动作
    # （适配器约定：GET/SET 类命令支撑 rw 资源，如 SET led 支撑 led_mode/led_color）。
    set_actions = [a for a in actions if a["name"].startswith("set")]
    for r in resources:
        if r.get("access") != "rw":
            continue
        backed = any(r["name"] in (a.get("params") or {}) for a in actions) or set_actions
        if not backed:
            _fail(f"{path}.resources(name={r['name']})",
                  "rw 资源缺少 action 或 GET/SET 类命令支撑（§1 字段规则）")

    # -- health / meta --
    health = dcd.get("health", {})
    if health:
        probe = health.get("probe", {})
        if not isinstance(probe, dict) or probe.get("method") != "ping":
            _fail(f"{path}.health.probe", "一期 probe.method 仅支持 ping")
        if not isinstance(probe.get("interval_s", 0), int) or probe.get("interval_s", 0) <= 0:
            _fail(f"{path}.health.probe", "interval_s 必须为正整数")
        if not isinstance(probe.get("miss_threshold", 0), int) or probe.get("miss_threshold", 0) <= 0:
            _fail(f"{path}.health.probe", "miss_threshold 必须为正整数")
    meta = dcd.get("meta", {})
    if meta.get("descriptor_version") != 1:
        _fail(f"{path}.meta", "descriptor_version 必须为 1")

    return dcd


def _parse_file(p: Path) -> dict:
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        data = yaml.safe_load(text)
    elif p.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        raise DcdError(f"{p}: 不支持的文件后缀（用 .yaml/.yml/.json）")
    return data


def load_file(path: str | Path) -> dict:
    """加载并校验单个描述符文件，返回校验后的 dict。"""
    p = Path(path)
    if not p.is_file():
        raise DcdError(f"{p}: 文件不存在")
    return validate(_parse_file(p), path=str(p))


def load_dir(path: str | Path) -> dict[str, dict]:
    """加载目录下全部描述符，返回 {device.id: descriptor}；任一非法即抛错。"""
    out: dict[str, dict] = {}
    files = sorted(Path(path).glob("*.yaml")) + sorted(Path(path).glob("*.yml")) \
        + sorted(Path(path).glob("*.json"))
    if not files:
        raise DcdError(f"{path}: 目录下没有描述符文件")
    for f in files:
        dcd = load_file(f)
        dev_id = dcd["device"]["id"]
        if dev_id in out:
            raise DcdError(f"{f}: 设备 id 重复：{dev_id}")
        out[dev_id] = dcd
    return out


# ---------- MCP 命名映射（§1：tool 名 = <device.id>.<action.name>；resource 名 = <device.id>/<resource.name>） ----------

def tool_name(dcd: dict, action: dict) -> str:
    return f"{dcd['device']['id']}.{action['name']}"


def resource_name(dcd: dict, resource: dict) -> str:
    return f"{dcd['device']['id']}/{resource['name']}"


def tools_of(dcd: dict) -> dict[str, dict]:
    """{tool 名: 动作定义}，供 MCP server 生成 tools 用。"""
    return {tool_name(dcd, a): a for a in dcd["actions"]}


def resources_of(dcd: dict) -> dict[str, dict]:
    """{resource 名: 资源定义}。"""
    return {resource_name(dcd, r): r for r in dcd.get("resources", [])}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    rc = 0
    for arg in sys.argv[1:]:
        try:
            d = load_file(arg)
            print(f"OK   {arg}: device={d['device']['id']} "
                  f"resources={len(d.get('resources', []))} actions={len(d['actions'])}")
        except DcdError as e:
            print(f"FAIL {e}")
            rc = 1
    sys.exit(rc)
