#!/bin/bash
# /opt/dix/repdix/docker_build.sh
#
# Запускать docker compose с явным путём к compose.yaml (в корне).
# Относительные пути внутри compose.yaml (env_file: ./.env, volumes: ./config,
# build.context: ./) резолвятся ОТНОСИТЕЛЬНО compose.yaml (корень),
# поэтому запускать можно из любой директории.
set -e
docker compose -f "$(dirname "$0")/compose.yaml" up -d --build