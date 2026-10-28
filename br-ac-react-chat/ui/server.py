"""br-ac-react-chat UI server: serves the built React app over HTTPS.

The internal NLB passes TCP 443 straight through to this container, so it terminates TLS
itself with a self-signed certificate made at start-up (browsers will warn).

  /              the React app (static files from /app/static, built by npm run build)
  /config.json   runtime settings for the browser: {"apiBaseUrl": API_BASE_URL}
  /healthz       health check (NLB)

The certificate names come from TLS_HOSTNAMES (comma-separated DNS names or IPs; the stack
sets the NLB's DNS name plus ExtraTlsNames) plus the task's own IP address and localhost.
"""
import datetime
import ipaddress
import os
import socket
from urllib.parse import urlparse

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

STATIC_DIR = os.environ.get("STATIC_DIR", "/app/static")
API_BASE_URL = os.environ.get("API_BASE_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "8443"))
TLS_DIR = os.environ.get("TLS_DIR", "/app/run/tls")      # a writable volume (see Dockerfile)
CERT, KEY = f"{TLS_DIR}/cert.pem", f"{TLS_DIR}/key.pem"


# ---------------------------------------------------------------------------
# Self-signed certificate
# ---------------------------------------------------------------------------
def make_certificate():
    entries = [n.strip() for n in os.environ.get("TLS_HOSTNAMES", "").split(",") if n.strip()]
    ips, names = {"127.0.0.1"}, []
    for entry in entries:                 # entries may be DNS names or IP addresses
        try:
            ips.add(str(ipaddress.ip_address(entry)))
        except ValueError:
            names.append(entry)
    try:
        ips.add(socket.gethostbyname(socket.gethostname()))
    except OSError:
        pass
    sans = [x509.DNSName(n) for n in dict.fromkeys(names + ["localhost"])]
    sans += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in sorted(ips)]

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, (names or ["localhost"])[0][:64]),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "br-ac-react-chat lab (self-signed)")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=397))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    try:
        os.makedirs(TLS_DIR, mode=0o700, exist_ok=True)
    except PermissionError:
        raise SystemExit(f"Can't create {TLS_DIR} as UID {os.getuid()}. The task definition must mount a volume "
                         f"at {os.path.dirname(TLS_DIR)} and the image must declare it (VOLUME) owned by this user.")
    with open(KEY, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    os.chmod(KEY, 0o600)
    with open(CERT, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    fp = cert.fingerprint(hashes.SHA256()).hex(":").upper()
    print(f"Self-signed certificate for {[str(s.value) for s in sans]}\n  SHA-256 fingerprint: {fp}", flush=True)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
def api_origin():
    u = urlparse(API_BASE_URL)
    return f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else ""


class SecurityHeaders(BaseHTTPMiddleware):
    """The page may only load its own files and call the API."""
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            f"connect-src 'self' {api_origin()}; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.url.path in ("/", "/index.html", "/config.json"):
            response.headers["Cache-Control"] = "no-cache"
        return response


async def config(request):
    return JSONResponse({"apiBaseUrl": API_BASE_URL})


async def healthz(request):
    return PlainTextResponse("ok")


app = Starlette(
    routes=[Route("/config.json", config), Route("/healthz", healthz),
            Mount("/", app=StaticFiles(directory=STATIC_DIR, html=True), name="static")],
    middleware=[Middleware(SecurityHeaders)],
)


if __name__ == "__main__":
    try:
        os.makedirs(os.environ.get("TMPDIR", "/tmp"), exist_ok=True)
    except OSError:
        pass   # make_certificate() reports a clear error if the volume isn't writable
    make_certificate()
    print(f"Serving {STATIC_DIR} on https://0.0.0.0:{PORT} (API: {API_BASE_URL or 'not set'})", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=PORT, ssl_certfile=CERT, ssl_keyfile=KEY,
                access_log=False, proxy_headers=False, server_header=False)
