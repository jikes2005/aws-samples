#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


def call(method, url, body=None, timeout=360):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def path(trace):
    out = []
    for h in trace or []:
        where = h.get("endpoint") or h.get("ip")  # guard shows GUARD_EXTERNAL_IP:device-port
        out.append(f"{h['hop']}[{h.get('task', '')[:8]}@{where}]" + ("(fwd)" if h.get("note") else ""))
    return " -> ".join(out)


def table(rows, cols):
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) if rows else len(c) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--director", default=os.environ.get("DIRECTOR_URL"))
    sub = p.add_subparsers(dest="what", required=True)
    sub.add_parser("servers")
    sub.add_parser("devices")
    c = sub.add_parser("cmd")
    c.add_argument("device")
    c.add_argument("--cmd", default="status")
    c.add_argument("--delay", type=float, default=0)
    s = sub.add_parser("sweep")
    s.add_argument("--rounds", type=int, default=1)
    s.add_argument("--sleep", type=float, default=5)
    a = p.parse_args()
    if not a.director:
        sys.exit("set --director or DIRECTOR_URL")
    d = a.director.rstrip("/")

    if a.what == "servers":
        rows = call("GET", f"{d}/servers")[1]["servers"]
        for r in rows:
            r["server"] = r["server_id"][:12]
            r["address"] = f"{r['address']}:{r['port']}"
        table(rows, ["server", "address", "az", "live", "devices", "last_heartbeat"])
    elif a.what == "devices":
        rows = call("GET", f"{d}/devices")[1]["devices"]
        for r in rows:
            r["owner"] = (r["owner_server_id"] or "")[:12]
            r["guard"] = (r["guard_id"] or "")[:12]
            r["via"] = (r["registered_via"] or "")[:12]
        table(rows, ["device_id", "online", "owner", "owner_address", "guard", "guard_endpoint", "via", "rehome_count"])
    elif a.what == "cmd":
        t0 = time.time()
        code, res = call("POST", f"{d}/devices/{a.device}/command", {"cmd": a.cmd, "delay": a.delay})
        print(f"HTTP {code} in {time.time() - t0:.1f}s  result={res.get('result') or res.get('error')}")
        print("path:", path(res.get("trace")))
    elif a.what == "sweep":
        last = {}
        for r in range(1, a.rounds + 1):
            print(f"\nRound {r}")
            devices = [x["device_id"] for x in call("GET", f"{d}/devices")[1]["devices"]]
            for dev in devices:
                code, res = call("POST", f"{d}/devices/{dev}/command", {"cmd": "status"})
                pth = path(res.get("trace"))
                changed = " (CHANGED)" if dev in last and last[dev] != pth else ""
                last[dev] = pth
                print(f"  {dev:<9} {code}  {pth or res.get('error')}{changed}")
            if r < a.rounds:
                time.sleep(a.sleep)


if __name__ == "__main__":
    main()
