# app/core/validator.py
dv_file_version = '261030.01'
r"""Валидатор SQL-запросов: проверка безопасности."""

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
      - -- ... \n (однострочные; пробел после `--` не требуется — SQLite)
      - # ... \n (MySQL)

    Комментарии вырезаются только ВНЕ строковых литералов: ``'...'`` и
    ``"..."``. Внутри литералов учитываются экранирование (``\'``, ``\"``)
    и удвоение кавычек (``''``, ``""``). Незакрытый литерал «съедает»
    остаток строки — это безопасное (fail-closed) поведение: комментарий
    не будет вырезан, и forbidden keyword, если он есть, поймается.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    quote: str | None = None  # текущий открытый литерал: ' или "

    while i < n:
        ch = sql[i]

        if quote is not None:
            # Внутри строкового литерала — копируем всё как есть
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                # Экранирование: \' или \" (и любые \x) — переносим парой
                out.append(sql[i + 1])
                i += 2
                continue
            if ch == quote:
                # Удвоенная кавычка внутри литерала ('' или "") не закрывает
                if i + 1 < n and sql[i + 1] == quote:
                    out.append(sql[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue

        # Вне литералов
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue

        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            # Однострочный комментарий -- ... до конца строки
            nl = sql.find("\n", i)
            out.append(" ")
            i = n if nl == -1 else nl
            continue

        if ch == "#":
            # MySQL-комментарий # ... до конца строки
            nl = sql.find("\n", i)
            out.append(" ")
            i = n if nl == -1 else nl
            continue

        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            # Многострочный комментарий /* ... */ (без вложенности)
            end = sql.find("*/", i + 2)
            out.append(" ")
            i = n if end == -1 else end + 2
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def _has_statement_separator(sql: str) -> bool:
    """True, если `;` встречается ВНЕ строковых литералов.

    Страховка после вырезания комментариев: sqlglot может счесть
    ``SELECT 1; -- DROP`` одним statement'ом (после `;` идёт только
    комментарий), но для безопасности такой ввод отклоняем. При этом
    `;` внутри строковых литералов (``'a;b'``) считаем обычным символом.

    Учитываются экранирование (``\\'``, ``\\"``) и удвоение кавычек
    (``''``, ``""``).

    Args:
        sql: Строка SQL без комментариев (например, результат
            `_strip_comments_for_check`).

    Returns:
        True, если найден `;` вне литералов.
    """
    quote: str | None = None
    i = 0
    n = len(sql)

    while i < n:
        ch = sql[i]

        if quote is not None:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                if i + 1 < n and sql[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue

        if ch in ("'", '"'):
            quote = ch
        elif ch == ";":
            return True
        i += 1

    return False


def strip_sql_comments(sql: str) -> str:
    """Удаляет SQL-комментарии ВНЕ строковых литералов.

    В отличие от _strip_comments_for_check, эта функция
    предназначена для ПЕРЕДАЧИ SQL В БД: она возвращает
    SQL без комментариев --, #, /* */ (вне литералов), но
    сохраняет все литералы, экранирование и удвоение кавычек.

    Если после вырезания комментариев остаётся пустая строка,
    возвращает исходный sql (не хотим отправлять в БД пустоту).

    Args:
        sql: Строка с SQL-запросом.

    Returns:
        SQL без комментариев (stripped) либо исходная строка, если
        после вырезания ничего не осталось.
    """
    cleaned = _strip_comments_for_check(sql)
    if not cleaned.strip():
        return sql
    # убираем "висящие" пробелы от вырезанных комментариев
    return cleaned.strip()


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

    # Множественные запросы определяются через sqlglot.parse ниже
    # (len(statements) > 1). Проверка `";" in cleaned` здесь больше не
    # используется: она ложно блокировала запросы, где `;` находится
    # внутри SQL-комментария (`SELECT 1 -- ; DROP TABLE x`).
    #
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

    # Страховка: `;` вне строковых литералов в тексте без комментариев
    # означает второй statement. sqlglot уже отсёк явные случаи, но
    # `SELECT 1; -- DROP` он считает одним statement'ом (после `;` идёт
    # только комментарий), поэтому сохраняем строгий отказ для него.
    if _has_statement_separator(check_sql):
        return False, "multiple statements"

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
