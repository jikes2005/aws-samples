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
# POC test hooks: the Director page can make a server or guard task exit (ECS then replaces it)
ADMIN_ACTIONS = os.environ.get("ADMIN_ACTIONS", "true").lower() == "true"


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


class Html(str):
    """Return Html(...) from a route to send text/html instead of JSON."""


class Handler(BaseHTTPRequestHandler):
    routes = []  # (method, path prefix, fn(handler, parts, query, body) -> (code, payload))
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, payload):
        is_html = isinstance(payload, Html)
        raw = payload.encode() if is_html else json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8" if is_html else "application/json")
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
            if m == method and u.path.startswith(prefix) and (prefix != "/" or u.path == "/"):
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
# Like the vendor image: the address comes from the environment (default: host name).
# On ECS it is set by the wrapper entrypoint (wrapper/entrypoint.sh) to the task IP.
SERVER_NAME = os.environ.get("NG_SERVER_NAME") or socket.gethostname()
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
    try:
        res = guard_call(d["guard_address"], d["guard_port"], req, timeout=CMD_TIMEOUT)
    except OSError as exc:
        return 502, {"error": f"guard {d['guard_address']}:{d['guard_port']} unreachable ({exc}) - "
                              "the agent tunnel is probably reconnecting", "trace": trace}
    trace += res.pop("trace", [])
    log("command_done", device_id=dev, guard=f"{d['guard_address']}:{d['guard_port']}",
        cmd=req["cmd"], ms=int((time.time() - t0) * 1000))
    return 200, {"device_id": dev, "result": res.get("result"), "error": res.get("error"), "trace": trace}


def exit_soon(reason):
    log("exiting_on_request", reason=reason)
    threading.Timer(0.5, lambda: os._exit(1)).start()


def server_exit(h, parts, q, b):
    """POC hook: Director -> this server (task IP). The task exits and ECS replaces it."""
    if not ADMIN_ACTIONS:
        return 403, {"error": "admin actions disabled"}
    exit_soon("crash requested from the test page")
    return 200, {"exiting": ME["task_id"]}


def server_crash_guard(h, parts, q, b):
    """POC hook: Director -> server -> one specific guard, on a device port it advertised."""
    if not ADMIN_ACTIONS:
        return 403, {"error": "admin actions disabled"}
    res = guard_call(b["guard_address"], b["guard_port"], {"request_id": "admin", "admin": "exit"}, timeout=10)
    return 200, {"guard": f"{b['guard_address']}:{b['guard_port']}", "reply": res}


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
        ("POST", "/internal/exit", server_exit),
        ("POST", "/internal/crash-guard", server_crash_guard),
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


def director_crash_server(h, parts, q, b):
    """POST /admin/servers/<id>/crash -> tell that server (by task IP) to exit."""
    srv = get_server(parts[2]) if len(parts) > 2 else None
    if not srv:
        return 404, {"error": "unknown server"}
    return http_json("POST", f"http://{srv['address']}:{srv['port']}/internal/exit", {}, timeout=5)


def director_crash_guard(h, parts, q, b):
    """POST /admin/guards/<id>/crash -> via a server (only servers may reach guard device ports)."""
    gid = parts[2] if len(parts) > 2 else ""
    dev = next((d for d in scan(tables()[1]) if d.get("guard_id") == gid and d.get("online")), None)
    srv = get_server(dev["owner_server_id"]) if dev else None
    if not srv:
        return 404, {"error": "no online device on that guard"}
    return http_json("POST", f"http://{srv['address']}:{srv['port']}/internal/crash-guard",
                     {"guard_address": dev["guard_address"], "guard_port": dev["guard_port"]}, timeout=15)


def director_page(h, parts, q, b):
    return 200, Html(TEST_PAGE)


def run_director():
    wait_for_registry()
    serve_http(DIRECTOR_PORT, [
        ("GET", "/healthz", director_healthz),
        ("GET", "/servers", director_servers),
        ("GET", "/devices", director_devices),
        ("POST", "/devices/", director_command),
        ("POST", "/admin/servers/", director_crash_server),
        ("POST", "/admin/guards/", director_crash_guard),
        ("GET", "/", director_page),
    ])


# =========================================================================== GUARD
GUARD_IP = os.environ.get("GUARD_EXTERNAL_IP") or socket.gethostname()  # set by wrapper/entrypoint.sh
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
    for attempt in range(60):  # keep trying for ~2 minutes (servers may be restarting)
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
        if req.get("admin") == "exit" and ADMIN_ACTIONS:   # POC hook from the test page
            writer.write((json.dumps({"exiting": ME["task_id"]}) + "\n").encode())
            await writer.drain()
            log("exiting_on_request", reason="crash requested from the test page")
            asyncio.get_running_loop().call_later(0.5, os._exit, 1)
            return
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


