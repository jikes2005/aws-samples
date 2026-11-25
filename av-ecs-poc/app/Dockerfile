ARG BASE_IMAGE=public.ecr.aws/docker/library/python:3.12-slim
FROM ${BASE_IMAGE}
ARG PIP_INDEX_URL=""

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} -r requirements.txt

COPY app.py healthcheck.py entrypoint.sh ./
RUN chmod 755 /app/entrypoint.sh && useradd --uid 10001 --no-create-home appuser
USER 10001

# ROLE = director | server | guard | agent (set per task definition)
ENTRYPOINT ["/app/entrypoint.sh"]
