import asyncio
import json
import os
import random
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROLE = os.environ.get("ROLE", "server").lower()
REGION = os.environ.get("AWS_REGION", "")
CMD_TIMEOUT = float(os.environ.get("CMD_TIMEOUT_SECONDS", "330"))


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(event, **fields):
    print(json.dumps({"ts": now_iso(), "role": ROLE, "event": event, **fields}, default=str), flush=True)


# --------------------------------------------------------------------------- identity
def task_identity():
    uri = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    if not uri:
        return {"task_id": os.environ.get("LOCAL_TASK_ID", socket.gethostname()), "ip": "127.0.0.1", "az": "local"}
    for _ in range(10):
        try:
            with urllib.request.urlopen(uri + "/task", timeout=2) as r:
                meta = json.load(r)
            ip = next(
                (n["IPv4Addresses"][0] for c in meta.get("Containers", []) for n in c.get("Networks", [])
                 if n.get("IPv4Addresses")),
                None,
            )
            return {"task_id": meta["TaskARN"].split("/")[-1], "ip": ip, "az": meta.get("AvailabilityZone")}
        except Exception:
            time.sleep(1)
    return {"task_id": socket.gethostname(), "ip": None, "az": None}


ME = task_identity()


def hop(name, **extra):
    return {"hop": name, "task": ME["task_id"][:12], "ip": ME["ip"], **extra}


# --------------------------------------------------------------------------- registry (DynamoDB)
# servers: server_id (PK) -> address (NG_SERVER_NAME), port, az, started_at, last_heartbeat (epoch)
# devices: device_id (PK) -> owner_server_id (GSI by-owner), guard_id, guard_address
#          (GUARD_EXTERNAL_IP), guard_port, online, registered_via, rehome_count, updated_at
TABLE_SERVERS = os.environ.get("TABLE_SERVERS", "servers")
TABLE_DEVICES = os.environ.get("TABLE_DEVICES", "devices")
STALE_SECONDS = int(os.environ.get("SERVER_STALE_SECONDS", "30"))
_ddb = threading.local()


class Conflict(Exception):
    pass


def tables():
    """Per-thread boto3 resources (resources are not thread safe)."""
    if not hasattr(_ddb, "servers"):
        import boto3

        res = boto3.resource("dynamodb", region_name=REGION or None)
        _ddb.servers, _ddb.devices = res.Table(TABLE_SERVERS), res.Table(TABLE_DEVICES)
    return _ddb.servers, _ddb.devices


def plain(item):
    out = {}
    for k, v in (item or {}).items():
        out[k] = (int(v) if v == int(v) else float(v)) if isinstance(v, Decimal) else v
    return out


def epoch():
    return int(time.time())


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def scan(table):
    items, kw = [], {}
    while True:
        page = table.scan(**kw)
        items += [plain(i) for i in page.get("Items", [])]
        if "LastEvaluatedKey" not in page:
            return items
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def get_server(server_id):
    return plain(tables()[0].get_item(Key={"server_id": server_id}, ConsistentRead=True).get("Item")) or None


def get_device(device_id):
    return plain(tables()[1].get_item(Key={"device_id": device_id}, ConsistentRead=True).get("Item")) or None


def device_count(server_id):
    from boto3.dynamodb.conditions import Key

    n, kw = 0, {}
    while True:
        page = tables()[1].query(IndexName="by-owner", KeyConditionExpression=Key("owner_server_id").eq(server_id),
                                 Select="COUNT", **kw)
        n += page["Count"]
        if "LastEvaluatedKey" not in page:
            return n
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def all_servers():
    rows = scan(tables()[0])
    for r in rows:
        r["live"] = r.get("last_heartbeat", 0) > epoch() - STALE_SECONDS
    return rows


def live_servers():
    """Live servers, least loaded first."""
    rows = [r for r in all_servers() if r["live"]]
    for r in rows:
        r["devices"] = device_count(r["server_id"])
    return sorted(rows, key=lambda r: (r["devices"], r["server_id"]))


def least_loaded(load):
    """Random pick among the least-loaded servers, so concurrent registrations spread out."""
    low = min(load.values())
    return random.choice([k for k, v in load.items() if v == low])


