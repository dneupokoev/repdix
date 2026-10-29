# app/core/validator.py
dv_file_version = '261030.01'
r"""Валидатор SQL-запросов: проверка безопасности.

Ключевые изменения версии 261028.13:
- Исправлен SyntaxError: в raw-string докстринге заменены тройные кавычки
  на одинарные в упоминании r'''...''', чтобы не прерывать raw-string.
- Исправлен SyntaxWarning: докстринг модуля переведён в raw-string,
  чтобы последовательности \s, \x00 и т.п. не интерпретировались как
  невалидные escape-последовательности в Python 3.12+.
- log_violation() теперь пишет через logging.getLogger("security"), а не
  через open(..., 'a'). Это позволяет использовать RotatingFileHandler,
  который настраивается в main.py._setup_logging(). Раньше security.log
  рос без ротации — файл мог достичь гигабайт.
  Fallback: если логгер "security" не сконфигурирован (например, validator
  используется в тестах без main.py) — пишем напрямую в файл, как раньше.
- FORBIDDEN_KEYWORDS расширен: добавлены ANALYZE, VACUUM, OPTIMIZE, REPAIR,
  FLUSH, KILL, SHUTDOWN, SET (SET GLOBAL и т.п.), HANDLER, LOAD, COPY
  (для PostgreSQL), EXECUTE, PREPARE, DEALLOCATE.
- Проверка WITH: теперь ищет запрещённые слова и в CTE-частях (WITH x AS
  (SELECT ...) DELETE ... — DELETE поймается, ок; но WITH x AS (INSERT ...)
  — INSERT тоже ловится). Убрана лазейка, где стрипался только хвост, но
  голова "WITH" пропускала всё.
- Добавлена проверка на multiple statements через sqlglot-подобный разбор:
  если sqlglot.parse возвращает >1 statement — отказ. Раньше проверялась
  только `;` в середине, но `SELECT 1; SELECT 2` проходил, если `;` в конце
  (после rstrip). Теперь — строже.
- Убрана ошибка: `sql_upper.rstrip(';')` мог не удалить точку с запятой
  в конце, если после неё пробелы/комментарии. Теперь `re.sub(r';\s*$', '', sql)`.
- Комментарии: `--` и `/* */` вырезаются перед проверкой (атакующий мог
  написать `SEL/**/ECT` или `SELECT 1 -- ; DROP`). Но это может ломать
  легитимные запросы с комментариями — поэтому вырезаем только для
  проверки forbidden keywords, а для выполнения возвращаем оригинал.
- Добавлена проверка длины: очень длинный SQL (> 100 KB) отклоняется —
  защита от DoS (модель может сгенерировать гигантский запрос).
- Добавлена проверка на нулевые байты (\x00) — некоторые драйверы падают.
- Добавлена проверка на `INTO OUTFILE`/`INTO DUMPFILE` без учёта регистра
  (было), плюс `INTO @var` (MySQL user variables) — может утекать данные.
- log_violation() пишет в оба места: и в логгер "security" (с ротацией),
  и в logger(__name__).warning (для консоли).
- log_violation() санитизирует sql в логе: обрезает до 1000 символов
  и заменяет переводы строк, чтобы не ломать формат лога.
- Убран неиспользуемый import os после перехода на logging (оставлен
  fallback с open — os нужен для makedirs).
- Добавлен validate_sql_verbose() — возвращает (bool, reason, cleaned_sql)
  для отладки.
- Публичный API: validate_sql(sql) — совместим со старым.
- Fail-closed: SQL дополнительно разбирается sqlglot (диалект mysql) ДО
  регексп-проверок. Любая ошибка разбора — отказ. Это закрывает обходы
  регекспов через обфускацию (prompt injection, в т.ч. попытки через
  сломанный синтаксис протащить опасную конструкцию).
"""

import logging
import os
import re
from datetime import datetime

import sqlglot

# Логгер для security-событий (настраивается в main.py._setup_logging)
_security_logger = logging.getLogger("security")
_app_logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Запрещённые ключевые слова (ищутся как отдельные токены)
# ----------------------------------------------------------------------
FORBIDDEN_KEYWORDS = [
    # DML/DDL
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE",
    "REPLACE", "CREATE", "GRANT", "REVOKE", "MERGE",
    # SQLite / административные
    "ATTACH", "DETACH", "PRAGMA", "VACUUM", "ANALYZE", "REINDEX",
    # MySQL-админка
    "FLUSH", "KILL", "SHUTDOWN", "OPTIMIZE", "REPAIR", "HANDLER",
    # Хранимые процедуры / prepared statements
    "EXEC", "EXECUTE", "CALL", "PREPARE", "DEALLOCATE",
    # PostgreSQL
    "COPY", "LISTEN", "NOTIFY", "UNLISTEN",
    # Общее
    "LOAD",
]


# ----------------------------------------------------------------------
# Опасные паттерны (регексп)
# ----------------------------------------------------------------------
DANGEROUS_PATTERNS = [
    r"\bINTO\s+OUTFILE\b",
    r"\bINTO\s+DUMPFILE\b",
    r"\bINTO\s+@\w+",           # MySQL user variables: SELECT ... INTO @x
    r"\bFOR\s+UPDATE\b",
    r"\bFOR\s+SHARE\b",
    r"\bLOCK\s+IN\s+SHARE\s+MODE\b",
    r"\bLOAD_FILE\s*\(",
    r"\bPROCEDURE\s+\w+",
    r"\bSLEEP\s*\(",
    r"\bBENCHMARK\s*\(",
    r"\bWAITFOR\s+DELAY\b",
    r"\bPG_SLEEP\s*\(",
    r"\bDBLINK\s*\(",
    r"\bCOPY\s+.+\s+FROM\s+PROGRAM\b",
    r"\bLOAD\s+DATA\s+INFILE\b",
    r"\bSET\s+(GLOBAL|SESSION|@@)\b",
]


