#!/bin/bash

# ensure_file_headers.sh
# Добавляет комментарий с полным путем от корня проекта (например, # app/core/db_manager.py)
# в первую строку каждого .py файла, если его там нет.

SEARCH_DIR="${1:-.}"

find "$SEARCH_DIR" -name "*.py" -type f -not -path "*/\.*" | while read -r file; do
    # Убираем префикс ./ для получения чистого относительного пути
    rel_path="${file#./}"
    expected="# $rel_path"

    # Берём первую строку файла
    first_line=$(head -n 1 "$file")

    # Если первая строка уже совпадает с ожидаемым заголовком — пропускаем
    if [ "$first_line" = "$expected" ]; then
        echo "OK:   $rel_path"
    else
        # Вставляем ожидаемую строку в самое начало файла
        sed -i "1i $expected" "$file"
        echo "ADD:  $rel_path"
    fi
done