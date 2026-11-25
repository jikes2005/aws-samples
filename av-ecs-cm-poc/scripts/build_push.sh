#!/usr/bin/env bash
# Build the single POC image (roles a, b, c) and push it to the ECR repo
# created by the base stack. Run from the repo root or the scripts folder.
set -euo pipefail

REGION="${AWS_REGION:-ca-central-1}"
PROJECT="${PROJECT:-av-ecs-cm-poc}"
BASE_STACK="${BASE_STACK:-${PROJECT}-base}"
TAG="${TAG:-v1}"
# Point this at your internal mirror if public.ecr.aws is not reachable
BASE_IMAGE="${BASE_IMAGE:-public.ecr.aws/docker/library/python:3.12-slim}"

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_URI=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$BASE_STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='RepositoryUri'].OutputValue" --output text)
REGISTRY="${REPO_URI%%/*}"

echo "Building ${REPO_URI}:${TAG}"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"
docker build --platform linux/amd64 --build-arg BASE_IMAGE="$BASE_IMAGE" -t "${REPO_URI}:${TAG}" "$HERE/../app"
docker push "${REPO_URI}:${TAG}"
echo "Pushed ${REPO_URI}:${TAG}"
