#!/bin/bash
PROJECT_PATH=/opt/dix/repdix

cd $PROJECT_PATH
PIPENV_VENV_PATH=$PROJECT_PATH/.venv/bin
PATH="$PIPENV_VENV_PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
echo PATH=$PATH

pipenv run python -m app.main
