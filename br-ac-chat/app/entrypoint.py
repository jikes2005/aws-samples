"""Container entry point: create a self-signed TLS certificate, then start Streamlit on HTTPS.

The Network Load Balancer passes TCP straight through, so the app terminates TLS itself.
A new certificate is made each time a task starts (browsers will warn - it's self-signed).
The certificate names come from TLS_HOSTNAMES (comma-separated DNS names or IP addresses; the
stack sets the NLB's DNS name plus ExtraTlsNames) plus the task's own IP address and localhost.
"""
import datetime
import ipaddress
import os
import socket
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

CERT_DIR = "/tmp/tls"
CERT, KEY = f"{CERT_DIR}/cert.pem", f"{CERT_DIR}/key.pem"


def make_certificate():
    entries = [n.strip() for n in os.environ.get("TLS_HOSTNAMES", "").split(",") if n.strip()]
    ips = {"127.0.0.1"}
    names = []
    for entry in entries:          # entries may be DNS names or IP addresses
        try:
            ips.add(str(ipaddress.ip_address(entry)))
        except ValueError:
            names.append(entry)
    sans = [x509.DNSName(n) for n in dict.fromkeys(names + ["localhost"])]
    try:
        ips.add(socket.gethostbyname(socket.gethostname()))
    except OSError:
        pass
    sans += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in sorted(ips)]

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, (names or ["localhost"])[0][:64]),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "br-ac-chat lab (self-signed)")])
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

    os.makedirs(CERT_DIR, mode=0o700, exist_ok=True)
    with open(KEY, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    os.chmod(KEY, 0o600)
    with open(CERT, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    fp = cert.fingerprint(hashes.SHA256()).hex(":").upper()
    print(f"Self-signed certificate for {[str(s.value) for s in sans]}\n  SHA-256 fingerprint: {fp}", flush=True)


if __name__ == "__main__":
    make_certificate()
    # "python -m streamlit": the packages are on PYTHONPATH, not installed with console scripts
    os.execv(sys.executable, [sys.executable, "-m", "streamlit", "run", "app.py",
                              "--server.sslCertFile", CERT, "--server.sslKeyFile", KEY])
