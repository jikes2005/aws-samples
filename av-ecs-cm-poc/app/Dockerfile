# Override BASE_IMAGE if your build host pulls base images from an internal mirror
ARG BASE_IMAGE=public.ecr.aws/docker/library/python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py healthcheck.py ./

RUN useradd --uid 10001 --no-create-home appuser
USER 10001

# SERVICE_ROLE (a|b|c) and PORT are set per ECS task definition
CMD ["python", "app.py"]
