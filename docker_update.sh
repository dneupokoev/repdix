#!/bin/bash

#pipenv lock
set -e

pipenv requirements > docker/requirements.txt
cp -f README.md docker/README.md

rm -rf docker/app
cp -R app docker/app

find ./docker -type d -name "__pycache__" -exec rm -rf {} +
find ./docker -type f -name "*.log" -delete
find ./docker -type d -name ".pytest_cache" -exec rm -rf {} +
find ./docker -type d -name ".git" -exec rm -rf {} +
find ./docker -type f -name ".gitignore" -delete
find ./docker -name ".aider*" -exec rm -rf {} +