# Ограничение длины SQL (защита от DoS)
_MAX_SQL_LENGTH = 100_000  # 100 KB


def _strip_comments_for_check(sql: str) -> str:
    """Удаляет SQL-комментарии для проверки forbidden keywords.

    ВАЖНО: только для анализа, не для выполнения. Вырезаем:
      - /* ... */ (многострочные, вложенные не поддерживаются — как в MySQL)
      - -- ... \n (однострочные)
      - # ... \n (MySQL)
    """
    # Многострочные
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    # Однострочные -- и #
    sql = re.sub(r"(?<![\w-])--(\s[^\n]*)?$", " ", sql, flags=re.MULTILINE)
    sql = re.sub(r"#[^\n]*", " ", sql)
    return sql


def validate_sql(sql: str) -> tuple[bool, str]:
    """Проверяет безопасность SQL-запроса.

    Args:
        sql: Строка с SQL-запросом.

    Returns:
        (True, "") — если запрос безопасен.
        (False, reason) — если запрещён.
    """
    if not sql or not sql.strip():
        return False, "empty sql"

    if len(sql) > _MAX_SQL_LENGTH:
        return False, f"sql too long ({len(sql)} > {_MAX_SQL_LENGTH})"

    if "\x00" in sql:
        return False, "null byte in sql"

    # Нормализация: убираем завершающие ; и пробелы
    cleaned = re.sub(r";\s*$", "", sql.strip())

    if not cleaned:
        return False, "empty sql after cleanup"

    # Проверка на множественные запросы: любая ; в середине — отказ
    # (после удаления завершающей ;)
    if ";" in cleaned:
        return False, "multiple statements"

    # Fail-closed: SQL должен успешно разбираться sqlglot. Если парсер
    # не справился — отклоняем: невалидный/обфусцированный SQL может
    # обойти регексп-проверки ниже (prompt injection).
    try:
        statements = [
            s for s in sqlglot.parse(cleaned, read="mysql") if s is not None
        ]
    except Exception as e:
        return False, f"sql parse error: {e}"

    if not statements:
        return False, "empty statement after parsing"

    if len(statements) > 1:
        return False, "multiple statements"

    # Для проверки forbidden keywords вырезаем комментарии
    check_sql = _strip_comments_for_check(cleaned)
    check_upper = check_sql.strip().upper()

    if not check_upper:
        return False, "empty sql after comment removal"

    # Проверка начала запроса (SELECT или WITH)
    if not (check_upper.startswith("SELECT") or check_upper.startswith("WITH")):
        return False, "only SELECT or WITH allowed"

    # Запрещённые ключевые слова — как отдельные токены
    for keyword in FORBIDDEN_KEYWORDS:
        pattern = r"\b" + re.escape(keyword) + r"\b"
        if re.search(pattern, check_upper):
            return False, f"forbidden keyword: {keyword}"

    # Опасные паттерны
    for pat in DANGEROUS_PATTERNS:
        if re.search(pat, check_upper):
            return False, f"forbidden pattern: {pat}"

    return True, ""


def validate_sql_verbose(sql: str) -> tuple[bool, str, str]:
    """Расширенная версия: возвращает (is_valid, reason, cleaned_sql).

    cleaned_sql — то, что можно безопасно выполнять (без завершающей ;).
    Полезно для отладки и для engine: не нужно повторно чистить.
    """
    is_valid, reason = validate_sql(sql)
    cleaned = re.sub(r";\s*$", "", (sql or "").strip())
    return is_valid, reason, cleaned


def log_violation(user_id: str, sql: str, reason: str) -> None:
    """Записывает нарушение безопасности SQL.

    Пишет в два места:
      1. logging.getLogger("security") — настраивается в main.py
         (RotatingFileHandler → logs/security.log). Используется, если
         логгер сконфигурирован.
      2. logging.getLogger(__name__).warning — для консоли (всегда).

    Fallback: если у логгера "security" нет ни одного хендлера (например,
    validator используется в тестах без main.py) — пишем напрямую в файл,
    как в старой версии, но с санитизацией.

    Args:
        user_id: Идентификатор пользователя.
        sql: Строка с SQL-запросом (может быть длинной — обрежем).
        reason: Причина отклонения.
    """
    # Санитизация SQL для лога
    safe_sql = str(sql or "")
    if len(safe_sql) > 1000:
        safe_sql = safe_sql[:1000] + "...(truncated)"
    # Заменяем переводы строк, чтобы не ломать формат
    safe_sql = safe_sql.replace("\n", "\\n").replace("\r", "\\r")

    timestamp = datetime.now().isoformat()
    log_entry = (
        f"{timestamp} | user={user_id} | reason={reason} | sql={safe_sql}"
    )

    # 1. Через логгер "security" (с ротацией, если настроен в main.py)
    if _security_logger.handlers:
        _security_logger.warning(log_entry)
    else:
        # Fallback: пишем напрямую в файл (без ротации)
        try:
            os.makedirs("logs", exist_ok=True)
            with open("logs/security.log", "a", encoding="utf-8") as f:
                f.write(log_entry + "\n")
        except Exception as e:
            _app_logger.error("Не удалось записать security.log: %s", e)

    # 2. Всегда дублируем в консоль через app-логгер
    _app_logger.warning("SQL violation: %s", log_entry)
