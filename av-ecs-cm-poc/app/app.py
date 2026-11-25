"""
Cloud Map sticky-routing POC - one image, three roles (set SERVICE_ROLE).

  a  = caller    (stands in for NG1 / the CRU client side). Asks the director
                 which task it should talk to, then calls that task directly.
  b  = worker    (stands in for Guard / CRU server tasks). Many tasks; each
                 keeps per-client in-memory state so stickiness is visible.
  c  = director  (orchestrator). Reads live tasks from AWS Cloud Map and keeps
                 the client -> task mapping in DynamoDB.

Only dependencies: boto3 (a, c) and dnspython (a, for SRV lookups).
"""
import json
import logging
import os
import random
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROLE = os.environ.get("SERVICE_ROLE", "b").lower()
PORT = int(os.environ.get("PORT", "8080"))
REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "ca-central-1"
NAMESPACE = os.environ.get("NAMESPACE_NAME", "poc.internal")
NAMESPACE_MODE = os.environ.get("NAMESPACE_MODE", "DNS").upper()  # DNS or HTTP

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(event, **fields):
    """One JSON line per event - easy to query in CloudWatch Logs Insights."""
    logging.info(json.dumps({"ts": now_iso(), "role": ROLE, "event": event, **fields}, default=str))


# --------------------------------------------------------------------------
# Task identity from the ECS task metadata endpoint (v4)
# --------------------------------------------------------------------------
def task_identity():
    uri = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    if not uri:  # running locally
        return {"task_id": socket.gethostname(), "task_arn": None, "ip": "127.0.0.1", "az": "local"}
    for attempt in range(10):
        try:
            with urllib.request.urlopen(uri + "/task", timeout=2) as resp:
                meta = json.load(resp)
            ip = None
            for container in meta.get("Containers", []):
                for net in container.get("Networks", []):
                    if net.get("IPv4Addresses"):
                        ip = net["IPv4Addresses"][0]
                        break
                if ip:
                    break
            arn = meta.get("TaskARN", "")
            return {"task_id": arn.split("/")[-1], "task_arn": arn, "ip": ip, "az": meta.get("AvailabilityZone")}
        except Exception as exc:  # metadata endpoint can lag a second at start
            log("metadata_retry", attempt=attempt, error=str(exc))
            time.sleep(1)
    return {"task_id": socket.gethostname(), "task_arn": None, "ip": None, "az": None}


IDENTITY = task_identity()


# --------------------------------------------------------------------------
# Cloud Map discovery (API based - works for DNS and HTTP namespaces)
# --------------------------------------------------------------------------
class CloudMap:
    def __init__(self, namespace, ttl_seconds=5):
        import boto3

        self.sd = boto3.client("servicediscovery", region_name=REGION)
        self.namespace = namespace
        self.ttl = ttl_seconds
        self.cache = {}
        self.lock = threading.Lock()

    def healthy(self, service):
        """Healthy instances of a Cloud Map service: [{instance_id, ip, port, az, hostname}]"""
        now = time.time()
        with self.lock:
            cached = self.cache.get(service)
        if cached and now - cached["ts"] < self.ttl:
            return cached["instances"]
        # Cheap check first: only re-read the list when the revision changed
        if cached:
            try:
                rev = self.sd.discover_instances_revision(
                    NamespaceName=self.namespace, ServiceName=service
                )["InstancesRevision"]
                if rev == cached["rev"]:
                    with self.lock:
                        cached["ts"] = now
                    return cached["instances"]
            except Exception as exc:
                log("revision_check_failed", service=service, error=str(exc))
        resp = self.sd.discover_instances(
            NamespaceName=self.namespace, ServiceName=service, HealthStatus="HEALTHY", MaxResults=1000
        )
        instances = []
        for inst in resp.get("Instances", []):
            attrs = inst.get("Attributes", {})
            port = attrs.get("AWS_INSTANCE_PORT")
            instances.append(
                {
                    "instance_id": inst["InstanceId"],
                    "ip": attrs.get("AWS_INSTANCE_IPV4"),
                    "port": int(port) if port else None,
                    "az": attrs.get("AVAILABILITY_ZONE"),
                    # With A+SRV records Cloud Map also publishes an A record
                    # named <instance-id>.<service>.<namespace> for each task.
                    "hostname": f"{inst['InstanceId']}.{service}.{self.namespace}"
                    if NAMESPACE_MODE == "DNS"
                    else None,
                }
            )
        with self.lock:
            self.cache[service] = {"rev": resp.get("InstancesRevision"), "ts": now, "instances": instances}
        return instances


# --------------------------------------------------------------------------
# Small HTTP helpers
# --------------------------------------------------------------------------
def http_json(method, url, body=None, timeout=3):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


