#!/usr/bin/env python3
"""回归 fixture：T2-3 防抖 / T2-6 verify 失败重试（独立 rules-run 进程 + 共享审计判定）。

用法:
  python3 reg_fixture.py --mode debounce --file reg-fixture-debounce.yaml --rule-id reg-debounce-01
  python3 reg_fixture.py --mode verify   --file reg-fixture-verify.yaml   --rule-id reg-verifyfail-01
判定全部来自 audit.jsonl（rule_id 过滤），进程收尾自动清理 output=off。
"""
import argparse, asyncio, json, subprocess, time
from pathlib import Path

HOME = Path.home()
GW_DIR = HOME / "mhs-gateway"
PY = str(GW_DIR / ".venv/bin/python")
GW = str(GW_DIR / "gateway.py")
AUDIT = GW_DIR / "audit.jsonl"
OUT = Path("/tmp/mhsreg")


def audit_lines():
    try:
        return AUDIT.read_text(errors="replace").splitlines()
    except OSError:
        return []


def fires_for(base, rule_id):
    return [ln for ln in audit_lines()[base:]
            if ('"rule_id":"%s"' % rule_id) in ln and '"decision":"allowed"' in ln]


def exec_output(state):
    r = subprocess.run([PY, GW, "exec", "virtual-actuator-01", "set_output",
                        "--params", json.dumps({"state": state})],
                       capture_output=True, text=True, cwd=str(GW_DIR), timeout=30)
    return r.returncode == 0


def start_engine(log_name, rule_file):
    log = open(OUT / log_name, "w")
    proc = subprocess.Popen([PY, GW, "rules-run", "--file", rule_file],
                            cwd=str(GW_DIR), stdout=log, stderr=subprocess.STDOUT)
    return proc


def stop_engine(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


async def run_debounce(a):
    proc = start_engine("fixture_debounce_run.log", a.file)
    await asyncio.sleep(1.5)
    base = len(audit_lines())
    t0 = time.monotonic()
    if not exec_output("on"):
        stop_engine(proc)
        return {"error": "trigger-exec-failed"}
    await asyncio.sleep(max(0.0, a.for_s - 0.4))
    early = fires_for(base, a.rule_id)
    fire_at = None
    while time.monotonic() - t0 < a.for_s + 3:
        if fires_for(base, a.rule_id):
            fire_at = time.monotonic() - t0
            break
        await asyncio.sleep(0.05)
    exec_output("off")
    await asyncio.sleep(1)
    stop_engine(proc)
    return {"early_fires": len(early), "checked_before_s": round(a.for_s - 0.4, 1),
            "fire_at_s": round(fire_at, 2) if fire_at else None,
            "expect": "no fire before for_s, fire in [for_s-0.3, for_s+3]",
            "pass": (len(early) == 0 and fire_at is not None
                     and (a.for_s - 0.3) <= fire_at <= (a.for_s + 3))}


async def run_verify(a):
    proc = start_engine("fixture_verify_run.log", a.file)
    await asyncio.sleep(1.5)
    base = len(audit_lines())
    t0 = time.monotonic()
    times = []
    if exec_output("on"):
        while time.monotonic() - t0 < a.max_wait and len(times) < 2:
            if len(fires_for(base, a.rule_id)) > len(times):
                times.append(round(time.monotonic() - t0, 2))
            await asyncio.sleep(0.05)
    exec_output("off")
    await asyncio.sleep(1)
    stop_engine(proc)
    interval = round(times[1] - times[0], 2) if len(times) == 2 else None
    return {"exec_detect_s": times, "interval_s": interval,
            "expect": "2 allowed executions ~2s apart (verify timeout -> retry re-exec)",
            "pass": len(times) == 2 and interval is not None and 1.2 <= interval <= 3.2}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["debounce", "verify"], required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--rule-id", required=True)
    ap.add_argument("--for-s", type=float, default=2)
    ap.add_argument("--max-wait", type=float, default=9)
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    res = asyncio.run(run_debounce(a) if a.mode == "debounce" else run_verify(a))
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
