#!/bin/bash
# /opt/dix/repdix/docker_build.sh
#
# DOCKER_BUILDKIT=0 — чтобы pip install мог достучаться до github.com
# (BuildKit игнорирует network: host в build).
# -f "$(dirname "$0")/compose.yaml" — запуск из любой директории,
# путь к compose.yaml резолвится относительно самого скрипта.
set -e

#DOCKER_BUILDKIT=0 docker compose -f "$(dirname "$0")/compose.yaml" up -d --build
docker compose -f "$(dirname "$0")/compose.yaml" up -d --build