class JsonHandler(BaseHTTPRequestHandler):
    routes = {}  # (method, first path segment) -> function(handler, parts, query, body)

    def log_message(self, *args):  # silence default access log
        pass

    def _send(self, code, payload):
        raw = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        body = {}
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            try:
                body = json.loads(self.rfile.read(length))
            except ValueError:
                return self._send(400, {"error": "invalid json"})
        key = (method, parts[0] if parts else "")
        fn = self.routes.get(key)
        if not fn:
            return self._send(404, {"error": f"no route {method} /{'/'.join(parts)}"})
        try:
            code, payload = fn(self, parts, query, body)
            self._send(code, payload)
        except Exception as exc:
            log("handler_error", path=self.path, error=str(exc))
            self._send(500, {"error": str(exc)})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")


def health(_h, _p, _q, _b):
    return 200, {"status": "ok", "role": ROLE, **IDENTITY, "port": PORT}


# --------------------------------------------------------------------------
# Role B - worker (Guard / CRU server stand-in)
# --------------------------------------------------------------------------
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()


def worker_session(_h, parts, _q, _b):
    if len(parts) < 2:
        return 400, {"error": "use /session/<client_id>"}
    client_id = parts[1]
    with SESSIONS_LOCK:
        s = SESSIONS.setdefault(client_id, {"hits": 0, "first_seen": now_iso()})
        s["hits"] += 1
        hits, first_seen = s["hits"], s["first_seen"]
    # hits only climbs if the client keeps landing on this same task
    return 200, {
        "client_id": client_id,
        "hits_on_this_task": hits,
        "first_seen_on_this_task": first_seen,
        "task_id": IDENTITY["task_id"],
        "ip": IDENTITY["ip"],
        "port": PORT,
        "az": IDENTITY["az"],
    }


def worker_sessions(_h, _p, _q, _b):
    with SESSIONS_LOCK:
        return 200, {"task_id": IDENTITY["task_id"], "sessions": dict(SESSIONS)}


