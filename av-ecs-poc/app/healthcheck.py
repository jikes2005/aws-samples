"""Container health check per role (ECS passes the result to Service Connect / Cloud Map)."""
import os
import socket
import sys
import urllib.request

role = os.environ.get("ROLE", "server")
try:
    if role == "server":
        urllib.request.urlopen(f"http://127.0.0.1:{os.environ.get('SERVER_PORT', '8261')}/ng/server/healthz", timeout=3)
    elif role == "director":
        urllib.request.urlopen(f"http://127.0.0.1:{os.environ.get('DIRECTOR_PORT', '11000')}/healthz", timeout=3)
    elif role == "guard":
        socket.create_connection(("127.0.0.1", int(os.environ.get("TUNNEL_PORT", "9000"))), timeout=3).close()
except Exception:
    sys.exit(1)
