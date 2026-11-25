#!/usr/bin/env bash
set -euo pipefail
REGION="${AWS_REGION:-us-east-1}"
PROJECT="${PROJECT:-av-ecs-poc}"
TAG="${TAG:-v1}"
BASE_IMAGE="${BASE_IMAGE:-public.ecr.aws/docker/library/python:3.12-slim}"
PIP_INDEX_URL="${PIP_INDEX_URL:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_URI=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "${PROJECT}-base" \
  --query "Stacks[0].Outputs[?OutputKey=='RepositoryUri'].OutputValue" --output text)
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${REPO_URI%%/*}"
docker build --platform linux/amd64 --build-arg BASE_IMAGE="$BASE_IMAGE" --build-arg PIP_INDEX_URL="$PIP_INDEX_URL" \
  -t "${REPO_URI}:${TAG}" "$HERE/../app"
docker push "${REPO_URI}:${TAG}"