# --------------------------------------------------------------------------
# Role C - director (orchestrator)
# --------------------------------------------------------------------------
class Director:
    def __init__(self):
        import boto3

        self.boto3 = boto3
        self.table_name = os.environ["TABLE_NAME"]
        self.index_name = os.environ.get("TABLE_INSTANCE_INDEX", "by-instance")
        self.cloudmap = CloudMap(NAMESPACE, ttl_seconds=int(os.environ.get("DISCOVERY_TTL_SECONDS", "5")))
        self.local = threading.local()  # boto3 resources are not thread safe

    @property
    def table(self):
        if not hasattr(self.local, "table"):
            self.local.table = self.boto3.resource("dynamodb", region_name=REGION).Table(self.table_name)
        return self.local.table

    def _load(self, instance_id):
        """How many clients are mapped to an instance (GSI count)."""
        from boto3.dynamodb.conditions import Key

        resp = self.table.query(
            IndexName=self.index_name,
            KeyConditionExpression=Key("instance_id").eq(instance_id),
            Select="COUNT",
        )
        return resp["Count"]

    def _pick(self, healthy):
        loads = {i["instance_id"]: self._load(i["instance_id"]) for i in healthy}
        low = min(loads.values())
        choice = random.choice([i for i in healthy if loads[i["instance_id"]] == low])
        return choice, loads

    @staticmethod
    def _view(item, **extra):
        port = int(item["port"])
        out = {
            "client_id": item["client_id"],
            "service": item["service"],
            "instance_id": item["instance_id"],
            "ip": item["ip"],
            "port": port,
            "url_ip": f"http://{item['ip']}:{port}",
            "hostname": item.get("hostname"),
            "url_host": f"http://{item['hostname']}:{port}" if item.get("hostname") else None,
            "assigned_at": item.get("assigned_at"),
            "reassign_count": int(item.get("reassign_count", 0)),
            "previous_instance_id": item.get("previous_instance_id"),
            "state": item.get("state"),
        }
        out.update(extra)
        return out

    def assign(self, client_id, service):
        healthy = self.cloudmap.healthy(service)
        if not healthy:
            return 503, {"error": f"no healthy instances registered in Cloud Map for service '{service}'"}
        by_id = {i["instance_id"]: i for i in healthy}

        item = self.table.get_item(Key={"client_id": client_id}, ConsistentRead=True).get("Item")

        # 1) Sticky path - existing mapping still points at a live task
        if item and item.get("service") == service and item.get("instance_id") in by_id:
            self.table.update_item(
                Key={"client_id": client_id},
                UpdateExpression="SET #ls = :t, #st = :a",
                ExpressionAttributeNames={"#ls": "last_seen", "#st": "state"},
                ExpressionAttributeValues={":t": now_iso(), ":a": "ACTIVE"},
            )
            item["state"] = "ACTIVE"
            return 200, self._view(item, sticky=True, reassigned=False)

        # 2) New client, or its task is gone - pick the least loaded live task
        target, loads = self._pick(healthy)
        previous = item.get("instance_id") if item else None
        new_item = {
            "client_id": client_id,
            "service": service,
            "instance_id": target["instance_id"],
            "ip": target["ip"],
            "port": target["port"],
            "az": target.get("az"),
            "hostname": target.get("hostname"),
            "assigned_at": now_iso(),
            "last_seen": now_iso(),
            "assigned_by": IDENTITY["task_id"],
            "state": "ACTIVE",
            "reassign_count": int(item.get("reassign_count", 0)) + 1 if item else 0,
            "previous_instance_id": previous,
        }
        new_item = {k: v for k, v in new_item.items() if v is not None}
        # Optimistic concurrency: several director tasks can race for the same client
        if item:
            cond = {"ConditionExpression": "instance_id = :prev", "ExpressionAttributeValues": {":prev": previous}}
        else:
            cond = {"ConditionExpression": "attribute_not_exists(client_id)"}
        try:
            self.table.put_item(Item=new_item, **cond)
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            winner = self.table.get_item(Key={"client_id": client_id}, ConsistentRead=True)["Item"]
            log("assign_race_lost", client_id=client_id, winner=winner.get("instance_id"))
            return 200, self._view(winner, sticky=False, reassigned=False, race_lost=True)

        log(
            "assigned",
            client_id=client_id,
            service=service,
            instance_id=target["instance_id"],
            ip=target["ip"],
            port=target["port"],
            previous_instance_id=previous,
            loads=loads,
        )
        return 200, self._view(new_item, sticky=False, reassigned=previous is not None)

    def reconcile_forever(self, interval):
        """Mark mappings whose task left Cloud Map as ORPHANED (reassigned on next check-in)."""
        while True:
            time.sleep(interval)
            try:
                orphaned = 0
                scan = {"ProjectionExpression": "client_id, service, instance_id, #st",
                        "ExpressionAttributeNames": {"#st": "state"}}
                while True:
                    page = self.table.scan(**scan)
                    for it in page.get("Items", []):
                        live = {i["instance_id"] for i in self.cloudmap.healthy(it["service"])}
                        if it["instance_id"] not in live and it.get("state") != "ORPHANED":
                            try:
                                self.table.update_item(
                                    Key={"client_id": it["client_id"]},
                                    UpdateExpression="SET #st = :o",
                                    ConditionExpression="instance_id = :i",
                                    ExpressionAttributeNames={"#st": "state"},
                                    ExpressionAttributeValues={":o": "ORPHANED", ":i": it["instance_id"]},
                                )
                                orphaned += 1
                            except Exception:
                                pass  # reassigned meanwhile
                    if "LastEvaluatedKey" not in page:
                        break
                    scan["ExclusiveStartKey"] = page["LastEvaluatedKey"]
                if orphaned:
                    log("reconcile", orphaned=orphaned)
            except Exception as exc:
                log("reconcile_error", error=str(exc))


DIRECTOR = None


def director_assign(_h, _p, query, body):
    client_id = body.get("client_id") or query.get("client_id")
    service = body.get("service") or query.get("service") or "b"
    if not client_id:
        return 400, {"error": "client_id is required"}
    return DIRECTOR.assign(client_id, service)


def director_instances(_h, _p, query, _b):
    service = query.get("service", "b")
    return 200, {"service": service, "namespace": NAMESPACE, "instances": DIRECTOR.cloudmap.healthy(service)}


