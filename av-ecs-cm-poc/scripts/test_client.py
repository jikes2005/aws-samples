#!/usr/bin/env python3
"""
Simulated CRU clients (stdlib only). Run from a host inside the VPC
(e.g. the SageMaker notebook) that can reach the director NLB and the worker
task IPs (ClientCidr in the base stack).

  python3 test_client.py --director http://<nlb-dns>:7000 --clients 6 --rounds 3

Each client asks the director for its task, then calls that task directly.
Watch 'hits' climb on the same task (sticky). Stop a B task between runs and
re-run: only that task's clients show REASSIGNED.
"""
import argparse
import json
import time
import urllib.request


def call(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--director", required=True, help="e.g. http://internal-xxx.elb.ca-central-1.amazonaws.com:7000")
    p.add_argument("--clients", type=int, default=6)
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--prefix", default="sim-")
    p.add_argument("--service", default="b")
    p.add_argument("--use-hostname", action="store_true", help="connect via <task-id>.b.<namespace> instead of IP")
    p.add_argument("--sleep", type=float, default=2.0)
    a = p.parse_args()
    director = a.director.rstrip("/")

    print("Live instances (Cloud Map, via director):")
    for i in call("GET", f"{director}/instances?service={a.service}")["instances"]:
        print(f"  {i['instance_id']}  {i['ip']}:{i['port']}  {i['az']}  {i.get('hostname') or ''}")

    seen = {}
    for r in range(1, a.rounds + 1):
        print(f"\nRound {r}")
        print(f"  {'client':<10} {'task':<34} {'endpoint':<28} {'hits':>4}  status")
        for n in range(1, a.clients + 1):
            cid = f"{a.prefix}{n:03d}"
            try:
                m = call("POST", f"{director}/assign", {"client_id": cid, "service": a.service})
                url = m["url_host"] if a.use_hostname and m.get("url_host") else m["url_ip"]
                w = call("GET", f"{url}/session/{cid}")
                status = "STICKY" if m.get("sticky") else ("REASSIGNED" if m.get("reassigned") else "NEW")
                seen.setdefault(cid, set()).add(w["task_id"])
                print(f"  {cid:<10} {w['task_id']:<34} {url.replace('http://', ''):<28} {w['hits_on_this_task']:>4}  {status}")
            except Exception as exc:
                print(f"  {cid:<10} ERROR {exc}")
        time.sleep(a.sleep)

    print("\nTasks seen per client (1 = fully sticky):")
    for cid, tasks in sorted(seen.items()):
        print(f"  {cid}: {len(tasks)}")


if __name__ == "__main__":
    main()
