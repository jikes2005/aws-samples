# Built by build_images.ipynb, which first runs "npm run build" in web/ (-> web/dist) and
# installs the server's Python packages for the target platform into packages/. No RUN steps:
# nothing is downloaded during the build, and an arm64 image can be built on x86_64.
ARG BASE_IMAGE=public.ecr.aws/docker/library/python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/packages \
    HOME=/app/run \
    TMPDIR=/app/run/tmp \
    TLS_DIR=/app/run/tls \
    STATIC_DIR=/app/static

WORKDIR /app
COPY packages/ /app/packages/
COPY server.py ./
COPY web/dist/ /app/static/

# Writable space for the non-root user (the root filesystem is read-only at runtime). Fargate
# copies a VOLUME path's owner from the image, and COPY --chown sets it without a RUN step.
COPY --chown=10001:10001 run/ /app/run/
VOLUME ["/app/run", "/tmp"]
USER 10001

EXPOSE 8443
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD ["python", "-c", "import ssl, urllib.request; urllib.request.urlopen('https://127.0.0.1:8443/healthz', timeout=4, context=ssl._create_unverified_context())"]
CMD ["python", "server.py"]