def update_device(device_id, sets, condition=None, values=None):
    """SET the given attributes; optional condition (raises Conflict if it fails)."""
    names = {f"#{k}": k for k in sets}
    vals = {f":{k}": v for k, v in sets.items()}
    kw = {"Key": {"device_id": device_id},
          "UpdateExpression": "SET " + ", ".join(f"#{k} = :{k}" for k in sets),
          "ExpressionAttributeNames": names, "ExpressionAttributeValues": vals}
    if condition:
        kw["ConditionExpression"] = condition[0]
        kw["ExpressionAttributeNames"].update(condition[1])
        kw["ExpressionAttributeValues"].update(values or {})
    try:
        tables()[1].update_item(**kw)
    except tables()[1].meta.client.exceptions.ConditionalCheckFailedException:
        raise Conflict()


def wait_for_registry():
    for attempt in range(30):
        try:
            tables()[0].scan(Limit=1)
            tables()[1].scan(Limit=1)
            return
        except Exception as exc:
            log("registry_wait", attempt=attempt, error=str(exc))
            time.sleep(5)
    raise SystemExit("registry tables not reachable")


# --------------------------------------------------------------------------- HTTP plumbing
def http_json(method, url, body=None, timeout=10.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {"error": str(e)}


class Handler(BaseHTTPRequestHandler):
    routes = []  # (method, path prefix, fn(handler, parts, query, body) -> (code, payload))
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, payload):
        raw = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _dispatch(self, method):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        query = {k: v[0] for k, v in parse_qs(u.query).items()}
        body = {}
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            try:
                body = json.loads(self.rfile.read(n))
            except ValueError:
                return self._send(400, {"error": "invalid json"})
        for m, prefix, fn in self.routes:
            if m == method and u.path.startswith(prefix):
                try:
                    return self._send(*fn(self, parts, query, body))
                except Exception as exc:
                    log("handler_error", path=self.path, error=repr(exc))
                    return self._send(500, {"error": repr(exc)})
        self._send(404, {"error": f"no route for {method} {u.path}"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


def serve_http(port, routes):
    Handler.routes = routes
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    srv.serve_forever()


def every(seconds, fn):
    def loop():
        while True:
            try:
                fn()
            except Exception as exc:
                log("background_error", fn=fn.__name__, error=repr(exc))
            time.sleep(seconds)

    threading.Thread(target=loop, daemon=True).start()


# =========================================================================== SERVER
SERVER_NAME = os.environ.get("NG_SERVER_NAME") or socket.gethostname()  # set by entrypoint.sh
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8261"))


def server_register():
    tables()[0].put_item(Item={
        "server_id": ME["task_id"], "address": SERVER_NAME, "port": SERVER_PORT, "az": ME["az"],
        "started_at": now_iso(), "last_heartbeat": epoch()})
    log("server_registered", server_id=ME["task_id"], ng_server_name=SERVER_NAME, port=SERVER_PORT)


def server_heartbeat():
    tables()[0].update_item(Key={"server_id": ME["task_id"]}, UpdateExpression="SET last_heartbeat = :t",
                            ExpressionAttributeValues={":t": epoch()})


def server_rehome():
    """Give devices of dead servers (stale heartbeat, e.g. a replaced task) to live servers.
    Every server runs this; a conditional write makes sure only one of them moves each device."""
    live = live_servers()
    if not live:
        return
    load = {s["server_id"]: s["devices"] for s in live}
    for d in scan(tables()[1]):
        old = d.get("owner_server_id")
        if old in load:
            continue
        target = least_loaded(load)
        try:
            update_device(d["device_id"],
                          {"owner_server_id": target, "rehome_count": d.get("rehome_count", 0) + 1,
                           "updated_at": now_iso()},
                          condition=("#owner_server_id = :old", {}) if old else
                                    ("attribute_not_exists(#owner_server_id)", {}),
                          values={":old": old} if old else None)
        except Conflict:
            continue  # another server moved it first
        load[target] += 1
        log("device_rehomed", device_id=d["device_id"], from_server=old, to_server=target)


def server_healthz(h, parts, q, b):
    return 200, {"status": "ok", **hop("server"), "ng_server_name": SERVER_NAME}


def server_register_device(h, parts, q, b):
    """Called by any Guard through Service Connect (GUARD_API_ROOT) - lands on ANY server."""
    dev = b["device_id"]
    cur = get_device(dev)
    live = live_servers()
    live_ids = {s["server_id"] for s in live}
    owner_id = (cur["owner_server_id"] if cur and cur.get("owner_server_id") in live_ids
                else least_loaded({s["server_id"]: s["devices"] for s in live}))
    tables()[1].put_item(Item={
        "device_id": dev, "owner_server_id": owner_id, "guard_id": b["guard_id"],
        "guard_address": b["guard_address"], "guard_port": int(b["guard_port"]), "online": True,
        "registered_via": ME["task_id"], "rehome_count": (cur or {}).get("rehome_count", 0),
        "updated_at": now_iso()})
    owner = get_server(owner_id)
    forwarded = owner_id != ME["task_id"]
    if forwarded:
        # server -> server, one specific instance, straight to its task IP
        http_json("POST", f"http://{owner['address']}:{owner['port']}/internal/devices/adopt",
                  {"device_id": dev, "from_server": ME["task_id"]}, timeout=5)
    log("device_registered", device_id=dev, guard=f"{b['guard_address']}:{b['guard_port']}",
        received_by=ME["task_id"], owner=owner_id, forwarded_to_owner=forwarded)
    return 200, {"device_id": dev, "received_by": ME["task_id"], "owner_server_id": owner_id,
                 "owner_address": f"{owner['address']}:{owner['port']}", "forwarded": forwarded}


def server_adopt(h, parts, q, b):
    log("device_adopted", device_id=b.get("device_id"), from_server=b.get("from_server"))
    return 200, {"adopted_by": ME["task_id"]}


def server_unregister_device(h, parts, q, b):
    try:  # only if the device is still on this guard (it may have reconnected elsewhere)
        update_device(b["device_id"], {"online": False, "updated_at": now_iso()},
                      condition=("#guard_id = :g", {"#guard_id": "guard_id"}), values={":g": b["guard_id"]})
        log("device_offline", device_id=b["device_id"], guard_id=b["guard_id"])
    except Conflict:
        pass
    return 200, {"ok": True}


def guard_call(address, port, message, timeout):
    """server -> one specific Guard instance, on the device port it advertised."""
    with socket.create_connection((address, int(port)), timeout=10) as s:
        s.settimeout(timeout)
        s.sendall((json.dumps(message) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf or b"{}")


def server_command(h, parts, q, b):
    """POST /devices/<id>/command - normally from the Director (direct to this task's IP)."""
    dev = parts[1]
    trace = b.get("trace", []) + [hop("server")]
    d = get_device(dev)
    if not d:
        return 404, {"error": f"unknown device {dev}", "trace": trace}
    if d["owner_server_id"] != ME["task_id"]:
        # Not the owner (e.g. ownership moved) -> server -> server to the owner
        trace[-1]["note"] = "not owner, forwarding"
        o = get_server(d["owner_server_id"])
        code, res = http_json("POST", f"http://{o['address']}:{o['port']}/devices/{dev}/command",
                              {**b, "trace": trace}, timeout=CMD_TIMEOUT)
        return code, res
    trace[-1]["owner"] = True
    if not d["online"]:
        return 409, {"error": f"device {dev} offline", "trace": trace}
    req = {"request_id": uuid.uuid4().hex[:8], "cmd": b.get("cmd", "status"), "delay": float(b.get("delay", 0))}
    t0 = time.time()
    res = guard_call(d["guard_address"], d["guard_port"], req, timeout=CMD_TIMEOUT)
    trace += res.pop("trace", [])
    log("command_done", device_id=dev, guard=f"{d['guard_address']}:{d['guard_port']}",
        cmd=req["cmd"], ms=int((time.time() - t0) * 1000))
    return 200, {"device_id": dev, "result": res.get("result"), "error": res.get("error"), "trace": trace}


def run_server():
    wait_for_registry()
    server_register()
    every(int(os.environ.get("HEARTBEAT_SECONDS", "10")), server_heartbeat)
    every(int(os.environ.get("REHOME_SECONDS", "15")), server_rehome)
    serve_http(SERVER_PORT, [
        ("GET", "/ng/server/healthz", server_healthz),
        ("POST", "/internal/devices/register", server_register_device),
        ("POST", "/internal/devices/unregister", server_unregister_device),
        ("POST", "/internal/devices/adopt", server_adopt),
        ("POST", "/devices/", server_command),
    ])


# =========================================================================== DIRECTOR
DIRECTOR_PORT = int(os.environ.get("DIRECTOR_PORT", "11000"))


def director_healthz(h, parts, q, b):
    return 200, {"status": "ok", **hop("director")}


def director_servers(h, parts, q, b):
    rows = all_servers()
    for r in rows:
        r["devices"] = device_count(r["server_id"])
        r["last_heartbeat"] = iso(r.get("last_heartbeat"))
    rows.sort(key=lambda r: (not r["live"], r.get("started_at", "")))
    return 200, {"servers": rows}


def director_devices(h, parts, q, b):
    servers = {s["server_id"]: s for s in scan(tables()[0])}
    rows = []
    for d in sorted(scan(tables()[1]), key=lambda x: x["device_id"]):
        o = servers.get(d.get("owner_server_id"), {})
        rows.append({
            "device_id": d["device_id"], "online": d.get("online"), "owner_server_id": d.get("owner_server_id"),
            "owner_address": f"{o['address']}:{o['port']}" if o else None, "guard_id": d.get("guard_id"),
            "guard_endpoint": f"{d.get('guard_address')}:{d.get('guard_port')}", "registered_via": d.get("registered_via"),
            "rehome_count": d.get("rehome_count", 0), "updated_at": d.get("updated_at")})
    return 200, {"devices": rows}


def director_command(h, parts, q, b):
    """Workstation -> (NLB, any Director) -> owning server, direct to its task IP."""
    if len(parts) < 3:
        return 400, {"error": "POST /devices/<device_id>/command"}
    dev = parts[1]
    d = get_device(dev)
    r = get_server(d["owner_server_id"]) if d else None
    if not r:
        return 404, {"error": f"device {dev} not registered"}
    url = f"http://{r['address']}:{r['port']}/devices/{dev}/command"   # no DNS, no LB
    trace = [hop("director", lookup=f"{dev} -> {r['address']}:{r['port']}")]
    t0 = time.time()
    try:
        code, res = http_json("POST", url, {**b, "trace": trace}, timeout=CMD_TIMEOUT)
    except Exception as exc:
        return 502, {"error": f"owner server {r['address']}:{r['port']} unreachable: {exc}",
                     "hint": "a replaced server keeps its old row until rehoming moves the device", "trace": trace}
    res["elapsed_ms"] = int((time.time() - t0) * 1000)
    return code, res


def run_director():
    wait_for_registry()
    serve_http(DIRECTOR_PORT, [
        ("GET", "/healthz", director_healthz),
        ("GET", "/servers", director_servers),
        ("GET", "/devices", director_devices),
        ("POST", "/devices/", director_command),
    ])


# =========================================================================== GUARD
GUARD_IP = os.environ.get("GUARD_EXTERNAL_IP") or ME["ip"]  # set by entrypoint.sh
GUARD_API_ROOT = os.environ.get("GUARD_API_ROOT", "http://ng-server:8261").rstrip("/")
TUNNEL_PORT = int(os.environ.get("TUNNEL_PORT", "9000"))
PORT_MIN = int(os.environ.get("DEVICE_PORT_MIN", "10000"))
PORT_MAX = int(os.environ.get("DEVICE_PORT_MAX", "10099"))


class Tunnel:
    def __init__(self, device_id, writer):
        self.device_id, self.writer = device_id, writer
        self.pending = {}
        self.port = None
        self.server = None
        self.lock = asyncio.Lock()

    async def send(self, msg):
        async with self.lock:
            self.writer.write((json.dumps(msg) + "\n").encode())
            await self.writer.drain()


TUNNELS = {}
USED_PORTS = set()


async def guard_post(path, body):
    loop = asyncio.get_running_loop()
    for attempt in range(10):
        try:
            return await loop.run_in_executor(None, lambda: http_json("POST", GUARD_API_ROOT + path, body, 10))
        except Exception as exc:
            log("guard_api_retry", path=path, attempt=attempt, error=str(exc))
            await asyncio.sleep(2)
    return None, None


async def guard_device_conn(tunnel, reader, writer):
    """A server connected to this guard's device port -> relay over the agent tunnel."""
    try:
        line = await reader.readline()
        if not line:
            return
        req = json.loads(line)
        fut = asyncio.get_running_loop().create_future()
        tunnel.pending[req["request_id"]] = fut
        await tunnel.send({"type": "cmd", **req})
        try:
            res = await asyncio.wait_for(fut, CMD_TIMEOUT)
        except asyncio.TimeoutError:
            res = {"error": "agent did not answer in time"}
        res.setdefault("trace", [])
        res["trace"] = [hop("guard", endpoint=f"{GUARD_IP}:{tunnel.port}")] + res["trace"]
        writer.write((json.dumps(res) + "\n").encode())
        await writer.drain()
    except Exception as exc:
        log("device_conn_error", device_id=tunnel.device_id, error=repr(exc))
    finally:
        writer.close()


async def guard_tunnel(reader, writer):
    """Long-lived tunnel from a branch agent (via the NLB)."""
    peer = writer.get_extra_info("peername")
    try:
        line = await asyncio.wait_for(reader.readline(), 10)
    except asyncio.TimeoutError:
        line = b""
    if not line:  # NLB / container health checks just open and close
        writer.close()
        return
    hello = json.loads(line)
    dev = hello["device_id"]
    old = TUNNELS.pop(dev, None)
    if old:
        old.writer.close()
    t = TUNNELS[dev] = Tunnel(dev, writer)
    free = [p for p in range(PORT_MIN, PORT_MAX + 1) if p not in USED_PORTS]
    if not free:
        writer.close()
        log("no_free_device_port", device_id=dev)
        return
    t.port = random.choice(free)  # "RandomPort" from the configured range
    USED_PORTS.add(t.port)
    t.server = await asyncio.start_server(lambda r, w: guard_device_conn(t, r, w), "0.0.0.0", t.port)
    await t.send({"type": "welcome", "guard_id": ME["task_id"], "guard_ip": GUARD_IP, "device_port": t.port})
    code, res = await guard_post("/internal/devices/register", {
        "device_id": dev, "guard_id": ME["task_id"], "guard_address": GUARD_IP, "guard_port": t.port})
    log("tunnel_open", device_id=dev, peer=peer, device_port=t.port, registered=code, server=res)
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            msg = json.loads(line)
            if msg.get("type") == "ping":
                await t.send({"type": "pong"})
            elif msg.get("type") == "result":
                fut = t.pending.pop(msg.get("request_id"), None)
                if fut and not fut.done():
                    fut.set_result(msg)
    except Exception as exc:
        log("tunnel_error", device_id=dev, error=repr(exc))
    finally:
        t.server.close()
        USED_PORTS.discard(t.port)
        for f in t.pending.values():
            if not f.done():
                f.set_result({"error": "tunnel closed"})
        if TUNNELS.get(dev) is t:
            TUNNELS.pop(dev, None)
            await guard_post("/internal/devices/unregister", {"device_id": dev, "guard_id": ME["task_id"]})
        log("tunnel_closed", device_id=dev, device_port=t.port)


async def guard_main():
    log("guard_start", guard_external_ip=GUARD_IP, guard_api_root=GUARD_API_ROOT, device_ports=f"{PORT_MIN}-{PORT_MAX}")
    srv = await asyncio.start_server(guard_tunnel, "0.0.0.0", TUNNEL_PORT)
    async with srv:
        await srv.serve_forever()


# =========================================================================== AGENT (simulator)
async def agent_device(device_id, targets):
    while True:
        host, port = random.choice(targets)
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.write((json.dumps({"type": "hello", "device_id": device_id}) + "\n").encode())
            await writer.drain()
            welcome = json.loads(await asyncio.wait_for(reader.readline(), 15))
            log("agent_connected", device_id=device_id, guard_ip=welcome.get("guard_ip"),
                device_port=welcome.get("device_port"))
            lock = asyncio.Lock()

            async def send(msg):
                async with lock:
                    writer.write((json.dumps(msg) + "\n").encode())
                    await writer.drain()

            async def pinger():
                while True:
                    await asyncio.sleep(float(os.environ.get("AGENT_PING_SECONDS", "30")))
                    await send({"type": "ping"})

            async def handle(cmd):
                await asyncio.sleep(float(cmd.get("delay", 0)))
                await send({"type": "result", "request_id": cmd["request_id"],
                            "result": f"{cmd.get('cmd')} ok from {device_id}",
                            "trace": [hop("agent", device=device_id)]})

            ping_task = asyncio.create_task(pinger())
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    msg = json.loads(line)
                    if msg.get("type") == "cmd":
                        asyncio.create_task(handle(msg))
            finally:
                ping_task.cancel()
                writer.close()
            log("agent_disconnected", device_id=device_id)
        except Exception as exc:
            log("agent_connect_failed", device_id=device_id, target=f"{host}:{port}", error=repr(exc))
        await asyncio.sleep(random.uniform(2, 5))


async def agent_main():
    targets = []
    for t in os.environ["AGENT_TARGETS"].split(","):
        h, p = t.strip().rsplit(":", 1)
        targets.append((h, int(p)))
    n = int(os.environ.get("AGENT_DEVICES", "10"))
    prefix = os.environ.get("AGENT_PREFIX", "dev-")
    await asyncio.gather(*(agent_device(f"{prefix}{i:03d}", targets) for i in range(1, n + 1)))


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    log("starting", **ME, ng_server_name=os.environ.get("NG_SERVER_NAME"),
        guard_external_ip=os.environ.get("GUARD_EXTERNAL_IP"))
    if ROLE == "server":
        run_server()
    elif ROLE == "director":
        run_director()
    elif ROLE == "guard":
        asyncio.run(guard_main())
    elif ROLE == "agent":
        asyncio.run(agent_main())
    else:
        sys.exit(f"unknown ROLE {ROLE}")
