#!/usr/bin/env python3
"""MCP 读泛洪负载（复现 BUG-001 并发条件）：动态发现设备资源后按间隔轮读。

用法: python3 mcp_load.py --seconds 120 --interval-ms 50 --tag load1
输出: [tag] resources=... / [tag] reads=N errors=N max_read_s=... last_err=...
"""
import argparse, asyncio, time
from mcp.client.streamable_http import streamable_http_client
from mcp.client.session import ClientSession


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8808/mcp")
    ap.add_argument("--seconds", type=float, default=120)
    ap.add_argument("--interval-ms", type=int, default=50)
    ap.add_argument("--tag", default="load")
    a = ap.parse_args()
    end = time.monotonic() + a.seconds
    n = errs = 0
    maxlat = 0.0
    last_err = ""
    async with streamable_http_client(a.url) as streams:
        rd, wr = streams[0], streams[1]
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            raw = await s.list_resources()
            items = getattr(raw, "resources", None) or list(raw)
            uris = []
            for it in items:
                uri = it[0] if isinstance(it, tuple) else getattr(it, "uri", None)
                if uri:
                    uris.append(str(uri))
            uris = [u for u in uris if "gateway" not in u] or uris
            print("[%s] resources=%d -> %s" % (a.tag, len(uris), uris), flush=True)
            i = 0
            while time.monotonic() < end:
                t0 = time.monotonic()
                try:
                    await s.read_resource(uris[i % len(uris)])
                    n += 1
                    lat = time.monotonic() - t0
                    if lat > maxlat:
                        maxlat = lat
                except Exception as e:
                    errs += 1
                    last_err = repr(e)[:150]
                i += 1
                await asyncio.sleep(a.interval_ms / 1000.0)
    print("[%s] reads=%d errors=%d max_read_s=%.3f last_err=%s"
          % (a.tag, n, errs, maxlat, last_err), flush=True)


asyncio.run(main())
