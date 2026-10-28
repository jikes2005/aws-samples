# Built by build_image.ipynb. The notebook installs the Python packages for the target
# platform into packages/ first, so this Dockerfile has no RUN steps:
#   - nothing is downloaded during the build (no internet needed), and
#   - an arm64 image can be built on an x86_64 notebook without emulation.
#
# BASE_IMAGE defaults to the ECR pull-through cache path for ECR Public's python:3.12-slim
# (the notebook passes the stack's BaseImageUri output).
ARG BASE_IMAGE=public.ecr.aws/docker/library/python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/packages \
    HOME=/app/run \
    TMPDIR=/app/run/tmp \
    TLS_DIR=/app/run/tls

WORKDIR /app
COPY packages/ /app/packages/
COPY app.py entrypoint.py ./
COPY streamlit_config/config.toml .streamlit/config.toml

# Writable space for the non-root user. The root filesystem is read-only at runtime, and a
# Fargate bind-mount volume is owned by root (0755) unless the image declares the path with
# VOLUME - then Fargate copies the directory and its owner from the image. COPY --chown
# makes /app/run owned by UID 10001 without a RUN step. The certificate goes in /app/run/tls.
COPY --chown=10001:10001 run/ /app/run/
VOLUME ["/app/run", "/tmp"]

# Non-root (numeric UID, so no useradd step)
USER 10001

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD ["python", "-c", "import ssl, urllib.request; urllib.request.urlopen('https://127.0.0.1:8501/_stcore/health', timeout=4, context=ssl._create_unverified_context())"]
CMD ["python", "entrypoint.py"]
