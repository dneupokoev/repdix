# app/core/validator.py
dv_file_version = '261025.02'
"""Валидатор SQL-запросов: проверка безопасности и синтаксиса."""
import logging
import os
import re
from datetime import datetime

# Список запрещенных ключевых слов (в верхнем регистре)
FORBIDDEN_KEYWORDS = [
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE",
    "REPLACE", "CREATE", "GRANT", "REVOKE", "ATTACH", "PRAGMA",
    "EXEC", "CALL", "MERGE"
]


def validate_sql(sql: str) -> tuple[bool, str]:
    """Проверяет безопасность SQL-запроса.

    Args:
        sql: Строка с SQL-запросом.

    Returns:
        Кортеж (is_valid, reason). Если запрос безопасен, возвращает (True, "").
        Иначе возвращает (False, описание ошибки).
    """
    if not sql or not sql.strip():
        return False, "empty sql"

    sql_upper = sql.strip().upper()

    # Проверка на множественные запросы (точка с запятой не в конце)
    # Удаляем завершающую точку с запятой, если она есть, для проверки
    stripped_sql = sql_upper.rstrip(';').strip()
    
    if ';' in stripped_sql:
        return False, "multiple statements"

    # Проверка начала запроса
    if not (stripped_sql.startswith("SELECT") or stripped_sql.startswith("WITH")):
        return False, "only SELECT or WITH allowed"

    # Проверка запрещенных слов с границами слов
    for keyword in FORBIDDEN_KEYWORDS:
        # Используем регулярное выражение для поиска слова как отдельного токена
        pattern = r'\b' + keyword + r'\b'
        if re.search(pattern, stripped_sql):
            return False, f"forbidden keyword: {keyword}"

    return True, ""


def log_violation(user_id: str, sql: str, reason: str) -> None:
    """Записывает нарушение безопасности SQL в файл logs/security.log.

    Args:
        user_id: Идентификатор пользователя.
        sql: Строка с SQL-запросом.
        reason: Причина отклонения запроса.
    """
    os.makedirs("logs", exist_ok=True)
    log_entry = f"{datetime.now().isoformat()} | user={user_id} | reason={reason} | sql={sql}\n"
    with open("logs/security.log", "a", encoding="utf-8") as f:
        f.write(log_entry)
    logging.getLogger(__name__).warning("SQL violation: %s", log_entry.strip())
