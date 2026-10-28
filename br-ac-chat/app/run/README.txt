Empty on purpose. The Dockerfile copies this folder to /app/run, owned by the app user,
and declares it as a volume - the app's writable space (certificate, temp files).