def director_assignments(_h, parts, _q, _b):
    if len(parts) > 1:
        item = DIRECTOR.table.get_item(Key={"client_id": parts[1]}).get("Item")
        return (200, DIRECTOR._view(item)) if item else (404, {"error": "not mapped"})
    items, scan = [], {}
    while True:
        page = DIRECTOR.table.scan(**scan)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            break
        scan["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return 200, {"count": len(items), "assignments": [DIRECTOR._view(i) for i in items]}


def director_release(_h, parts, _q, _b):
    if len(parts) < 2:
        return 400, {"error": "use DELETE /assignments/<client_id>"}
    DIRECTOR.table.delete_item(Key={"client_id": parts[1]})
    return 200, {"released": parts[1]}


# --------------------------------------------------------------------------
# Role A - caller (NG1 / CRU client stand-in)
# --------------------------------------------------------------------------
class Caller:
    def __init__(self):
        self.cloudmap = CloudMap(NAMESPACE)
        self.target_service = os.environ.get("TARGET_SERVICE", "b")
        self.director_service = os.environ.get("DIRECTOR_SERVICE", "c")
        self.director_url = os.environ.get("DIRECTOR_URL", "").rstrip("/")
        self.prefer_hostname = os.environ.get("PREFER_HOSTNAME", "true").lower() == "true"
        prefix = os.environ.get("CLIENT_PREFIX", "cru-")
        count = int(os.environ.get("CLIENT_COUNT", "10"))
        self.clients = [f"{prefix}{n:03d}" for n in range(1, count + 1)]
        self.last_task = {}

    def director_base(self):
        if self.director_url:
            return self.director_url
        # Discover the director itself through the Cloud Map API
        inst = random.choice(self.cloudmap.healthy(self.director_service))
        return f"http://{inst['ip']}:{inst['port']}"

    def dns_view(self):
        """What plain DNS clients see for the worker service (DNS namespace only)."""
        if NAMESPACE_MODE != "DNS":
            return {"mode": "HTTP", "note": "HTTP namespace - no DNS records, use the Cloud Map API"}
        name = f"{self.target_service}.{NAMESPACE}"
        out = {"name": name}
        try:
            out["a_records"] = sorted({ai[4][0] for ai in socket.getaddrinfo(name, None, socket.AF_INET)})
        except Exception as exc:
            out["a_error"] = str(exc)
        try:
            import dns.resolver

            answers = dns.resolver.resolve(name, "SRV")
            out["srv_records"] = sorted(f"{r.target.to_text().rstrip('.')}:{r.port}" for r in answers)
        except Exception as exc:
            out["srv_error"] = str(exc)
        return out

    def run_round(self):
        base = self.director_base()
        results = []
        for cid in self.clients:
            row = {"client_id": cid}
            try:
                a = http_json("POST", f"{base}/assign", {"client_id": cid, "service": self.target_service})
                use_host = self.prefer_hostname and a.get("url_host")
                url = a["url_host"] if use_host else a["url_ip"]
                w = http_json("GET", f"{url}/session/{cid}")
                prev = self.last_task.get(cid)
                self.last_task[cid] = w["task_id"]
                row.update(
                    {
                        "assigned_instance": a["instance_id"],
                        "served_by_task": w["task_id"],
                        "endpoint": url,
                        "via": "hostname" if use_host else "ip",
                        "hits_on_this_task": w["hits_on_this_task"],
                        "sticky": a.get("sticky"),
                        "reassigned": a.get("reassigned"),
                        "task_changed": prev is not None and prev != w["task_id"],
                        "id_match": a["instance_id"] == w["task_id"],
                    }
                )
            except Exception as exc:
                row["error"] = str(exc)
            results.append(row)
            log("call", **row)
        return results

    def loop_forever(self, interval):
        time.sleep(15)  # let the director and workers register first
        while True:
            try:
                log("dns_view", **self.dns_view())
                self.run_round()
            except Exception as exc:
                log("round_error", error=str(exc))
            time.sleep(interval)


CALLER = None


def caller_run(_h, _p, _q, _b):
    return 200, {"results": CALLER.run_round()}


def caller_discovery(_h, _p, query, _b):
    service = query.get("service", CALLER.target_service)
    return 200, {"api": CALLER.cloudmap.healthy(service), "dns": CALLER.dns_view()}


# --------------------------------------------------------------------------
def main():
    global DIRECTOR, CALLER
    routes = {("GET", "health"): health}
    if ROLE == "b":
        routes.update({("GET", "session"): worker_session, ("GET", "sessions"): worker_sessions})
    elif ROLE == "c":
        DIRECTOR = Director()
        routes.update(
            {
                ("POST", "assign"): director_assign,
                ("GET", "assign"): director_assign,
                ("GET", "instances"): director_instances,
                ("GET", "assignments"): director_assignments,
                ("DELETE", "assignments"): director_release,
            }
        )
        interval = int(os.environ.get("RECONCILE_SECONDS", "30"))
        threading.Thread(target=DIRECTOR.reconcile_forever, args=(interval,), daemon=True).start()
    elif ROLE == "a":
        CALLER = Caller()
        routes.update({("GET", "run"): caller_run, ("GET", "discovery"): caller_discovery})
        interval = int(os.environ.get("LOOP_SECONDS", "20"))
        if interval > 0:
            threading.Thread(target=CALLER.loop_forever, args=(interval,), daemon=True).start()
    else:
        raise SystemExit(f"unknown SERVICE_ROLE {ROLE}")

    JsonHandler.routes = routes
    log("starting", port=PORT, namespace=NAMESPACE, mode=NAMESPACE_MODE, **IDENTITY)
    ThreadingHTTPServer(("0.0.0.0", PORT), JsonHandler).serve_forever()


if __name__ == "__main__":
    main()
