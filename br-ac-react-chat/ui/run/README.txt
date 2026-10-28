Empty on purpose. The Dockerfile copies this folder to /app/run, owned by the app user,
and declares it as a volume - the server's writable space (TLS certificate, temp files).
