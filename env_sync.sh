#!/bin/bash

# ==============================================================================
# Скрипт: env_sync.sh
# Назначение: 
# 1. Сравнивает ключи в .env и docker/.env.
# 2. Генерирует обезличенные .env.example (удаляет ВСЕ значения, кроме безопасных).
# ==============================================================================

set -e

ENV_FILE=".env"
DOCKER_ENV_FILE="docker/.env"
EXAMPLE_FILE=".env.example"
DOCKER_EXAMPLE_FILE="docker/.env.example"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

if [ ! -f "$ENV_FILE" ]; then
    echo -e "${RED}Ошибка: Файл $ENV_FILE не найден в корне проекта!${NC}"
    exit 1
fi

# ==============================================================================
# 1. ПРОВЕРКА СООТВЕТСТВИЯ КЛЮЧЕЙ
# ==============================================================================
if [ -f "$DOCKER_ENV_FILE" ]; then
    echo -e "${YELLOW}--- Проверка соответствия ключей в $DOCKER_ENV_FILE ---${NC}"
    
    KEYS_MAIN=$(grep -v '^#' "$ENV_FILE" | grep -v '^\s*$' | cut -d= -f1 | sort)
    KEYS_DOCKER=$(grep -v '^#' "$DOCKER_ENV_FILE" | grep -v '^\s*$' | cut -d= -f1 | sort)

    MISSING_IN_DOCKER=$(comm -23 <(echo "$KEYS_MAIN") <(echo "$KEYS_DOCKER"))

    if [ -z "$MISSING_IN_DOCKER" ]; then
        echo -e "${GREEN}Все ключи из $ENV_FILE присутствуют в $DOCKER_ENV_FILE.${NC}"
    else
        echo -e "${RED}ВНИМАНИЕ! В $DOCKER_ENV_FILE не хватает следующих ключей:${NC}"
        echo "$MISSING_IN_DOCKER" | while read -r key; do
            echo -e "  - $key"
        done
        echo -e "${YELLOW}Рекомендуется скопировать их в $DOCKER_ENV_FILE.${NC}"
    fi
    echo ""
fi

# ==============================================================================
# 2. ГЕНЕРАЦИЯ .env.example (ТОТАЛЬНОЕ ОБЕЗЛИЧИВАНИЕ)
# ==============================================================================

# Белый список безопасных ключей, значения которых можно оставлять
SAFE_KEYS=("ADAPTER" "DEFAULT_DB" "DEFAULT_MODEL" "REDIS_HOST" "REDIS_PORT")

generate_example() {
    local input_file=$1
    local output_file=$2
    
    echo -e "${YELLOW}--- Генерация $output_file ---${NC}"
    > "$output_file" # Очищаем файл
    
    while IFS= read -r line || [ -n "$line" ]; do
        # Если пустая строка или комментарий — оставляем как есть
        if [[ -z "$line" || "$line" == \#* ]]; then
            echo "$line" >> "$output_file"
        else
            key=$(echo "$line" | cut -d= -f1)
            val=$(echo "$line" | cut -d= -f2-)
            
            is_safe=false
            for safe_key in "${SAFE_KEYS[@]}"; do
                if [ "$key" == "$safe_key" ]; then
                    is_safe=true
                    break
                fi
            done
            
            if [ "$is_safe" = true ]; then
                # Оставляем безопасное значение
                echo "$line" >> "$output_file"
            elif [[ "$key" == "ALLOWED_USERS" ]]; then
                # Для ID пользователей оставляем заглушку
                echo "$key=123456789" >> "$output_file"
            else
                # ВСЕ ОСТАЛЬНЫЕ значения удаляем (заменяем на пустую строку)
                # Чтобы было понятно, что это надо заполнить, можно добавить комментарий
                echo "$key=" >> "$output_file"
            fi
        fi
    done < "$input_file"
    
    echo -e "${GREEN}Создан: $output_file${NC}"
}

rm -f "$EXAMPLE_FILE"
rm -f "$DOCKER_EXAMPLE_FILE"

generate_example "$ENV_FILE" "$EXAMPLE_FILE"

if [ -f "$DOCKER_ENV_FILE" ]; then
    generate_example "$DOCKER_ENV_FILE" "$DOCKER_EXAMPLE_FILE"
else
    echo -e "${YELLOW}Файл $DOCKER_ENV_FILE не найден. Пропускаем генерацию для docker/.${NC}"
fi

echo -e "${GREEN}Готово! Файлы .env.example безопасны для коммита.${NC}"