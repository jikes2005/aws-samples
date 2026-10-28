# Built by build_images.ipynb. The notebook installs the Python packages for the target
# platform into packages/ first, so this Dockerfile has no RUN steps: nothing is downloaded
# during the build, and an arm64 image can be built on an x86_64 notebook without emulation.
ARG BASE_IMAGE=public.ecr.aws/docker/library/python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/packages \
    HOME=/app/run \
    TMPDIR=/app/run

WORKDIR /app
COPY packages/ /app/packages/
COPY main.py ./

# Writable space for the non-root user (the root filesystem is read-only at runtime). Fargate
# copies a VOLUME path's owner from the image, and COPY --chown sets it without a RUN step.
COPY --chown=10001:10001 run/ /app/run/
VOLUME ["/app/run", "/tmp"]
USER 10001

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"]
# The API Gateway VPC link reaches this through the NLB over HTTP inside the VPC.
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--timeout-keep-alive", "75"]
