#!/usr/bin/env python3
"""MCP stdio 冒烟：initialize → tools/list → tools/call → resources/list → resources/read。"""
import atexit, json, subprocess, sys, time

GW = [sys.executable, "mhs-gateway/gateway.py", "mcp", "--transport", "stdio"]
VIRT = subprocess.Popen([sys.executable, "mhs-gateway/virtual_executor.py",
                         "--port", "9999"], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
atexit.register(VIRT.terminate)
time.sleep(0.6)

def msg(method, params=None, id_=None, notify=False):
    m = {"jsonrpc": "2.0", "method": method}
    if params is not None: m["params"] = params
    if not notify: m["id"] = id_
    return m

def read_msg(p):
    while True:
        line = p.stdout.readline()
        if not line: raise RuntimeError("server closed")
        line = line.strip()
        if not line: continue
        return json.loads(line)

p = subprocess.Popen(GW, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     text=True, bufsize=1)
def send(m): p.stdin.write(json.dumps(m) + "\n"); p.stdin.flush()
def call(method, params, id_):
    send(msg(method, params, id_))
    while True:
        r = read_msg(p)
        if r.get("id") == id_: return r

try:
    r = call("initialize", {"protocolVersion": "2025-03-26",
             "capabilities": {}, "clientInfo": {"name": "smoke", "version": "0"}},
             1)
    print("INIT:", r["result"]["serverInfo"])
    send(msg("notifications/initialized", notify=True))

    r = call("tools/list", {}, 2)
    tools = [t["name"] for t in r["result"]["tools"]]
    print("TOOLS(%d):" % len(tools), tools)
    led_set = [t for t in r["result"]["tools"] if t["name"] == "esp32s3-led-01.set_led"]
    print("SET_LED SCHEMA:", json.dumps(led_set[0]["inputSchema"], ensure_ascii=False)[:400])
    ping_t = [t for t in r["result"]["tools"] if t["name"] == "virtual-actuator-01.ping"]
    print("PING SCHEMA:", json.dumps(ping_t[0]["inputSchema"], ensure_ascii=False))
    reset_t = [t for t in r["result"]["tools"] if t["name"] == "esp32s3-led-01.reset"]
    print("RESET(L2) SCHEMA:", json.dumps(reset_t[0]["inputSchema"], ensure_ascii=False))

    r = call("tools/call", {"name": "virtual-actuator-01.ping", "arguments": {}}, 3)
    print("PING CALL:", r["result"]["content"][0]["text"][:200])

    r = call("tools/call", {"name": "virtual-actuator-01.set_output",
             "arguments": {"state": "on"}}, 4)
    print("SET_OUTPUT CALL:", r["result"]["content"][0]["text"][:250])

    r = call("resources/list", {}, 5)
    print("RESOURCES(%d):" % len(r["result"]["resources"]),
          [x["uri"] for x in r["result"]["resources"]])

    r = call("resources/read", {"uri": "mhs://virtual-actuator-01/output"}, 6)
    print("READ output:", r["result"]["contents"][0]["text"][:200])

    r = call("tools/call", {"name": "esp32s3-led-01.reset", "arguments": {"confirm": False}}, 7)
    print("RESET no-confirm:", r["result"]["content"][0]["text"][:150])

    # ---- 网关管理工具冒烟 ----
    r = call("tools/call", {"name": "gateway.kill_switch", "arguments": {"state": "on"}}, 8)
    print("KILL on:", r["result"]["content"][0]["text"][:200])
    r = call("tools/call", {"name": "virtual-actuator-01.set_output",
             "arguments": {"state": "on"}}, 9)
    print("L1 under kill:", r["result"]["content"][0]["text"][:150])
    r = call("tools/call", {"name": "gateway.kill_switch", "arguments": {"state": "off"}}, 10)
    print("KILL off:", r["result"]["content"][0]["text"][:150])
    r = call("resources/read", {"uri": "mhs://gateway/audit/recent"}, 11)
    evs = json.loads(r["result"]["contents"][0]["text"])["events"]
    print("AUDIT tail(%d): last=%s" % (len(evs), evs[-1]["action"] if evs else None))
finally:
    try: p.stdin.close()
    except Exception: pass
    p.terminate()
