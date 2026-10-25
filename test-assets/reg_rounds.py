#!/usr/bin/env python3
"""BUG-001 回归驱动：触发批次 + 审计实测时延 + miss 即 py-spy 双进程抓栈。

用法（Pi 上）:
  python3 reg_rounds.py --cooldown-probe                        # 冷却序列: 立即触发/8s 挡/31s 过
  python3 reg_rounds.py --rounds 6 --gap 31                     # 基线批次(无负载)
  python3 reg_rounds.py --rounds 6 --gap 31 --flood-seconds 210 # 带 MCP 读泛洪(BUG-001 条件)

触发源: CLI exec set_output on；判定: audit 中 rule-20261024-01 allowed 首现时间；
miss 时 py-spy dump gateway(mcp) 与 virtual_executor 两进程栈到 /tmp/mhsreg/。
每轮结束自动 set_output off 清理。
"""
import argparse, asyncio, json, subprocess, time
from pathlib import Path

HOME = Path.home()
GW_DIR = HOME / "mhs-gateway"
PY = str(GW_DIR / ".venv/bin/python")
GW = str(GW_DIR / "gateway.py")
PYSPY = str(GW_DIR / ".venv/bin/py-spy")
AUDIT = GW_DIR / "audit.jsonl"
OUT = Path("/tmp/mhsreg")
RULE_ID = "rule-20261024-01"


def audit_lines():
    try:
        return AUDIT.read_text(errors="replace").splitlines()
    except OSError:
        return []


def new_fires(base):
    return [ln for ln in audit_lines()[base:]
            if ('"rule_id":"%s"' % RULE_ID) in ln and '"decision":"allowed"' in ln]


def pid_of(pattern):
    r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    pids = [p for p in r.stdout.split() if p.isdigit()]
    return int(pids[0]) if pids else None


def pyspy(pid, path):
    if not pid:
        path.write_text("no pid\n")
        return
    r = subprocess.run([PYSPY, "dump", "--pid", str(pid)],
                       capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        r = subprocess.run(["sudo", "-n", PYSPY, "dump", "--pid", str(pid)],
                           capture_output=True, text=True, timeout=90)
    path.write_text((r.stdout or "") + (r.stderr or "") + "\n")


def exec_output(state):
    r = subprocess.run([PY, GW, "exec", "virtual-actuator-01", "set_output",
                        "--params", json.dumps({"state": state})],
                       capture_output=True, text=True, cwd=str(GW_DIR), timeout=30)
    return r.returncode == 0, (r.stderr or "")[-200:]


async def wait_fire(base, budget):
    t0 = time.monotonic()
    while time.monotonic() - t0 < budget:
        f = new_fires(base)
        if f:
            return time.monotonic() - t0, f[-1]
        await asyncio.sleep(0.05)
    return None, None


async def one_round(idx, a, pyspy_on=True):
    base = len(audit_lines())
    ok, err = exec_output("on")
    if not ok:
        return {"round": idx, "result": "TRIGGER-FAIL", "err": err}
    lat, line = await wait_fire(base, a.budget)
    if lat is None:
        dumps = []
        if pyspy_on:
            for tag, pat in (("gw", "gateway.py mcp"), ("ex", "virtual_executor.py")):
                p = OUT / ("dump_r%d_%s.txt" % (idx, tag))
                pyspy(pid_of(pat), p)
                dumps.append(str(p))
        res = {"round": idx, "result": "MISS", "budget_s": a.budget, "dumps": dumps}
    else:
        res = {"round": idx, "result": "fire", "latency_s": round(lat, 2)}
    exec_output("off")
    return res


async def run_rounds(a):
    OUT.mkdir(exist_ok=True)
    floods = []
    if a.flood_seconds > 0:
        for k in range(1, a.flood_clients + 1):
            fh = open(OUT / ("flood_%d.log" % k), "w")
            floods.append(subprocess.Popen(
                [PY, str(OUT / "mcp_load.py"), "--seconds", str(a.flood_seconds),
                 "--interval-ms", str(a.flood_ms), "--tag", "load%d" % k],
                stdout=fh, stderr=subprocess.STDOUT))
        await asyncio.sleep(2)
    results = []
    for i in range(1, a.rounds + 1):
        t_start = time.monotonic()
        results.append(await one_round(i, a, pyspy_on=a.pyspy))
        elapsed = time.monotonic() - t_start
        if i < a.rounds and elapsed < a.gap:
            await asyncio.sleep(a.gap - elapsed)
    for f in floods:
        try:
            f.wait(timeout=5)
        except Exception:
            f.kill()
    notes = []
    for k in range(1, a.flood_clients + 1):
        p = OUT / ("flood_%d.log" % k)
        if p.exists():
            notes.append(p.read_text().strip())
    return results, notes


async def cooldown_probe(a):
    OUT.mkdir(exist_ok=True)
    a.pyspy = False
    out = [{"phase": "initial-fire(expect)", **(await one_round(1, a))}]
    await asyncio.sleep(8)
    out.append({"phase": "cooldown-block(expect-no-fire)", **(await one_round(2, a))})
    await asyncio.sleep(23)
    out.append({"phase": "post-cooldown(expect-fire)", **(await one_round(3, a))})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--gap", type=float, default=31)
    ap.add_argument("--budget", type=float, default=3.5)
    ap.add_argument("--flood-seconds", type=float, default=0)
    ap.add_argument("--flood-clients", type=int, default=2)
    ap.add_argument("--flood-ms", type=int, default=50)
    ap.add_argument("--cooldown-probe", action="store_true")
    ap.add_argument("--no-pyspy", dest="pyspy", action="store_false")
    a = ap.parse_args()
    if a.cooldown_probe:
        print(json.dumps(asyncio.run(cooldown_probe(a)), ensure_ascii=False, indent=1))
    else:
        results, notes = asyncio.run(run_rounds(a))
        print(json.dumps(results, ensure_ascii=False, indent=1))
        for note in notes:
            print(note)


if __name__ == "__main__":
    main()
