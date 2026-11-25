"""Container health check used by ECS (ECS passes the result to Cloud Map)."""
import os
import sys
import urllib.request

try:
    urllib.request.urlopen(f"http://127.0.0.1:{os.environ.get('PORT', '8080')}/health", timeout=2)
except Exception:
    sys.exit(1)
