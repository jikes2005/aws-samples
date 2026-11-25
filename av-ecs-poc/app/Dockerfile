ARG BASE_IMAGE=public.ecr.aws/docker/library/python:3.12-slim
FROM ${BASE_IMAGE}
ARG PIP_INDEX_URL=""

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} -r requirements.txt

COPY app.py healthcheck.py entrypoint.sh ./
# Strip Windows line endings / UTF-8 BOM (files edited or unzipped on Windows), then
# run the script through sh so the kernel never has to parse its shebang line.
RUN sed -i '1s/^\xEF\xBB\xBF//; s/\r$//' /app/entrypoint.sh /app/app.py /app/healthcheck.py \
 && chmod 755 /app/entrypoint.sh && useradd --uid 10001 --no-create-home appuser
USER 10001

# ROLE = director | server | guard | agent (set per task definition)
ENTRYPOINT ["/bin/sh", "/app/entrypoint.sh"]