# =========================================================================== TEST PAGE (served by the Director at /)
TEST_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>av-ecs-poc test console</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --ink:#1d2430; --muted:#667085; --line:#e4e7ec; --ok:#12805c; --bad:#c0362c; --acc:#2f5bd3; }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; color:var(--ink); background:var(--bg); }
  header { padding:16px 24px; background:var(--card); border-bottom:1px solid var(--line); display:flex; gap:24px; align-items:baseline; flex-wrap:wrap; }
  header h1 { font-size:18px; margin:0; }
  header .meta { color:var(--muted); }
  main { padding:20px 24px; display:grid; gap:20px; grid-template-columns: 1fr 1fr; }
  section { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:16px; min-width:0; }
  section.wide { grid-column: 1 / -1; }
  h2 { font-size:15px; margin:0 0 4px; }
  p.hint { margin:0 0 12px; color:var(--muted); font-size:13px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:middle; white-space:nowrap; }
  th { color:var(--muted); font-weight:600; }
  code, .mono { font-family: ui-monospace, Consolas, monospace; font-size:12.5px; }
  button { font:inherit; font-size:12.5px; padding:4px 10px; border-radius:6px; border:1px solid var(--line); background:#fff; cursor:pointer; }
  button:hover { border-color:var(--acc); color:var(--acc); }
  button.primary { background:var(--acc); color:#fff; border-color:var(--acc); }
  button.primary:hover { color:#fff; opacity:.9; }
  button.danger:hover { border-color:var(--bad); color:var(--bad); }
  button:disabled { opacity:.5; cursor:wait; }
  .pill { display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px; }
  .pill.ok { background:#e6f4ee; color:var(--ok); } .pill.bad { background:#fbeaea; color:var(--bad); }
  .path { display:flex; gap:4px; align-items:center; flex-wrap:wrap; }
  .hop { border:1px solid var(--line); border-radius:6px; padding:2px 6px; background:#fafbfc; }
  .hop b { font-weight:600; margin-right:4px; }
  .arrow { color:var(--muted); }
  .toolbar { display:flex; gap:8px; align-items:center; margin-bottom:10px; flex-wrap:wrap; }
  .toolbar input { width:70px; padding:4px 6px; border:1px solid var(--line); border-radius:6px; font:inherit; }
  #log { max-height:220px; overflow:auto; font-family:ui-monospace,Consolas,monospace; font-size:12px; background:#0f1720; color:#d6e2f0; border-radius:6px; padding:10px; margin:0; }
  .changed { color:var(--bad); font-weight:600; }
  .scroll { overflow-x:auto; }
  @media (max-width: 1100px) { main { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <h1>av-ecs-poc test console</h1>
  <span class="meta">Page served by Director <b id="dirTask" class="mono">…</b> at <span id="dirIp" class="mono">…</span> (NLB picks any Director)</span>
  <label class="meta"><input type="checkbox" id="auto" checked> auto-refresh</label>
</header>
<main>
  <section>
    <h2>NG servers</h2>
    <p class="hint">Each server registered its own task IP as NG_SERVER_NAME:8261. "Crash" makes the task exit; ECS replaces it with a new IP and its devices are rehomed after ~30 s.</p>
    <div class="scroll"><table id="servers"></table></div>
  </section>
  <section>
    <h2>Guards</h2>
    <p class="hint">Guards that hold device tunnels, from the registry. "Crash" stops one; its agents reconnect through the NLB to another Guard.</p>
    <div class="scroll"><table id="guards"></table></div>
  </section>
  <section class="wide">
    <h2>Devices</h2>
    <div class="toolbar">
      <button class="primary" id="sweepBtn">Command all devices</button>
      <span class="meta">delay (s)</span><input type="number" id="delay" value="0" min="0" max="320">
      <span class="hint">Use a delay of 310 to test a 300 s+ operation through every hop.</span>
    </div>
    <div class="scroll"><table id="devices"></table></div>
  </section>
  <section class="wide">
    <h2>Activity</h2>
    <pre id="log"></pre>
  </section>
</main>
<script>
// Relative URLs, so the page also works behind a path prefix (e.g. a Jupyter /proxy/11000/ URL)
const $ = (id) => document.getElementById(id);
const short = (s) => (s || "").slice(0, 12);
const lastPath = {};
const lastResult = {};

function log(msg) {
  const t = new Date().toLocaleTimeString();
  $("log").textContent = `${t}  ${msg}\n` + $("log").textContent;
}

async function api(method, path, body) {
  const r = await fetch(path, { method, headers: { "Content-Type": "application/json" },
                                body: body ? JSON.stringify(body) : undefined });
  let data = {};
  try { data = await r.json(); } catch (e) { data = { error: `HTTP ${r.status}` }; }
  return { status: r.status, data };
}

function pathHtml(trace) {
  if (!trace || !trace.length) return "";
  return '<span class="path">' + trace.map((h) => {
    const where = h.endpoint || h.ip || "";
    const extra = h.hop === "agent" ? (h.device || "") : where;
    return `<span class="hop"><b>${h.hop}</b><span class="mono">${extra}</span>${h.note ? " (fwd)" : ""}</span>`;
  }).join('<span class="arrow">→</span>') + "</span>";
}

function pathKey(trace) { return (trace || []).map((h) => `${h.hop}@${h.endpoint || h.ip}`).join(">"); }

async function refresh() {
  const [h, s, d] = await Promise.all([api("GET", "healthz"), api("GET", "servers"), api("GET", "devices")]);
  $("dirTask").textContent = short(h.data.task); $("dirIp").textContent = h.data.ip || "";

  const servers = s.data.servers || [];
  $("servers").innerHTML = "<tr><th>Server</th><th>NG_SERVER_NAME:port</th><th>AZ</th><th>Status</th><th>Devices</th><th>Last heartbeat</th><th></th></tr>" +
    servers.map((x) => `<tr><td class="mono">${short(x.server_id)}</td><td class="mono">${x.address}:${x.port}</td><td>${x.az || ""}</td>
      <td>${x.live ? '<span class="pill ok">live</span>' : '<span class="pill bad">gone</span>'}</td><td>${x.devices}</td>
      <td class="mono">${(x.last_heartbeat || "").slice(11, 19)}</td>
      <td>${x.live ? `<button class="danger" onclick="crashServer('${x.server_id}')">Crash</button>` : ""}</td></tr>`).join("");

  const devices = d.data.devices || [];
  const guards = {};
  devices.forEach((x) => { if (x.online && x.guard_id) { const g = guards[x.guard_id] ||= { ip: x.guard_endpoint.split(":")[0], n: 0 }; g.n++; } });
  $("guards").innerHTML = "<tr><th>Guard</th><th>GUARD_EXTERNAL_IP</th><th>Tunnels</th><th></th></tr>" +
    Object.entries(guards).map(([id, g]) => `<tr><td class="mono">${short(id)}</td><td class="mono">${g.ip}</td><td>${g.n}</td>
      <td><button class="danger" onclick="crashGuard('${id}')">Crash</button></td></tr>`).join("");

  $("devices").innerHTML = "<tr><th>Device</th><th>Status</th><th>Owner server</th><th>Guard endpoint</th><th>Rehomed</th><th></th><th>Last command path</th><th>ms</th></tr>" +
    devices.map((x) => { const r = lastResult[x.device_id] || {};
      return `<tr><td class="mono">${x.device_id}</td>
      <td>${x.online ? '<span class="pill ok">online</span>' : '<span class="pill bad">offline</span>'}</td>
      <td class="mono">${x.owner_address || ""}</td><td class="mono">${x.guard_endpoint}</td><td>${x.rehome_count || 0}</td>
      <td><button onclick="sendCmd('${x.device_id}')">Send command</button></td>
      <td>${r.html || ""}${r.changed ? ' <span class="changed">path changed</span>' : ""}</td><td>${r.ms ?? ""}</td></tr>`; }).join("");
  return devices;
}

async function sendCmd(dev, quiet) {
  const delay = Number($("delay").value || 0);
  if (!quiet) log(`${dev}: command sent (delay ${delay}s)`);
  const t0 = performance.now();
  const r = await api("POST", `devices/${dev}/command`, { cmd: "status", delay });
  const ms = Math.round(performance.now() - t0);
  const key = pathKey(r.data.trace);
  const changed = lastPath[dev] && lastPath[dev] !== key;
  lastPath[dev] = key;
  lastResult[dev] = { html: r.status === 200 ? pathHtml(r.data.trace) : `<span class="pill bad">${r.status} ${r.data.error || ""}</span>`, ms, changed };
  log(`${dev}: HTTP ${r.status} in ${ms} ms ${r.status === 200 ? "" : "- " + (r.data.error || "")}${changed ? "  PATH CHANGED" : ""}`);
  await refresh();
}

async function sweep() {
  $("sweepBtn").disabled = true;
  try {
    const devices = (await api("GET", "devices")).data.devices || [];
    log(`Commanding ${devices.length} devices...`);
    await Promise.all(devices.map((x) => sendCmd(x.device_id, true)));
    const ok = devices.filter((x) => (lastResult[x.device_id] || {}).html?.includes("hop")).length;
    log(`Done: ${ok}/${devices.length} succeeded`);
  } finally { $("sweepBtn").disabled = false; }
}

async function crashServer(id) {
  if (!confirm(`Crash server ${short(id)}? ECS will start a replacement task with a new IP.`)) return;
  const r = await api("POST", `admin/servers/${id}/crash`);
  log(`crash server ${short(id)}: ${r.status} ${JSON.stringify(r.data)}`);
  refresh();
}

async function crashGuard(id) {
  if (!confirm(`Crash guard ${short(id)}? Its agents will reconnect to another Guard.`)) return;
  const r = await api("POST", `admin/guards/${id}/crash`);
  log(`crash guard ${short(id)}: ${r.status} ${JSON.stringify(r.data)}`);
  refresh();
}

$("sweepBtn").onclick = sweep;
refresh();
setInterval(() => { if ($("auto").checked) refresh().catch((e) => log("refresh failed: " + e)); }, 3000);
</script>
</body>
</html>
"""


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
