#!/bin/bash
# docker_update.sh
#
# Готовит папку docker/ как сборочный контекст для прода:
#   app/, config/, dockerfiles/, compose.yaml, requirements.txt, README.md
#
# Использование:
#   ./docker_update.sh           # без pipenv lock (быстро, для итераций)
#   ./docker_update.sh --lock    # с pipenv lock (для релиза, фиксирует версии)
#
# На проде:
#   cd /opt/dix/repdix/docker && docker compose up -d --build

set -e

# ---------------------------------------------------------------
# Разбор аргументов
# ---------------------------------------------------------------
RUN_LOCK=0
for arg in "$@"; do
    case "$arg" in
        --lock)
            RUN_LOCK=1
            ;;
        -h|--help)
            echo "Usage: $0 [--lock]"
            echo "  --lock   выполнить pipenv lock перед сборкой контекста"
            exit 0
            ;;
        *)
            echo "Неизвестный аргумент: $arg" >&2
            echo "Usage: $0 [--lock]" >&2
            exit 2
            ;;
    esac
done

# ---------------------------------------------------------------
# 0. (опционально) pipenv lock
# ---------------------------------------------------------------
if [ "$RUN_LOCK" -eq 1 ]; then
    echo "[1/8] pipenv lock (фиксируем версии зависимостей)..."
    pipenv lock
else
    echo "[1/8] pipenv lock пропущен (используйте --lock, чтобы включить)"
fi

# ---------------------------------------------------------------
# 1. requirements.txt
# ---------------------------------------------------------------
echo "[2/8] генерируем docker/requirements.txt..."
pipenv requirements > docker/requirements.txt

# ---------------------------------------------------------------
# 2. README.md
# ---------------------------------------------------------------
echo "[3/8] копируем README.md..."
cp -f README.md docker/README.md

# ---------------------------------------------------------------
# 3. app/
# ---------------------------------------------------------------
echo "[4/8] копируем app/..."
rm -rf docker/app
cp -R app docker/app

# ---------------------------------------------------------------
# 4. config/ (шаблоны с ${VAR}, БЕЗ .env)
# ---------------------------------------------------------------
echo "[5/8] копируем config/..."
rm -rf docker/config
cp -R config docker/config
# На всякий случай — не тащим .env в образ
rm -f docker/config/.env

# ---------------------------------------------------------------
# 5. dockerfiles/
# ---------------------------------------------------------------
echo "[6/8] копируем dockerfiles/..."

# Проверяем, где лежит папка с Dockerfile'ами (в корне или внутри docker/)
if [ -d "docker/dockerfiles" ]; then
    # Если папка лежит внутри docker/ (как на dev-машине)
    rm -rf docker/dockerfiles_tmp
    cp -R docker/dockerfiles docker/dockerfiles_tmp
    rm -rf docker/dockerfiles
    mv docker/dockerfiles_tmp docker/dockerfiles
elif [ -d "dockerfiles" ]; then
    # Если папка лежит в корне (как на проде)
    rm -rf docker/dockerfiles
    cp -R dockerfiles docker/dockerfiles
else
    echo "ОШИБКА: Папка с Dockerfile'ами не найдена ни в корне, ни в docker/"
    exit 1
fi

# ---------------------------------------------------------------
# 6. compose.yaml
# ---------------------------------------------------------------
echo "[7/8] копируем compose.yaml..."

# Проверяем, где лежит compose.yaml (в корне или внутри docker/)
if [ -f "docker/compose.yaml" ] && [ "$PWD" != "$(dirname "$(realpath docker/compose.yaml)")/.." ]; then
    # Если файл уже внутри docker/ и мы не пытаемся скопировать его самого в себя
    echo "  compose.yaml уже находится в docker/, пропускаем."
elif [ -f "compose.yaml" ]; then
    # Если файл лежит в корне (как на проде)
    cp -f compose.yaml docker/compose.yaml
else
    echo "ОШИБКА: compose.yaml не найден ни в корне, ни в docker/"
    exit 1
fi

# ---------------------------------------------------------------
# 7. Чистка мусора
# ---------------------------------------------------------------
echo "[8/8] чистим мусор в docker/..."

# Удаляем папки (с флагом -prune, чтобы find не пытался зайти внутрь удаляемой папки)
find ./docker -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find ./docker -type d -name ".pytest_cache" -prune -exec rm -rf {} + 2>/dev/null || true
find ./docker -type d -name ".git" -prune -exec rm -rf {} + 2>/dev/null || true

# Удаляем файлы
find ./docker -type f -name "*.pyc" -delete 2>/dev/null || true
find ./docker -type f -name "*.log" -delete 2>/dev/null || true
find ./docker -type f -name ".gitignore" -delete 2>/dev/null || true

# Удаляем файлы/папки, начинающиеся на .aider
find ./docker -name ".aider*" -exec rm -rf {} + 2>/dev/null || true

echo "Готово. Сборочный контекст в ./docker/"