# app/core/engine.py
dv_file_version = '261030.08'
"""Ядро repdix: диспетчер сообщений, не зависящий от мессенджера."""

import asyncio
import hashlib
import html
import json
import logging
import os
import re
import time
from datetime import datetime

import sqlglot
import yaml
from sqlglot import exp

from app.core.session import SessionState, SessionStore
from app.core.technical_filter import is_technical, is_technical_qualified
from app.core.validator import log_violation, strip_sql_comments, validate_sql
from app.core.rbac_guard import RBACGuard
from app.core.sql_complexity import SQLTooComplex, check_complexity
from app.core.manual_sql_audit import log_manual_sql
from app.interfaces.base import ChatMessage, ChatResponse

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("audit")


# ----------------------------------------------------------------------
# Константы
# ----------------------------------------------------------------------
TIME_MARKERS = ["когда", "дата", "время", "последние", "последняя", "свежие", "newest", "latest"]

STRUCTURE_MARKERS = ["описание", "поля", "структура", "метаданные"]

# Служебные токены, которые не должны считаться упоминанием таблицы
# (иначе "покажи data" подтянет случайную таблицу с таким фрагментом).
_TABLE_TOKEN_STOPWORDS = {
    "data", "table", "tables", "show", "select", "from", "where", "with",
    "this", "that", "what", "when", "last", "latest", "json", "text",
    "the", "and", "for", "not", "all", "any", "new", "old", "top", "count",
}


# ----------------------------------------------------------------------
# Инструменты для LLM. ВАЖНО: get_table_schema сюда НЕ добавлять
# (см. README: "Мы отказались от тяжелого get_table_schema").
# Модель видит схему в системном промпте и не нуждается в SHOW COLUMNS
# (SHOW не пройдёт validate_sql — только SELECT/WITH).
# ----------------------------------------------------------------------
TOOLS_DEFINITION: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "execute_sql",
            "description": "Выполнить SQL-запрос SELECT в текущей базе данных",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "SQL-запрос (только SELECT)"}
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_relations",
            "description": "Получить список связей для указанной таблицы (какие поля ссылаются на другие таблицы).",
            "parameters": {
                "type": "object",
                "properties": {"table_name": {"type": "string", "description": "Имя таблицы"}},
                "required": ["table_name"],
            },
        },
    },
]


# ----------------------------------------------------------------------
# Промпт (вынесен из метода для читаемости и возможного hot-reload в будущем)
# ----------------------------------------------------------------------
def _build_base_prompt(
    db_type: str,
    schemas: list[str] | None = None,
    default_schema: str = "",
) -> str:
    """Собирает системный промпт с учётом диалекта БД.

    Args:
        db_type: "mysql", "postgresql" или "sqlite".
        schemas: Доступные пользователю схемы (multi-schema).
        default_schema: Схема, подставляемая, если в SQL схема не указана.
    """
    db_type = (db_type or "mysql").lower()
    schemas_line = (
        f"Доступные схемы: {', '.join(schemas or []) or '—'}. "
        f"Схема по умолчанию: {default_schema or '—'}. "
        "Всегда указывай схему перед именем таблицы. JOIN между схемами разрешён, "
        "если обе схемы в списке доступных.\n"
    )

    if db_type == "mysql":
        dialect_line = (
            "Ты работаешь с базой данных MySQL (диалект MySQL 8.0). "
            "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать синтаксис PostgreSQL "
            "(FILTER (WHERE...), ILIKE и т.д.). Для условий используй "
            "SUM(CASE WHEN ... THEN 1 ELSE 0 END). "
        )
        info_schema_line = (
            "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО делать запросы к information_schema или SHOW TABLES. "
            "Вся необходимая информация о структуре таблиц уже передана тебе в блоке "
            "'Доступные таблицы и поля'. Дополнительные запросы к системным каталогам не требуются. "
        )
    elif db_type == "postgresql":
        dialect_line = (
            "Ты работаешь с базой данных PostgreSQL (диалект PostgreSQL 15+). "
            "Для условий используй SUM(CASE WHEN ... THEN 1 ELSE 0 END) или FILTER (WHERE ...). "
        )
        info_schema_line = (
            "Структура таблиц уже передана тебе в блоке 'Доступные таблицы и поля'. "
            "Дополнительные запросы к системным каталогам не требуются. "
        )
    else:  # sqlite
        dialect_line = (
            "Ты работаешь с базой данных SQLite. "
            "Для условий используй SUM(CASE WHEN ... THEN 1 ELSE 0 END). "
        )
        info_schema_line = (
            "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО делать запросы к sqlite_master без необходимости. "
            "Вся необходимая информация о структуре таблиц уже передана тебе в блоке "
            "'Доступные таблицы и поля'. Дополнительные запросы к системным каталогам не требуются. "
        )

    return (
        dialect_line
        + info_schema_line
        + schemas_line
        + "Ты — старший аналитик данных (Senior Data Analyst). Твоя задача — не просто "
        "находить цифры, а находить ПРИЧИНЫ явлений (почему упали продажи, почему выросла "
        "нагрузка, кто виноват в простое).\n"
        "У тебя есть доступ к базе данных через инструменты:\n"
        "1. execute_sql — выполнить любой SELECT-запрос.\n"
        "2. get_relations — узнать связи таблиц.\n\n"
        "ПРАВИЛА АНАЛИЗА:\n"
        "- Если пользователь задаёт сложный вопрос ('почему упали продажи?', 'в чём причина?', "
        "'сравни'), ты ДОЛЖЕН выполнить НЕСКОЛЬКО SQL-запросов подряд, чтобы собрать данные.\n"
        "- Например, сначала запроси общее количество продаж за прошлый месяц, затем за этот. "
        "Найди разницу.\n"
        "- Затем запроси детализацию по операторам или по дням, чтобы найти аномалии.\n"
        "- Только получив все данные, напиши аналитический вывод: 'Продажи упали на X%. "
        "Основная причина — падение активности оператора Y (с 10 до 2 продаж).'\n"
        "- СРАВНИВАЙ С ПРЕДЫДУЩИМ ПЕРИОДОМ. Если вопрос про 'за последний месяц/квартал' — "
        "запроси также данные за предыдущий такой же период и укажи динамику (рост/падение в %). "
        "Без сравнения ответ неполный.\n"
        "- ИЩИ УЗКОЕ МЕСТО (bottleneck). Если пользователь спрашивает 'как идут дела' / "
        "'какой статус' / 'что происходит' — определи, какой этап или группа ограничивает "
        "процесс (например, 'операторы не успевают', 'судья завис', 'нет новых данных'). "
        "Назови его явно.\n"
        "- УКАЗЫВАЙ ИСТОЧНИК ЦИФР ТОЛЬКО В СЛОЖНЫХ АНАЛИТИЧЕСКИХ ОТВЕТАХ "
        "(когда пользователь спрашивает 'почему', 'как дела', 'проанализируй', "
        "'когда получим'). Для простых запросов (топ-N, список, сколько записей) "
        "НЕ пиши имена таблиц и полей — просто выдавай результат. Пользователю "
        "нужны данные, а не SQL-схема.\n"
        "- ЕСЛИ ДАННЫЕ УСТАРЕЛИ — сообщи об этом. Например: 'последняя сборка 22.09, новых "
        "данных 5 недель нет'. Это важнее абсолютных цифр.\n"
        "- УЧИТЫВАЙ ДОЛГОСРОЧНУЮ ПАМЯТЬ. Если в блоке 'Долгосрочная память пользователя' "
        "есть факт, ограничивающий область (например, 'работаю только с центром X', "
        "'игнорируй технические звонки'), ты ОБЯЗАН применить его как фильтр в SQL "
        "(WHERE center_name = 'X'), даже если пользователь не повторил это в текущем "
        "вопросе. Память — это постоянное условие, а не просто контекст.\n\n"
        "ПРАВИЛА SQL:\n"
        "- ПРАВИЛО ИДЕНТИФИКАТОРОВ (MySQL): ВСЕГДА оборачивай имена таблиц, колонок и "
        "алиасов в обратные кавычки (`). Например: SELECT `center_name`, "
        "`appointment_revenue` FROM `db_analysis`.`pbi_centers_monthly`. Это защищает "
        "от ошибок, если имя совпадает с зарезервированным словом MySQL (order, group, "
        "interval, key и др.) или содержит спецсимволы. Никогда не пиши имена без кавычек.\n\n"
        "ПРАВИЛА ВЫВОДА:\n"
        "- Отвечай МАКСИМАЛЬНО КРАТКО. Только суть, цифры и факты. Никаких вступлений.\n"
        "- Если доступ запрещён или данных нет — пиши ОДНО предложение. НЕ ИЗВИНЯЙСЯ.\n"
        "- В статусах (перед вызовом инструмента) пиши ОДНО короткое предложение (до 7 слов).\n"
        "- Если пользователь просит показать данные — выводи ТОЛЬКО данные.\n"
        "- Не выводи Markdown-таблицы с '|'. Используй списки и абзацы.\n"
        "- НЕ УПОМИНАЙ имена таблиц и полей (pbi_centers_monthly, "
        "appointment_revenue) в обычных ответах. Это техническая информация. "
        "Исключение — если пользователь сам спросил, из какой таблицы взяты данные.\n"
        "- ФОРМАТИРУЙ ОТВЕТ В HTML: <b>жирный</b> для ключевых цифр, <code>код</code> "
        "для имён таблиц и полей. НЕ используй Markdown (**жирный**, *курсив*, "
        "`код`) — Telegram не рендерит его без parse_mode, а с parse_mode "
        "ломается на подчёркиваниях в именах таблиц.\n"
        "- В КОНЦЕ ответа — блок 'Что делать' с 1–3 конкретными действиями, "
        "отсортированными по приоритету (сначала самое важное). Например: '1. Разгрузить 717 "
        "необработанных диалогов между 3 операторами (сейчас 31 в работе). 2. Прогнать 56 "
        "диалогов to_judge (висят с сентября). 3. Запустить build_dataset (последний — 22.09).'\n"
        "- ВЫДЕЛЯЙ КЛЮЧЕВЫЕ ЦИФРЫ тегом <b> (<b>748 диалогов</b>, "
        "<b>96% не тронуты</b>). "
        "Не выделяй всё подряд — только 3–5 самых важных чисел.\n"
        "- СТРУКТУРИРУЙ ответ. Для сложных вопросов используй короткие разделы: 'Объём', "
        "'Работа операторов', 'Судья', 'Вывод'. Для простых — один абзац.\n"
        "- НЕ ПИШИ ВОДУ. Убирай фразы типа 'как видно из данных', 'стоит отметить', "
        "'важно понимать'. Сразу к сути.\n\n"
        "КАТЕГОРИЧЕСКОЕ ПРАВИЛО ПО ОТКАЗАМ: Если инструмент вернул сообщение, "
        "начинающееся с '⛔ Доступ запрещен', ты ОБЯЗАН сообщить пользователю "
        "причину отказа из этого сообщения. НЕ придумывай результат, НЕ пиши "
        "'Результат: пусто' и НЕ пытайся обойти запрет другими запросами.\n\n"
        "КРИТИЧЕСКОЕ ПРАВИЛО: Ты ОБЯЗАН перед КАЖДЫМ вызовом инструмента написать "
        "пользователю краткий комментарий о том, что ты собираешься сделать. "
        "Например: 'Сейчас посмотрю структуру таблицы...', 'Выполню запрос для группировки по дням...'. "
        "Без этого пользователь подумает, что бот завис.\n"
        "КАТЕГОРИЧЕСКОЕ ПРАВИЛО: Тебе разрешено использовать ТОЛЬКО те таблицы и поля, "
        "которые перечислены в блоке 'Доступные таблицы и поля'. НЕ ПЫТАЙСЯ угадать другие. "
        "Если таблицы нет в списке — значит, у пользователя нет к ней доступа. "
        "Не делай SELECT * для проверки, если тебе не разрешены все поля. "
        "Если ты попытаешься использовать запрещённую таблицу, запрос будет заблокирован.\n"
        "КАТЕГОРИЧЕСКОЕ ПРАВИЛО ПО СХЕМАМ: Всегда указывай имя схемы (базы данных) перед "
        "именем таблицы в SQL-запросах. Например, 'db_itzamna.c2t_queue_analysis', "
        "а НЕ просто 'c2t_queue_analysis'. Это обязательно для безопасности. "
        "Если ты не укажешь схему, запрос может быть заблокирован.\n"
    )


class RepdixEngine:
    """Главный диспетчер: принимает ChatMessage, возвращает ChatResponse."""

    def __init__(self, db_manager=None, llm_client=None, cache_manager=None,
                 default_db: str | None = None, user_manager=None) -> None:
        self.db_manager = db_manager
        self.llm_client = llm_client
        self.cache_manager = cache_manager
        self.default_db = default_db
        self.user_manager = user_manager
        # technical_columns_getter=None: RBACGuard.validate_sql синхронный,
        # а db_manager асинхронный — реализовать getter без блокировки нельзя.
        # Защита SELECT * по техническим колонкам дополнительно выполняется
        # фильтрацией результата в _format_sql_result().
        self.rbac = RBACGuard(technical_columns_getter=None)
        self.sessions = SessionStore(default_db=default_db)
        self.max_history = 10
        self.tool_model = "tool_calling_model"
        self.show_model_in_response = os.environ.get("SHOW_MODEL_IN_RESPONSE", "false").lower() == "true"
        self.max_rows_in_response = int(os.environ.get("MAX_ROWS_IN_RESPONSE") or "100")
        self.max_message_length = int(os.environ.get("MAX_MESSAGE_LENGTH") or "4000")
        # AGENT_MAX_STEPS: лимит шагов агентского цикла (README, .env).
        # Нечисловое значение не должно ронять инициализацию — fallback 10.
        try:
            self.agent_max_steps = max(
                1, int(os.environ.get("AGENT_MAX_STEPS") or "10")
            )
        except (TypeError, ValueError):
            logger.warning(
                "Некорректное значение AGENT_MAX_STEPS=%r, использую 10",
                os.environ.get("AGENT_MAX_STEPS"),
            )
            self.agent_max_steps = 10
        # Пороги проверки сложности SQL (см. app/core/sql_complexity.py).
        # Читаются один раз при старте: нечисловые значения дают fallback.
        self.sql_max_joins = self._env_int("SQL_MAX_JOINS", 5)
        self.sql_max_depth = self._env_int("SQL_MAX_SUBQUERY_DEPTH", 3)
        self.sql_max_unions = self._env_int("SQL_MAX_UNIONS", 3)

    # ---------- Динамическая схема: настройки ----------
    # Сколько последних сообщений истории учитывать при поиске таблиц
    _SCHEMA_HISTORY_MESSAGES = 4

    # Смысловые подсказки: корень из запроса -> фрагменты имён таблиц
    _TABLE_KEYWORD_HINTS: dict[str, tuple[str, ...]] = {
        "центр": ("center", "centers"),
        "выручк": ("revenue", "appointment", "sales"),
        "продаж": ("sale", "sales", "revenue"),
        "запис": ("appointment", "appointments", "booking"),
        "приём": ("appointment", "appointments"),
        "прием": ("appointment", "appointments"),
        "диалог": ("dialog", "dialogs", "queue", "c2t"),
        "оператор": ("operator", "operators", "reviewer"),
        "судья": ("judge", "to_judge"),
        "очеред": ("queue",),
        "сборк": ("dataset",),
        "рейтинг": ("rating", "top"),
        "отзыв": ("review", "reviews"),
    }

    # ---------- Хелперы ----------
    @staticmethod
    def _env_int(name: str, default: int, minimum: int = 0) -> int:
        """Читает целочисленную настройку из env с безопасным fallback.

        Нечисловое значение не должно ронять инициализацию — пишем WARNING
        и возвращаем default.
        """
        raw = os.environ.get(name)
        if raw is None or not str(raw).strip():
            return default
        try:
            return max(minimum, int(str(raw).strip()))
        except (TypeError, ValueError):
            logger.warning(
                "Некорректное значение %s=%r, использую %d", name, raw, default,
            )
            return default

    def _get_default_schema(self, state: SessionState) -> str:
        """Возвращает имя схемы по умолчанию для текущей БД.

        Для MySQL — имя базы (database), для PostgreSQL — database (обычно
        public, но может быть переопределён), для SQLite — "".
        """
        if not self.db_manager or not state.current_db:
            return "public"
        try:
            db_conf = self.db_manager.get_db_config(state.current_db)
        except Exception:
            return "public"
        db_type = (db_conf.get('type') or '').lower()
        if db_type == 'sqlite':
            # В SQLite схем нет; пустая строка не ломает техническую проверку:
            # is_technical_qualified() ловит короткое имя таблицы без схемы.
            return ""
        db_name = (db_conf.get('database') or '').strip()
        if db_name:
            return db_name.lower()
        # Fallback: для MySQL схема совпадает с именем БД/подключения,
        # для PostgreSQL по умолчанию public.
        if db_type == 'mysql':
            return (state.current_db or '').lower()
        return "public"

    def _get_db_type(self, state: SessionState) -> str:
        """Возвращает тип текущей БД (mysql/postgresql/sqlite)."""
        if not self.db_manager or not state.current_db:
            return "mysql"
        try:
            return self.db_manager.get_db_type(state.current_db)
        except Exception:
            return "mysql"

    # ---------- Форматирование результата SQL ----------
    async def _format_sql_result(self, rows: list[dict], db_name: str | None = None,
                                  schema: str | None = None,
                                  table_name: str | None = None) -> str:
        """Форматирует результат SQL в компактный текст.

        Markdown-таблицы с '|' НЕ используются (противоречило бы промпту).
        Для <5 колонок — вертикальный список, для >=5 — нумерованный список.
        """
        # Вырезаем технические колонки из результата (защита в глубину:
        # RBAC-гард блокирует их в SQL, но фильтруем и здесь).
        filtered_rows: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                filtered_rows.append(row)
                continue
            cleaned = {
                k: v for k, v in row.items()
                if not is_technical(str(k))
            }
            if cleaned:
                filtered_rows.append(cleaned)
        if not filtered_rows and rows:
            # Все колонки оказались техническими: не показываем «пусто»,
            # это скрытый отказ доступа (защита в глубину после RBAC).
            return "⛔ Доступ запрещён: результат содержит только технические колонки."

        rows = filtered_rows

        if not rows:
            return "Результат: пусто"

        original_count = len(rows)
        display_rows = rows[:self.max_rows_in_response]
        if not display_rows:
            return "Результат: пусто"

        columns = list(display_rows[0].keys())
        lines: list[str] = []

        # Определяем текстовые колонки (для них лимит больше)
        text_columns = set()
        if db_name and table_name and self.db_manager:
            try:
                schema_info = await self.db_manager.get_table_schema(
                    db_name, table_name, schema,
                )
                for col in schema_info:
                    if col.get("data_type", "").lower() in ("text", "mediumtext", "longtext", "json"):
                        text_columns.add(col.get("column_name", "").lower())
            except Exception:
                pass

        max_short = 50
        max_long = 4000

        for i, row in enumerate(display_rows, 1):
            parts = []
            for col in columns:
                val = str(row.get(col, ""))
                limit = max_long if col.lower() in text_columns else max_short
                if len(val) > limit:
                    val = val[:limit - 3] + "..."
                parts.append(f"{col}: {val}")
            lines.append(f"{i}. " + ", ".join(parts))

        result_text = "\n".join(lines)

        if original_count > self.max_rows_in_response:
            result_text += f"\n... (показаны первые {self.max_rows_in_response} из {original_count})"

        if len(result_text) > self.max_message_length:
            result_text = result_text[:self.max_message_length - 30] + "\n... (сокращено)"

        return result_text

    def _extract_table_from_sql(self, sql: str) -> tuple[str | None, str] | None:
        """Извлекает (schema, table) из первого FROM-выражения через sqlglot.

        В отличие от regex-версии, корректно обрабатывает обратные кавычки,
        CTE (берётся реальная таблица из CTE, а не имя CTE) и подзапросы
        (приоритет — таблица из внешнего SELECT, затем любая).
        """
        if not sql:
            return None
        try:
            parsed = sqlglot.parse_one(sql, read="mysql")
        except Exception:
            return None
        if parsed is None:
            return None

        cte_names: set[str] = set()
        for cte in parsed.find_all(exp.CTE):
            alias = cte.alias_or_name
            if alias:
                cte_names.add(alias.lower())

        outer_tables: list[exp.Table] = []
        for select in parsed.find_all(exp.Select):
            outer_tables.extend(self._direct_tables(select))

        for table in outer_tables + list(parsed.find_all(exp.Table)):
            name = (table.name or "").lower()
            if not name or name in cte_names:
                continue
            raw_db = table.db
            if isinstance(raw_db, exp.Identifier):
                schema = (raw_db.name or "").lower()
            elif isinstance(raw_db, str):
                schema = raw_db.lower()
            else:
                schema = ""
            return (schema or None), name
        return None

    @staticmethod
    def _split_qualified(name: str, default_schema: str = "") -> tuple[str, str]:
        """Делит "schema.table" на (schema, table); короткое имя — default_schema."""
        raw = str(name or "")
        if "." in raw:
            schema, table = raw.split(".", 1)
            return schema.lower(), table
        return (default_schema or "").lower(), raw

    @staticmethod
    def _direct_tables(select: exp.Select) -> list[exp.Table]:
        """Таблицы из FROM/JOIN конкретного SELECT (без подзапросов)."""
        result: list[exp.Table] = []
        from_clause = select.args.get("from") or select.args.get("from_")
        if from_clause is not None and isinstance(from_clause.this, exp.Table):
            result.append(from_clause.this)
        for join in select.args.get("joins") or []:
            if isinstance(join.this, exp.Table):
                result.append(join.this)
        return result

    # ---------- Роутинг моделей ----------
    def _route_query(self, text: str, state: SessionState) -> str:
        """Возвращает выбранную пользователем модель или tool_model по умолчанию."""
        if state.current_model is not None:
            return state.current_model
        return self.tool_model

    # ---------- Извлечение имени таблицы из истории ----------
    def _extract_last_table_from_history(self, state: SessionState) -> str | None:
        """Ищет последнюю таблицу в state.last_sqls, затем в history.

        Возвращает "schema.table", если схема была указана в SQL, иначе
        короткое имя таблицы.
        """

        def _extract(content: str) -> str | None:
            m = re.search(r'\bFROM\s+([a-zA-Z0-9_\.]+)', content, re.IGNORECASE)
            if not m:
                return None
            raw = m.group(1)
            if raw.split(".")[-1].lower() in {"information_schema", "sqlite_master"}:
                return None
            return raw

        # 1) Проверяем last_sqls в обратном порядке
        for sql in reversed(state.last_sqls):
            found = _extract(sql)
            if found:
                return found

        # 2) Ищем в истории
        for msg in reversed(state.history):
            found = _extract(str(msg.get("content", "")))
            if found:
                return found

        return None

    # ---------- Последняя дата из таблицы ----------
    @staticmethod
    def _quote_ident(name: str, db_type: str) -> str:
        """Экранирует идентификатор по диалекту БД.

        MySQL — обратные кавычки, PostgreSQL/SQLite — двойные кавычки.
        Экранирование делается даже после валидации: имя может попасть в
        SQL из истории/конфига, защита не должна зависеть от одного regex.
        """
        db_type = (db_type or "mysql").lower()
        if db_type == "mysql":
            return "`" + str(name).replace("`", "``") + "`"
        return '"' + str(name).replace('"', '""') + '"'

    async def _try_get_last_date(self, state: SessionState, table_name: str) -> str | None:
        """MAX(<дата-колонка>) для таблицы с учётом схемы и диалекта БД.

        table_name может быть как коротким ("dialogs"), так и
        fully-qualified ("db_itzamna.dialogs"). Если схема не указана —
        подставляется схема по умолчанию текущей БД.
        """
        if state.current_db is None or self.db_manager is None:
            return None

        schema_part, table_part = self._split_qualified(
            table_name, self._get_default_schema(state),
        )

        # Имена попадают в SQL: пускаем только безопасные идентификаторы.
        _ident_re = r"[a-zA-Z0-9_]+"
        if not re.fullmatch(_ident_re, table_part or ""):
            return None
        if schema_part and not re.fullmatch(_ident_re, schema_part):
            return None

        try:
            columns = await self.db_manager.get_table_schema(
                state.current_db, table_part, schema_part or None,
            )
        except Exception:
            return None
        column_names = {
            c.get("column_name", "").lower() for c in (columns or [])
        }
        date_columns = ["created_at", "updated_at", "date", "timestamp", "inserted_at"]

        db_type = self._get_db_type(state)
        full_table = (
            f"{self._quote_ident(schema_part, db_type)}."
            f"{self._quote_ident(table_part, db_type)}"
            if schema_part
            else self._quote_ident(table_part, db_type)
        )

        for col in date_columns:
            if col not in column_names:
                continue
            # Валидация имени колонки (оно берётся из БД, но перестрахуемся)
            if not re.fullmatch(r"[a-zA-Z0-9_]+", col):
                continue
            # MySQL падает на зарезервированных словах (date, timestamp),
            # поэтому колонка и таблица всегда квотируются по диалекту.
            sql = (
                f"SELECT MAX({self._quote_ident(col, db_type)}) AS last_date "
                f"FROM {full_table}"
            )
            try:
                rows = await self.db_manager.execute_query(state.current_db, sql)
                if rows and rows[0].get("last_date") is not None:
                    return str(rows[0]["last_date"])
            except Exception:
                continue
        return None

    # ---------- Связи таблиц (текстом) ----------
    def _get_relations_text(self, allowed_tables: list[str] | None = None) -> str:
        """Загружает связи из config/relations.yaml.

        Если allowed_tables задан — фильтрует по полному имени (schema.table).
        """
        try:
            with open("config/relations.yaml", "r", encoding="utf-8") as f:
                relations_data = yaml.safe_load(f)
        except (FileNotFoundError, yaml.YAMLError):
            return ""

        if not relations_data:
            return ""

        relations = relations_data.get("relations", {})
        if not relations:
            return ""

        allowed_set = set(t.lower() for t in allowed_tables) if allowed_tables is not None else None

        lines = ["Связи таблиц:"]
        for table_name, rels in relations.items():
            clean_table_name = table_name.lower()

            # Технические таблицы-источники скрываем целиком.
            # Проверяем все части имени: техничной может быть schema-часть.
            if is_technical_qualified(clean_table_name):
                continue

            if allowed_set is not None and clean_table_name not in allowed_set:
                continue

            for rel in rels:
                col = rel.get("column", "")
                ref = rel.get("references", "")
                desc = rel.get("description", "")

                # Технические колонки не раскрываем
                if is_technical_qualified(col):
                    continue

                if "." in ref:
                    ref_table = ".".join(ref.split(".")[:-1]).lower()
                    # Ссылки на технические таблицы/схемы тоже скрываем
                    if is_technical_qualified(ref_table):
                        continue
                    if allowed_set is not None and ref_table not in allowed_set:
                        continue

                lines.append(f"- {table_name}.{col} -> {ref} ({desc})")

        if len(lines) == 1:
            return ""
        return "\n".join(lines)

    # ---------- Схема БД (RAG) ----------
    async def _get_db_schema(self, state: SessionState, user_query: str = "") -> str:
        """Формирует схему БД ТОЛЬКО из разрешённых схем, таблиц и полей.

        Multi-schema: таблицы выводятся как fully-qualified (schema.table).
        Схемы фильтруются через rbac.get_allowed_schemas(), таблицы — через
        rbac.get_allowed_tables(). Для admin разрешены все схемы из
        db_manager.get_schemas().

        Динамическая фильтрация: поля выводятся только для таблиц, релевантных
        запросу (user_query), последним 4 сообщениям истории и последним SQL.
        Если ничего не найдено — отдаём только список имён таблиц, чтобы
        не жечь контекст.
        """
        if state.current_db is None or self.db_manager is None:
            return ""

        user_roles = self.user_manager.get_user_roles(state.user_id) if self.user_manager else []
        logger.debug("RBAC: user_roles для %s: %s", state.user_id, user_roles)

        default_schema = self._get_default_schema(state)
        is_admin = "admin" in user_roles

        # 1) Схемы текущей БД
        try:
            schemas = await self.db_manager.get_schemas(state.current_db)
        except Exception as e:
            logger.error("Ошибка получения схем из БД: %s", e)
            return ""

        # 2) Реальный список таблиц (fully-qualified: schema.table).
        # Получаем ДО get_allowed_tables(): all_tables нужен для разворачивания
        # правил вида "schema.*" (все таблицы схемы) в конкретные таблицы.
        try:
            all_tables = await self.db_manager.get_table_list(state.current_db)
        except Exception as e:
            logger.error("Ошибка получения таблиц из БД: %s", e)
            return ""

        if is_admin:
            allowed_schemas: set[str] = set(schemas)
            allowed_tables_marker: set[str] = {"*"}
            rules: dict[str, dict] = {}
        else:
            allowed_schemas = self.rbac.get_allowed_schemas(
                user_roles, schemas, default_schema,
            )
            allowed_tables_marker = self.rbac.get_allowed_tables(
                user_roles, schemas, default_schema, all_tables=all_tables,
            )
            self.rbac._load_config()
            rules = self.rbac._collect_rules(user_roles)

        wildcard_tables = "*" in allowed_tables_marker
        if not allowed_schemas and not wildcard_tables:
            return "У вас нет доступа ни к одной таблице."

        # 3) Фильтрация по схемам и RBAC-правилам
        target: list[tuple[str, str, dict | None]] = []
        for full_name in all_tables:
            sch, tbl = self._split_qualified(full_name, default_schema)
            if sch not in allowed_schemas:
                continue
            if wildcard_tables:
                table_rules = None if is_admin else self.rbac._match_table_rule(
                    tbl, sch, rules,
                )
                target.append((sch, tbl, table_rules))
                continue
            if f"{sch}.{tbl}".lower() in allowed_tables_marker:
                table_rules = self.rbac._match_table_rule(tbl, sch, rules)
                target.append((sch, tbl, table_rules))

        if not target:
            return "В RBAC разрешены таблицы, но в БД их нет."

        # 4) Динамическая фильтрация: обычно нужны 1–2 таблицы из десятков.
        # Без неё схема с полями всех таблиц съедала большую часть контекста.
        target, matched = self._filter_target_tables(target, state, user_query)

        # 5) Текст для промпта
        schemas_str = ", ".join(sorted(allowed_schemas)) or "—"
        lines = [
            f"Доступные схемы: {schemas_str}. "
            f"Схема по умолчанию: {default_schema or '—'}."
        ]

        if matched:
            for sch, tbl, table_rules in target:
                lines.extend(
                    await self._describe_table(state, sch, tbl, table_rules, all_tables)
                )

            relations_text = self._get_relations_text(
                allowed_tables=[f"{s}.{t}" for s, t, _ in target]
            )
            if relations_text:
                lines.append("")
                lines.append(relations_text)
        else:
            # Ничего не угадали — только имена таблиц (без полей).
            # Модель сама вызовет execute_sql / get_relations, если нужны детали.
            lines.append("")
            lines.append(
                "Таблицы в доступе (поля не показаны — уточните запрос "
                "или используйте execute_sql / get_relations):"
            )
            for sch, tbl, _rules in target:
                full_name = f"{sch}.{tbl}" if sch else tbl
                lines.append(f"  - {full_name}")

        return "\n".join(lines)

    def _recent_history_text(self, state: SessionState) -> str:
        """Текст последних сообщений истории (для поиска упоминаний таблиц)."""
        limit = self._SCHEMA_HISTORY_MESSAGES
        parts = [str(m.get("content", "")) for m in state.history[-limit:]]
        return " ".join(parts).lower()

    @staticmethod
    def _text_mentions_table(text_lower: str, table: str) -> bool:
        """Проверяет, упоминается ли таблица в тексте (полностью или частями).

        Примеры: "pbi_centers" в "покажи pbi_centers_monthly",
        "centers" в "топ по centers".
        """
        if not text_lower or not table:
            return False
        t = table.lower()
        tokens = set(re.findall(r"[a-z0-9_]{3,}", text_lower))
        if not tokens:
            return False
        if t in tokens:
            return True

        # Части составного имени: pbi_centers_monthly -> centers, monthly
        for seg in t.split("_"):
            if len(seg) >= 3 and seg in tokens:
                return True

        # Пользователь написал только часть имени таблицы.
        for token in tokens:
            if token in _TABLE_TOKEN_STOPWORDS:
                continue
            if len(token) >= 4 and token in t:
                return True
        return False

    def _filter_target_tables(
        self,
        target: list[tuple[str, str, dict | None]],
        state: SessionState,
        user_query: str,
    ) -> tuple[list[tuple[str, str, dict | None]], bool]:
        """Оставляет в схеме только таблицы, релевантные вопросу.

        Returns:
            (таблицы, matched). matched=True — найдены конкретные таблицы,
            для них нужно вывести поля. matched=False — ничего не угадали,
            в промпт пойдёт только список имён (экономия токенов).
        """
        if not target:
            return [], False

        query = (user_query or "").lower()
        history_text = self._recent_history_text(state)
        last_sql_text = " ".join(state.last_sqls[-3:]).lower()
        haystack = " ".join((query, history_text, last_sql_text)).strip()

        matched: list[tuple[str, str, dict | None]] = []
        seen: set[tuple[str, str]] = set()

        def _add(item: tuple[str, str, dict | None]) -> None:
            key = (item[0].lower(), item[1].lower())
            if key not in seen:
                seen.add(key)
                matched.append(item)

        # 1) Прямые упоминания имён таблиц (вопрос, история, последние SQL)
        for item in target:
            tbl = item[1]
            if (
                self._text_mentions_table(query, tbl)
                or self._text_mentions_table(history_text, tbl)
                or self._text_mentions_table(last_sql_text, tbl)
            ):
                _add(item)

        # 2) Смысловые подсказки: "центр" -> centers, "выручка" -> revenue
        if not matched:
            needles: set[str] = set()
            for stem, hints in self._TABLE_KEYWORD_HINTS.items():
                if stem in haystack:
                    needles.update(hints)
            if needles:
                for item in target:
                    tbl = item[1].lower()
                    if any(n in tbl for n in needles):
                        _add(item)

        if not matched:
            # Возвращаем полный список — вызывающий выведет только имена.
            return target, False
        return matched, True

    async def _describe_table(self, state: SessionState, schema: str, table: str,
                               rules: dict | None,
                               all_tables: list[str]) -> list[str]:
        """Возвращает строки описания одной таблицы (для _get_db_schema).

        Args:
            state: Состояние сессии.
            schema: Имя схемы.
            table: Короткое имя таблицы.
            rules: Правило из rbac._collect_rules()
                ({"allowed": set, "excluded": set, "has_wildcard": bool})
                или None — поля не фильтруются по RBAC.
            all_tables: Полный список fully-qualified таблиц (проверка
                существования).
        """
        result: list[str] = []

        real_name = None
        for full_name in all_tables:
            sch, tbl = self._split_qualified(full_name, schema)
            if sch == schema and tbl.lower() == table.lower():
                real_name = tbl
                break

        if not real_name:
            logger.warning("RBAC: таблица '%s.%s' не найдена в БД", schema, table)
            return result

        title = f"{schema}.{real_name}" if schema else real_name
        result.append(f"\nТаблица: {title}")

        try:
            real_columns = await self.db_manager.get_table_schema(
                state.current_db, real_name, schema or None,
            )
        except Exception as e:
            logger.error("Ошибка получения колонок для %s: %s", real_name, e)
            return result

        allowed: set[str] = (rules or {}).get("allowed", set()) or set()
        excluded: set[str] = (rules or {}).get("excluded", set()) or set()
        has_wildcard = bool((rules or {}).get("has_wildcard"))

        for col in real_columns:
            c_name = col.get("column_name", "")
            c_type = col.get("data_type", "")
            c_comm = col.get("column_comment", "")
            c_lower = str(c_name).lower()

            if c_lower in excluded:
                continue
            if rules is not None and not has_wildcard and c_lower not in allowed:
                continue

            if c_comm:
                result.append(f"  - {c_name} ({c_type}): {c_comm}")
            else:
                result.append(f"  - {c_name} ({c_type})")

        return result

    # ---------- Auto-compact ----------
    async def _compact_history(self, state: SessionState, model_name: str,
                                messages: list[dict]) -> str | None:
        """Сжимает первые сообщения истории в короткое резюме (auto-compact).

        Args:
            state: Состояние сессии (для логов).
            model_name: Модель для сжатия.
            messages: Сообщения, которые нужно сжать.

        Returns:
            Текст резюме или None при ошибке/пустом ответе. При None история
            остаётся нетронутой (лучше переполнение, чем потеря контекста).
        """
        if self.llm_client is None or not messages:
            return None

        try:
            payload = json.dumps(messages, ensure_ascii=False)
        except (TypeError, ValueError):
            payload = str(messages)

        try:
            response = await self.llm_client.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "Сожми этот диалог в 2 предложения. Сохрани все цифры, "
                            "имена таблиц и условия фильтрации. Без вступлений и воды."
                        ),
                    },
                    {"role": "user", "content": payload},
                ],
                tools=None,
                model_name=model_name,
            )
            summary = (response["choices"][0]["message"].get("content") or "").strip()
        except Exception as e:
            logger.warning(
                "Auto-compact: не удалось сжать историю для %s: %s",
                getattr(state, "user_id", "?"), e,
            )
            return None

        if not summary:
            return None
        # Резюме само не должно съедать контекст
        return summary[:1000]

    # ---------- Главный обработчик ----------
    async def process_message(self, msg: ChatMessage) -> ChatResponse:
        user_id = msg.user_id
        state = self.sessions.get(user_id)
        lock = state.lock

        # default_schema вычисляется ПОСЛЕ загрузки кэша (см. ниже): до
        # восстановления current_db из session:{user_id} схема по умолчанию
        # всегда получалась "public", даже для MySQL.
        default_schema = ""

        if lock.locked():
            return ChatResponse(text="⏳ Пожалуйста, дождитесь ответа на предыдущий вопрос. Я ещё обрабатываю его.")

        await lock.acquire()
        try:
            text = msg.text.strip()
            audit_question = text
            audit_answer = ""

            # ---------- /help ----------
            if text == "/help":
                state.last_model_used = "algorithm (command)"
                try:
                    with open("config/help.md", "r", encoding="utf-8") as f:
                        audit_answer = f.read()
                except FileNotFoundError:
                    audit_answer = "Файл справки не найден. Используйте /db, /model, /sql."
                if self.show_model_in_response:
                    audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                self._log_audit(state, msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)

            # ---------- Загрузка истории из кэша ----------
            if self.cache_manager is not None:
                cache_key = f"repdix:history:{user_id}"
                cached = await self.cache_manager.get(cache_key)
                if cached:
                    try:
                        payload = json.loads(cached)
                    except json.JSONDecodeError:
                        payload = None
                    if isinstance(payload, dict):
                        # Новый формат: {"history": [...], "summary": "..."}
                        state.history = payload.get("history") or []
                        state.summary = payload.get("summary") or ""
                    elif isinstance(payload, list):
                        # Совместимость со старым форматом (только история)
                        state.history = payload
                    else:
                        state.history = []
                else:
                    state.history = []

                # Долгосрочная память (repdix:memory:{user_id}, TTL 30 дней).
                # Не обнуляем список, если ключа нет: факты, добавленные в этой
                # сессии через /remember, должны остаться в памяти процесса.
                mem_key = f"repdix:memory:{user_id}"
                mem_raw = await self.cache_manager.get(mem_key)
                if mem_raw:
                    try:
                        loaded_facts = json.loads(mem_raw)
                    except (json.JSONDecodeError, TypeError):
                        loaded_facts = None
                    if isinstance(loaded_facts, list):
                        state.long_term_facts = [str(f) for f in loaded_facts]

                # Выбранная БД/модель, резюме и последние SQL: отдельный ключ
                # session:{user_id} (формат — JSON-объект). Битый/отсутствующий
                # ключ НЕ сбрасывает значения, уже установленные в этой сессии.
                session_raw = await self.cache_manager.get(f"session:{user_id}")
                if session_raw:
                    try:
                        session_payload = json.loads(session_raw)
                    except (json.JSONDecodeError, TypeError):
                        session_payload = None
                    if isinstance(session_payload, dict):
                        restored_db = session_payload.get("current_db")
                        if isinstance(restored_db, str) and restored_db:
                            # Не восстанавливаем БД, которой больше нет в конфиге.
                            known_dbs = (
                                self.db_manager.get_db_names()
                                if self.db_manager is not None else [restored_db]
                            )
                            if restored_db in known_dbs:
                                state.current_db = restored_db
                        restored_model = session_payload.get("current_model")
                        if isinstance(restored_model, str) and restored_model:
                            state.current_model = restored_model
                        restored_summary = session_payload.get("summary")
                        if isinstance(restored_summary, str) and restored_summary:
                            state.summary = restored_summary
                        restored_sqls = session_payload.get("last_sqls")
                        if isinstance(restored_sqls, list):
                            state.last_sqls = [
                                s for s in restored_sqls if isinstance(s, str)
                            ]

            # Пересчитываем схему по умолчанию уже с восстановленным current_db.
            default_schema = self._get_default_schema(state)

            # ---------- /clear ----------
            if text == "/clear":
                state.last_model_used = "algorithm (command)"
                state.history = []
                state.summary = ""
                if self.cache_manager is not None:
                    await self.cache_manager.delete(f"repdix:history:{user_id}")
                await self._persist_session(state, user_id)
                audit_answer = "История очищена. Долгосрочная память сохранена."
                if self.show_model_in_response:
                    audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                self._log_audit(state, msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)

            # ---------- /tokens ----------
            if text == "/tokens":
                state.last_model_used = "algorithm (command)"
                total_chars = sum(len(str(m)) for m in state.history)
                total_tokens = total_chars // 4
                model_to_use = self._route_query(text, state)
                context_limit = 32768
                if self.llm_client is not None:
                    context_limit = self.llm_client.get_context_length(model_to_use)
                percent = (total_tokens / context_limit * 100) if context_limit > 0 else 0
                audit_answer = (
                    f"Сообщений в истории: {len(state.history)} из {self.max_history}. "
                    f"Лимит контекста: {context_limit} токенов. "
                    f"Текущий размер: ~{total_tokens} токенов ({percent:.1f}%)."
                )
                if self.show_model_in_response:
                    audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                self._log_audit(state, msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)

            # ---------- /history ----------
            if text == "/history":
                state.last_model_used = "algorithm (command)"
                if not state.history:
                    audit_answer = "История диалога пуста."
                    if self.show_model_in_response:
                        audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                    self._log_audit(state, msg, audit_question, audit_answer)
                    return ChatResponse(text=audit_answer)
                lines = []
                for m in state.history:
                    role = m.get("role", "unknown")
                    content = str(m.get("content", ""))[:300]
                    lines.append(f"[{role}] {content}\n")
                audit_answer = "".join(lines)
                if self.show_model_in_response:
                    audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                self._log_audit(state, msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)

            # ---------- /compact ----------
            if text == "/compact":
                if not state.history or len(state.history) < 4:
                    state.last_model_used = "algorithm (command)"
                    audit_answer = "История слишком коротка для сжатия."
                    if self.show_model_in_response:
                        audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                    self._log_audit(state, msg, audit_question, audit_answer)
                    return ChatResponse(text=audit_answer)
                if self.llm_client is None:
                    state.last_model_used = "algorithm (command)"
                    audit_answer = "LLM не подключена, сжатие невозможно."
                    if self.show_model_in_response:
                        audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                    self._log_audit(state, msg, audit_question, audit_answer)
                    return ChatResponse(text=audit_answer)
                try:
                    state.last_model_used = state.current_model or "default"
                    messages = [
                        {"role": "system",
                         "content": "Сожми следующий диалог в краткое резюме (не более 3-4 предложений). Сохрани ключевые факты, вопросы и ответы."},
                        {"role": "user", "content": json.dumps(state.history, ensure_ascii=False)}
                    ]
                    response = await self.llm_client.chat(
                        messages,
                        model_name=state.current_model or self.tool_model,
                    )
                    summary = response["choices"][0]["message"]["content"]
                    state.summary = summary
                    state.history = [{"role": "system", "content": f"Резюме предыдущего диалога: {summary}"}]
                    if self.cache_manager is not None:
                        await self.cache_manager.set(
                            f"repdix:history:{user_id}",
                            json.dumps(
                                {"history": state.history, "summary": state.summary},
                                ensure_ascii=False,
                            ),
                            ttl=86400,
                        )
                    await self._persist_session(state, user_id)
                    audit_answer = f"История сжата. Новый размер: {len(state.history)} сообщений. Резюме: {summary[:300]}..."
                    if self.show_model_in_response:
                        audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                    self._log_audit(state, msg, audit_question, audit_answer)
                    return ChatResponse(text=audit_answer)
                except Exception as e:
                    audit_answer = f"Ошибка сжатия истории: {str(e)}"
                    if self.show_model_in_response:
                        audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                    self._log_audit(state, msg, audit_question, audit_answer)
                    return ChatResponse(text=audit_answer)

            # ---------- /settings ----------
            if text == "/settings":
                state.last_model_used = "algorithm (command)"
                active_model_display = state.current_model or f"авто-выбор (роутер: {self.tool_model})"
                lines = [
                    f"Активная модель: {active_model_display}",
                    f"Лимит истории: {self.max_history}",
                ]
                if self.llm_client is not None:
                    model_for_limit = state.current_model or self.tool_model
                    context_limit = self.llm_client.get_context_length(model_for_limit)
                    lines.append(f"Лимит контекста: {context_limit}")
                else:
                    lines.append("Лимит контекста: недоступен")
                if self.db_manager is not None:
                    lines.append("Доступные БД:")
                    db_names = self.db_manager.get_db_names()
                    if db_names:
                        for n in db_names:
                            lines.append(
                                f"  • {self.db_manager.get_db_display_name(n)}"
                            )
                    else:
                        lines.append("  (нет)")

                    if state.current_db:
                        lines.append(
                            "Текущая БД: "
                            + self.db_manager.get_db_display_name(state.current_db)
                        )
                        settings_default_schema = self._get_default_schema(state)
                        settings_roles = (
                            self.user_manager.get_user_roles(state.user_id)
                            if self.user_manager else []
                        )
                        try:
                            settings_schemas = await self.db_manager.get_schemas(
                                state.current_db
                            )
                        except Exception:
                            settings_schemas = []
                        if "admin" in settings_roles:
                            settings_allowed = set(settings_schemas)
                        else:
                            settings_allowed = self.rbac.get_allowed_schemas(
                                settings_roles,
                                settings_schemas,
                                settings_default_schema,
                            )
                        lines.append(
                            "Доступные схемы: "
                            + (", ".join(sorted(settings_allowed)) or "—")
                        )
                        lines.append(
                            "Схема по умолчанию: "
                            + (settings_default_schema or "—")
                        )
                else:
                    lines.append("Доступные БД: нет")
                if self.llm_client is not None:
                    model_names = self.llm_client.get_model_names()
                    lines.append(f"Доступные модели: {', '.join(model_names) if model_names else 'нет'}")
                else:
                    lines.append("Доступные модели: нет")

                timeout_val = os.environ.get('SQL_QUERY_TIMEOUT', '15')
                lines.append(f"Таймаут SQL: {timeout_val} сек.")

                audit_answer = "\n".join(lines)
                if self.show_model_in_response:
                    audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                self._log_audit(state, msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)

            # ---------- /remember <текст> ----------
            if text == "/remember" or text.startswith("/remember "):
                state.last_model_used = "algorithm (command)"
                fact = text[len("/remember"):].strip()
                if not fact:
                    audit_answer = (
                        "Напишите, что запомнить (например: "
                        "/remember я работаю с центром 15_Типанова)"
                    )
                else:
                    state.long_term_facts.append(fact)
                    await self._save_memory(user_id, state.long_term_facts)
                    audit_answer = f"🧠 Запомнил: {fact}"
                if self.show_model_in_response:
                    audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                self._log_audit(state, msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)

            # ---------- /memory ----------
            if text == "/memory":
                state.last_model_used = "algorithm (command)"
                memory_response = await self.render_memory(user_id, state=state)
                audit_answer = memory_response.text
                if self.show_model_in_response:
                    audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                self._log_audit(state, msg, audit_question, audit_answer)
                return ChatResponse(
                    text=audit_answer,
                    reply_markup=memory_response.reply_markup,
                )

            # ---------- /forget <номер> ----------
            if text == "/forget" or text.startswith("/forget "):
                state.last_model_used = "algorithm (command)"
                arg = text[len("/forget"):].strip()
                idx: int | None = None
                if arg.isdigit():
                    num = int(arg)
                    if 1 <= num <= len(state.long_term_facts):
                        idx = num - 1
                if idx is None:
                    audit_answer = "Факт с таким номером не найден."
                else:
                    state.long_term_facts.pop(idx)
                    await self._save_memory(user_id, state.long_term_facts)
                    audit_answer = (
                        f"🗑 Факт удалён. Осталось: {len(state.long_term_facts)}."
                    )
                if self.show_model_in_response:
                    audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                self._log_audit(state, msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)

            # ---------- /reset (история + память + last_sqls) ----------
            if text == "/reset":
                state.last_model_used = "algorithm (command)"
                state.history = []
                state.summary = ""
                state.long_term_facts = []
                state.last_sqls = []
                if self.cache_manager is not None:
                    await self.cache_manager.delete(f"repdix:history:{user_id}")
                    await self.cache_manager.delete(f"repdix:memory:{user_id}")
                await self._persist_session(state, user_id)
                audit_answer = (
                    "🔄 Всё сброшено: история и долгосрочная память очищены."
                )
                if self.show_model_in_response:
                    audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                self._log_audit(state, msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)

            # ---------- /db <name> ----------
            if text.startswith("/db "):
                state.last_model_used = "algorithm (command)"
                db_name = text[4:].strip()
                available = self.db_manager.get_db_names() if self.db_manager is not None else []
                if db_name not in available:
                    lines = [f"БД '{db_name}' не найдена.", "", "Доступные БД:"]
                    for n in available:
                        lines.append(f"  • {self.db_manager.get_db_display_name(n)}")
                    audit_answer = "\n".join(lines)
                else:
                    state.current_db = db_name
                    await self._persist_session(state, user_id)
                    default_schema = self._get_default_schema(state)
                    display = self.db_manager.get_db_display_name(state.current_db)
                    audit_answer = (
                        f"БД: {display} "
                        f"(схема по умолчанию: {default_schema or '—'})"
                    )
                if self.show_model_in_response:
                    audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                self._log_audit(state, msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)

            if text == "/db":
                state.last_model_used = "algorithm (command)"
                lines: list[str] = []
                if state.current_db and self.db_manager is not None:
                    lines.append(
                        "Текущая БД: "
                        + self.db_manager.get_db_display_name(state.current_db)
                    )
                elif state.current_db:
                    lines.append(f"Текущая БД: {state.current_db}")
                else:
                    lines.append("Текущая БД: не выбрана")
                if self.db_manager is not None:
                    names = self.db_manager.get_db_names()
                    if names:
                        lines.append("")
                        lines.append("Доступные БД:")
                        for n in names:
                            lines.append(
                                f"  • {self.db_manager.get_db_display_name(n)}"
                            )
                audit_answer = "\n".join(lines)
                if self.show_model_in_response:
                    audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                self._log_audit(state, msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)

            # ---------- /model <name> ----------
            if text.startswith("/model "):
                state.last_model_used = "algorithm (command)"
                model_name = text[7:].strip()
                resolved = self.llm_client.resolve_model_name(model_name) if self.llm_client else None
                if resolved:
                    state.current_model = resolved
                    await self._persist_session(state, user_id)
                    audit_answer = f"Модель: {state.current_model}"
                else:
                    available_models = self.llm_client.get_model_names() if self.llm_client else []
                    audit_answer = f"Модель '{model_name}' не найдена или неоднозначна. Доступные: {', '.join(available_models)}"
                if self.show_model_in_response:
                    audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                self._log_audit(state, msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)

            if text == "/model":
                state.last_model_used = "algorithm (command)"
                if state.current_model is None:
                    audit_answer = "Текущая модель: не выбрана (авто-роутер)"
                else:
                    audit_answer = f"Текущая модель: {state.current_model}"
                if self.show_model_in_response:
                    audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                self._log_audit(state, msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)

            # ---------- /models ----------
            if text == "/models":
                state.last_model_used = "algorithm (command)"
                if self.llm_client is not None:
                    names = self.llm_client.get_model_names()
                    audit_answer = "Доступные модели: " + ", ".join(names)
                else:
                    audit_answer = "LLM не подключена"
                if self.show_model_in_response:
                    audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                self._log_audit(state, msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)

            # ---------- /sql <query> ----------
            if text.startswith("/sql "):
                state.last_model_used = "algorithm (command)"
                sql = text[5:].strip()
                is_valid, reason = validate_sql(sql)

                if not is_valid:
                    log_violation(user_id or "unknown", sql, reason)
                    audit_answer = f"SQL отклонён: {reason}"
                    if self.show_model_in_response:
                        audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                    self._log_audit(state, msg, audit_question, audit_answer)
                    return ChatResponse(text=audit_answer)

                # RBAC + технический фильтр.
                # ВАЖНО: проверка выполняется и для админов — иначе технические
                # таблицы/колонки доступны в обход RBAC (и вместо отказа виден
                # "Результат: пусто"). Админ обходит только обычные RBAC-правила.
                if self.user_manager:
                    user_roles = self.user_manager.get_user_roles(user_id) or []
                    default_schema = self._get_default_schema(state)
                    logger.info(
                        "/sql: user_roles=%s, default_schema=%r, sql=%r",
                        user_roles, default_schema, sql,
                    )
                    rbac_ok, rbac_msg = self.rbac.validate_sql(sql, user_roles, default_schema)
                    technical_denied = (not rbac_ok) and "technical" in (rbac_msg or "").lower()

                    # SELECT * по таблице с ограниченным набором полей:
                    # пробуем развернуть звёздочку в явный список разрешённых
                    # полей (RBAC при этом не ослабляется — результат
                    # повторно валидируется).
                    if (
                        not rbac_ok
                        and not technical_denied
                        and self.rbac.is_select_star_denial(rbac_msg)
                    ):
                        expanded_sql = await self._try_expand_select_star(
                            sql, state, user_roles, default_schema,
                            source="manual",
                        )
                        if expanded_sql:
                            sql = expanded_sql
                            rbac_ok, rbac_msg = True, sql

                    if not rbac_ok:
                        if "admin" in user_roles and not technical_denied:
                            logger.info("/sql: admin bypass for non-technical SQL")
                            rbac_ok = True
                            # sql оставляем как есть — это осознанный обход для админа
                        else:
                            log_violation(user_id or "unknown", sql, f"RBAC: {rbac_msg}")
                            audit_answer = f"⛔ Доступ запрещен: {rbac_msg}"
                            if self.show_model_in_response:
                                audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                            self._log_audit(state, msg, audit_question, audit_answer)
                            return ChatResponse(text=audit_answer)
                    else:
                        # rbac_ok=True. Если это была НЕ админ-ветка и validate_sql
                        # вернул нормализованный sql — подставляем его.
                        if rbac_msg and rbac_msg != sql:
                            sql = rbac_msg

                if state.current_db is None:
                    audit_answer = "Сначала выберите БД командой /db <name>"
                    if self.show_model_in_response:
                        audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                    self._log_audit(state, msg, audit_question, audit_answer)
                    return ChatResponse(text=audit_answer)

                # В БД — без комментариев (чистое имя колонки в ответе:
                # cursor.description иначе вернёт "1 -- ; DROP TABLE x").
                # В аудит и last_sqls — оригинал (for forensics).
                sql_for_exec = strip_sql_comments(sql)
                audit_sql = sql

                start_time = time.perf_counter()
                try:
                    rows = await self.db_manager.execute_query(state.current_db, sql_for_exec)
                    duration = time.perf_counter() - start_time
                    if rows is None:
                        rows = []
                    extracted = self._extract_table_from_sql(audit_sql)
                    schema_part, table_part = extracted if extracted else (None, None)
                    audit_answer = await self._format_sql_result(
                        rows, db_name=state.current_db,
                        schema=schema_part, table_name=table_part,
                    )
                    self._log_sql_audit(audit_sql, duration, "success", source="manual")
                    log_manual_sql(
                        user_id=user_id,
                        db_name=state.current_db,
                        sql=audit_sql,
                        duration_ms=duration * 1000,
                        rows_returned=len(rows),
                    )
                    if self.show_model_in_response:
                        audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                    self._log_audit(state, msg, audit_question, audit_answer)
                    return ChatResponse(text=audit_answer)
                except Exception as e:
                    duration = time.perf_counter() - start_time
                    err_str = str(e)
                    self._log_sql_audit(audit_sql, duration, "error", err_str, source="manual")
                    log_manual_sql(
                        user_id=user_id,
                        db_name=state.current_db,
                        sql=audit_sql,
                        duration_ms=duration * 1000,
                        error=err_str,
                    )
                    if "timeout" in err_str.lower() or "MAX_EXECUTION_TIME" in err_str:
                        timeout_val = os.environ.get('SQL_QUERY_TIMEOUT', '15')
                        audit_answer = f"Запрос выполнялся слишком долго (лимит {timeout_val} сек) и был прерван."
                    else:
                        audit_answer = f"Ошибка выполнения запроса: {err_str}"
                    if self.show_model_in_response:
                        audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                    self._log_audit(state, msg, audit_question, audit_answer)
                    return ChatResponse(text=audit_answer)

            # ---------- Детерминированный запрос имени таблицы ----------
            if state.current_db is not None and re.fullmatch(r"[a-zA-Z0-9_]+", text):
                state.last_model_used = "algorithm (deterministic)"
                recent_history_text = " ".join(
                    str(m.get("content", "")) for m in state.history[-2:]
                ).lower()

                has_structure_context = any(marker in recent_history_text for marker in STRUCTURE_MARKERS)
                has_time_context = any(marker in recent_history_text for marker in TIME_MARKERS)

                if has_time_context and not has_structure_context:
                    last_date = await self._try_get_last_date(state, text)
                    if last_date:
                        audit_logger.info(json.dumps({
                            "timestamp": datetime.now().isoformat(),
                            "type": "deterministic_last_date",
                            "table": text,
                            "result": str(last_date),
                        }, ensure_ascii=False))
                        audit_answer = f"Последняя запись в таблице {text}: {last_date}"
                        if self.show_model_in_response:
                            audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                        self._log_audit(state, msg, audit_question, audit_answer)
                        return ChatResponse(text=audit_answer)

                try:
                    rows = await self.db_manager.get_table_schema(state.current_db, text)
                    if not rows:
                        audit_answer = f"Таблица '{text}' не найдена в текущей БД."
                        if self.show_model_in_response:
                            audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                        self._log_audit(state, msg, audit_question, audit_answer)
                        return ChatResponse(text=audit_answer)

                    audit_logger.info(json.dumps({
                        "timestamp": datetime.now().isoformat(),
                        "type": "deterministic_table_schema",
                        "table": text,
                    }, ensure_ascii=False))
                    audit_answer = await self._format_sql_result(rows)
                    if self.show_model_in_response:
                        audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                    self._log_audit(state, msg, audit_question, audit_answer)
                    return ChatResponse(text=audit_answer)
                except Exception as e:
                    audit_answer = f"Ошибка выполнения запроса: {str(e)}"
                    if self.show_model_in_response:
                        audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                    self._log_audit(state, msg, audit_question, audit_answer)
                    return ChatResponse(text=audit_answer)

            # ---------- Обработка через LLM ----------
            if self.llm_client is None:
                state.last_model_used = "none (deterministic)"
                audit_answer = f"Эхо: {msg.text}"
                self._log_audit(state, msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)

            try:
                model_to_use = self._route_query(msg.text, state)
                state.last_model_used = model_to_use

                db_type = self._get_db_type(state)
                prompt_default_schema = self._get_default_schema(state)
                if self.db_manager is not None and state.current_db is not None:
                    try:
                        prompt_schemas = await self.db_manager.get_schemas(
                            state.current_db
                        )
                    except Exception:
                        prompt_schemas = []
                else:
                    prompt_schemas = []
                base_prompt = _build_base_prompt(
                    db_type, prompt_schemas, prompt_default_schema,
                )
                schema = await self._get_db_schema(state, msg.text)

                # Последние выполненные SQL: модель может сделать "то же самое,
                # но за сентябрь", не угадывая запрос заново.
                last_sqls_block = ""
                if state.last_sqls:
                    recent_sqls = state.last_sqls[-3:]
                    last_sqls_block = (
                        "\n\nПоследние выполненные SQL (для контекста):\n"
                        + "\n".join(f"- {s}" for s in recent_sqls)
                        + "\nЕсли пользователь просит 'то же самое, но за другой "
                          "период' — переиспользуй эти запросы, изменив условия "
                          "фильтрации."
                    )

                _now = datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")
                system_content = (
                    base_prompt
                    + f"\n\nСегодня: {_now} (локальное время сервера). "
                    + "Используй эту дату для 'сегодня', 'вчера', 'на этой неделе' и т.п."
                    + last_sqls_block
                    + (("\n\n" + schema) if schema else "")
                )

                # Долгосрочная память: факты, которые пользователь просил
                # запомнить (/remember). Модель обязана их учитывать.
                if state.long_term_facts:
                    system_content += (
                        "\n\nДолгосрочная память пользователя "
                        "(учитывай при ответах):\n"
                        + "\n".join(f"- {fact}" for fact in state.long_term_facts)
                    )

                # Подсказка о последней таблице
                text_lower = msg.text.lower()
                if any(marker in text_lower for marker in TIME_MARKERS):
                    last_table = self._extract_last_table_from_history(state)
                    if last_table:
                        system_content += (
                            f"\n\nПодсказка: Вероятно, речь о таблице '{last_table}'. "
                            "Если имя таблицы не указано явно, используй её для запросов о датах."
                        )

                # Проверка контекста
                context_limit = self.llm_client.get_context_length(model_to_use)
                history_chars = sum(len(str(m)) for m in state.history)
                estimated_tokens = (len(system_content) + history_chars + len(msg.text)) // 3

                # Auto-Compact: при 60% лимита сжимаем НАЧАЛО истории в резюме.
                # Так бот не упирается в лимит и не теряет суть диалога
                # (жёсткая обрезка по 10 сообщениям резала контекст целиком).
                if estimated_tokens > context_limit * 0.6 and len(state.history) >= 4:
                    compacted = await self._compact_history(
                        state, model_to_use, state.history[:4],
                    )
                    if compacted:
                        state.summary = compacted
                        state.history = [
                            {
                                "role": "system",
                                "content": "Резюме предыдущего диалога: " + compacted,
                            }
                        ] + state.history[4:]
                        history_chars = sum(len(str(m)) for m in state.history)
                        estimated_tokens = (
                            len(system_content) + history_chars + len(msg.text)
                        ) // 3

                if estimated_tokens > context_limit * 0.8:
                    if len(state.history) > 2:
                        logger.warning(
                            "Context overflow detected. Trimming history from %d to 2 messages.",
                            len(state.history),
                        )
                        state.history = state.history[-2:]
                        history_chars = sum(len(str(m)) for m in state.history)
                        estimated_tokens = (len(system_content) + history_chars + len(msg.text)) // 3
                        if estimated_tokens > context_limit * 0.8:
                            audit_answer = "Контекст переполнен даже после обрезки. Используйте /compact или /clear."
                            self._log_audit(state, msg, audit_question, audit_answer)
                            return ChatResponse(text=audit_answer)
                    else:
                        audit_answer = "Контекст переполнен. Используйте /compact или /clear."
                        self._log_audit(state, msg, audit_question, audit_answer)
                        return ChatResponse(text=audit_answer)

                messages: list[dict] = [{"role": "system", "content": system_content}]
                messages.extend(state.history[-self.max_history:])
                messages.append({"role": "user", "content": msg.text})

                # ---------- ЦИКЛ АГЕНТА (до 5 шагов) ----------
                max_steps = self.agent_max_steps
                step = 0
                final_text = ""

                while step < max_steps:
                    step += 1

                    response = await self.llm_client.chat(
                        messages,
                        tools=TOOLS_DEFINITION,
                        model_name=model_to_use,
                        tool_choice="auto",
                    )
                    choice = response["choices"][0]["message"]
                    tool_calls = choice.get("tool_calls")

                    # Статус — только если модель реально вызывает инструмент
                    if tool_calls and msg.status_callback:
                        # ВАЖНО: НЕ обрезаем текст — обрезка могла разорвать
                        # HTML-тег (<b>...</b>) и Telegram отвечал
                        # "Can't parse entities". Разметку отдаём как есть,
                        # лишь схлопываем пробелы в одну строку и снимаем теги.
                        clean_text = re.sub(
                            r"\s+", " ", choice.get("content") or "",
                        ).strip()
                        clean_text = re.sub(r"<[^>]+>", "", clean_text)  # статусы — plain text, без HTML
                        if clean_text:
                            await msg.status_callback(f"🤖 {clean_text}")
                        else:
                            tool_name = tool_calls[0]["function"]["name"]
                            await msg.status_callback(f"🤖 Выполняю {tool_name}...")

                    if not tool_calls:
                        final_text = choice.get("content") or ""
                        break

                    messages.append({
                        "role": "assistant",
                        "content": choice.get("content") or "",
                        "tool_calls": tool_calls,
                    })

                    for tool_call in tool_calls:
                        tool_name = tool_call["function"]["name"]

                        if tool_name not in {"execute_sql", "get_relations"}:
                            logger.warning("Unknown tool_call from LLM: %s", tool_name)
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call["id"],
                                "content": f"Ошибка: неизвестный инструмент '{tool_name}'",
                            })
                            continue

                        if tool_name == "get_relations":
                            try:
                                args = json.loads(tool_call["function"]["arguments"])
                                table_name = args.get("table_name", "")
                            except (json.JSONDecodeError, TypeError):
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_call["id"],
                                    "content": "Ошибка: некорректные аргументы инструмента",
                                })
                                continue

                            table_name = str(table_name or "").strip()
                            if not table_name:
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_call["id"],
                                    "content": "Ошибка: не указано имя таблицы",
                                })
                                continue

                            # Технические таблицы не раскрываем даже на уровне
                            # метаданных (как в _get_relations_text).
                            if is_technical_qualified(table_name):
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_call["id"],
                                    "content": "Связи для этой таблицы недоступны.",
                                })
                                continue

                            # RBAC: таблица должна быть разрешена пользователю.
                            # Проверяем через validate_sql на простом SELECT —
                            # отдельного метода для метаданных связей нет.
                            # Если user_manager не подключён — проверка
                            # пропускается (как в остальных ветках).
                            if self.user_manager:
                                user_roles = (
                                    self.user_manager.get_user_roles(user_id) or []
                                )
                                rbac_ok, rbac_msg = self.rbac.validate_sql(
                                    f"SELECT 1 FROM {table_name}",
                                    user_roles,
                                    default_schema,
                                )
                                if not rbac_ok:
                                    messages.append({
                                        "role": "tool",
                                        "tool_call_id": tool_call["id"],
                                        "content": (
                                            f"⛔ Доступ запрещен: {rbac_msg}. "
                                            "Сообщи пользователю причину отказа и "
                                            "НЕ придумывай результат."
                                        ),
                                    })
                                    continue

                            try:
                                with open("config/relations.yaml", "r", encoding="utf-8") as f:
                                    relations_data = (yaml.safe_load(f) or {}).get("relations", {})
                            except (FileNotFoundError, yaml.YAMLError):
                                relations_data = {}

                            table_relations = relations_data.get(table_name, [])

                            # Технический фильтр связей: колонки и таблицы-цели
                            # (ref_table из references) скрываем так же, как
                            # в _get_relations_text. RBAC для ref_table не
                            # применяем — это метаданные, а не доступ к данным.
                            filtered_relations = []
                            for rel in table_relations:
                                col = str(rel.get("column", ""))
                                ref = str(rel.get("references", ""))

                                if is_technical_qualified(col):
                                    continue
                                if "." in ref:
                                    ref_table = ".".join(
                                        ref.split(".")[:-1]
                                    ).lower()
                                    if is_technical_qualified(ref_table):
                                        continue
                                filtered_relations.append(rel)

                            if not filtered_relations:
                                result_text = f"Связи для таблицы {table_name} не описаны."
                            else:
                                lines = [f"Связи для {table_name}:"]
                                for rel in filtered_relations:
                                    lines.append(
                                        f"- {rel['column']} -> {rel['references']} "
                                        f"({rel.get('description', '')})"
                                    )
                                result_text = "\n".join(lines)
                            messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": result_text})
                            continue

                        # execute_sql
                        try:
                            args = json.loads(tool_call["function"]["arguments"])
                            sql = args.get("sql", "")
                        except (json.JSONDecodeError, TypeError):
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call["id"],
                                "content": "Ошибка: некорректные аргументы инструмента",
                            })
                            continue

                        is_valid, reason = validate_sql(sql)
                        if not is_valid:
                            log_violation(user_id or "unknown", sql, reason)
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call["id"],
                                "content": f"Ошибка: {reason}",
                            })
                            continue

                        # RBAC + технический фильтр (админы тоже проверяются:
                        # технические таблицы/колонки запрещены всем ролям).
                        if self.user_manager:
                            user_roles = self.user_manager.get_user_roles(user_id) or []
                            logger.info(
                                "tool_call execute_sql: user_roles=%s, default_schema=%r, sql=%r",
                                user_roles, default_schema, sql,
                            )
                            rbac_ok, rbac_msg = self.rbac.validate_sql(sql, user_roles, default_schema)
                            technical_denied = (not rbac_ok) and "technical" in (rbac_msg or "").lower()

                            # Мягкий выход из отказа по SELECT *: разворачиваем
                            # звёздочку в разрешённые поля и валидируем снова.
                            if (
                                not rbac_ok
                                and not technical_denied
                                and self.rbac.is_select_star_denial(rbac_msg)
                            ):
                                expanded_sql = await self._try_expand_select_star(
                                    sql, state, user_roles, default_schema,
                                    source="llm",
                                )
                                if expanded_sql:
                                    sql = expanded_sql
                                    rbac_ok, rbac_msg = True, sql

                            if not rbac_ok:
                                if "admin" in user_roles and not technical_denied:
                                    logger.info("tool_call: admin bypass for non-technical SQL")
                                    rbac_ok = True
                                    # sql оставляем как есть — это осознанный обход для админа
                                else:
                                    log_violation(user_id or "unknown", sql, f"RBAC: {rbac_msg}")
                                    messages.append({
                                        "role": "tool",
                                        "tool_call_id": tool_call["id"],
                                        "content": (
                                            f"⛔ Доступ запрещен: {rbac_msg}. "
                                            "Сообщи пользователю причину отказа и НЕ придумывай результат."
                                        ),
                                    })
                                    continue
                            else:
                                # rbac_ok=True. Если это была НЕ админ-ветка и validate_sql
                                # вернул нормализованный sql — подставляем его.
                                if rbac_msg and rbac_msg != sql:
                                    sql = rbac_msg

                        if state.current_db is None:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call["id"],
                                "content": "Ошибка: БД не выбрана",
                            })
                            continue

                        # Проверка сложности SQL — ПОСЛЕ RBAC и ДО выполнения.
                        # Отказ уходит модели как tool-сообщение (как и ошибки
                        # validate_sql): она может переформулировать запрос,
                        # пользователю ошибка не отправляется.
                        try:
                            check_complexity(
                                sql,
                                dialect=db_type,
                                max_joins=self.sql_max_joins,
                                max_depth=self.sql_max_depth,
                                max_unions=self.sql_max_unions,
                            )
                        except SQLTooComplex as e:
                            logger.warning(
                                "SQL rejected by complexity check",
                                extra={
                                    "user_id": user_id,
                                    "reason": e.reason,
                                    "sql": sql[:500],
                                    "dialect": db_type,
                                },
                            )
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call["id"],
                                "content": (
                                    f"Ошибка: {e.reason} "
                                    "Переформулируй запрос проще или попроси "
                                    "пользователя уточнить вопрос."
                                ),
                            })
                            continue

                        # Защита от переполнения контекста: если нет LIMIT — добавляем.
                        # Для агрегатных запросов (GROUP BY, COUNT/SUM/MIN/MAX/AVG)
                        # LIMIT бессмысленен — не добавляем и не засоряем лог.
                        has_aggregate = (
                            "group by" in sql.lower()
                            or re.search(
                                r'\b(count|sum|min|max|avg)\s*\(',
                                sql,
                                re.IGNORECASE,
                            )
                        )
                        if "limit" not in sql.lower() and not has_aggregate:
                            if "text_transcrib" in sql.lower() or "dialog_from_whisper" in sql.lower():
                                sql = sql.rstrip() + " LIMIT 10"
                            else:
                                sql = sql.rstrip() + f" LIMIT {self.max_rows_in_response}"

                        # auto-LIMIT добавлен выше; теперь отделяем SQL для
                        # выполнения (без комментариев) от аудита (оригинал).
                        sql_for_exec = strip_sql_comments(sql)
                        audit_sql = sql

                        start_time = time.perf_counter()
                        try:
                            rows = await self.db_manager.execute_query(state.current_db, sql_for_exec)
                            duration = time.perf_counter() - start_time
                            if rows is None:
                                rows = []
                            extracted = self._extract_table_from_sql(audit_sql)
                            schema_part, table_part = (
                                extracted if extracted else (None, None)
                            )
                            result_text = await self._format_sql_result(
                                rows, db_name=state.current_db,
                                schema=schema_part, table_name=table_part,
                            )
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call["id"],
                                "content": result_text,
                            })
                            self._log_sql_audit(audit_sql, duration, "success", source="llm")
                            logger.info("Executed SQL: %s", audit_sql)
                            # Запоминаем реальный SQL отдельно (для будущих уточнений)
                            state.last_sqls.append(audit_sql)
                            if len(state.last_sqls) > 20:
                                state.last_sqls = state.last_sqls[-20:]
                        except Exception as e:
                            duration = time.perf_counter() - start_time
                            err_str = str(e)
                            self._log_sql_audit(audit_sql, duration, "error", err_str, source="llm")
                            if "timeout" in err_str.lower() or "MAX_EXECUTION_TIME" in err_str:
                                timeout_val = os.environ.get('SQL_QUERY_TIMEOUT', '15')
                                error_msg = f"Запрос выполнялся слишком долго (лимит {timeout_val} сек) и был прерван."
                            else:
                                error_msg = f"Ошибка выполнения запроса: {err_str}"
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call["id"],
                                "content": error_msg,
                            })

                # После цикла
                if not final_text and step == max_steps:
                    logger.warning(
                        "Agent loop reached max_steps",
                        extra={
                            "steps": step,
                            "max_steps": max_steps,
                            "user_id": user_id,
                        },
                    )
                if not final_text:
                    messages.append({
                        "role": "system",
                        "content": "Ты уже собрал достаточно данных. НАПИШИ ФИНАЛЬНЫЙ ОТВЕТ ПОЛЬЗОВАТЕЛЮ. Больше не вызывай инструменты.",
                    })
                    final_response = await self.llm_client.chat(messages, tools=None, model_name=model_to_use)
                    final_text = final_response["choices"][0]["message"].get("content") or ""

                # Защита от пустого ответа
                if not final_text or not final_text.strip():
                    final_text = "⚠️ Я собрал данные, но не смог сформулировать вывод. Попробуйте уточнить вопрос."

                # Ужимаем длинные оправдания
                lower_text = final_text.lower()
                if "доступ запрещен" in lower_text or "извините" in lower_text or "к сожалению" in lower_text:
                    if len(final_text) > 150:
                        final_text = "⛔ Нет доступа к данным."

                # Обрезка по max_message_length (а не жёсткие 2000)
                if len(final_text) > self.max_message_length:
                    final_text = final_text[:self.max_message_length - 3] + "..."

                # Убираем псевдо-вызовы
                final_text = re.sub(r"execute_sql\s*\([^)]*\)", "", final_text).strip()

                await self._save_history(state, user_id, msg.text, final_text)

                audit_answer = final_text
                self._log_audit(state, msg, audit_question, audit_answer)

                if self.show_model_in_response:
                    final_text += f"\n\n_Ответ от модели: {state.last_model_used}_"

                return ChatResponse(text=final_text)

            except Exception as e:
                logger.exception("Ошибка LLM")
                audit_answer = f"Ошибка LLM: {e}"
                self._log_audit(state, msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)
        finally:
            lock.release()

    # ---------- Долгосрочная память: рендер и операции ----------
    async def _load_memory(self, user_id: str, state: SessionState) -> list[str]:
        """Подгружает факты из Redis, если в состоянии их ещё нет.

        Нужно для callback-хэндлеров адаптера: они не проходят через
        process_message и могут работать в «свежем» процессе.
        """
        if state.long_term_facts:
            return state.long_term_facts
        if self.cache_manager is None:
            return state.long_term_facts
        try:
            raw = await self.cache_manager.get(f"repdix:memory:{user_id}")
            if not raw:
                return state.long_term_facts
            loaded = json.loads(raw)
        except Exception as e:
            logger.warning("Не удалось загрузить память %s: %s", user_id, e)
            return state.long_term_facts
        if isinstance(loaded, list):
            state.long_term_facts = [str(f) for f in loaded]
        return state.long_term_facts

    @staticmethod
    def _fact_hash(fact: str) -> str:
        """Короткий хэш факта для callback_data: первые 8 символов md5.

        8 hex-символов влезают в лимит Telegram callback_data (64 байта)
        и с запасом покрывают список фактов одного пользователя.
        """
        return hashlib.md5(str(fact).encode("utf-8")).hexdigest()[:8]

    # Лимит подписи кнопки: Telegram допускает 1-64 символа на кнопку,
    # оставляем запас под префикс «❌ N. ».
    _MEMORY_BUTTON_LABEL_MAX = 30

    @staticmethod
    def _memory_button_label(fact: str) -> str:
        """Готовит подпись факта для Inline-кнопки (одна строка, <=30 симв.)."""
        label = re.sub(r"\s+", " ", str(fact or "")).strip()
        limit = RepdixEngine._MEMORY_BUTTON_LABEL_MAX
        if len(label) > limit:
            label = label[:limit - 3].rstrip() + "..."
        return label or "—"

    @staticmethod
    def _build_memory_markup(facts: list[str]) -> dict:
        """Собирает Inline-клавиатуру для /memory (dict формата Telegram).

        Кнопка удаления показывает текст факта («❌ 1. <текст>»), а
        callback_data привязан к содержимому (mem_edit_<hash> /
        mem_del_<hash>): индексы «плывут» после удаления, и кнопка могла
        бы изменить/удалить не тот факт.
        """
        rows: list[list[dict]] = []
        for idx, fact in enumerate(facts):
            fact_hash = RepdixEngine._fact_hash(fact)
            rows.append([
                {
                    "text": (
                        f"❌ {idx + 1}. "
                        f"{RepdixEngine._memory_button_label(fact)}"
                    ),
                    "callback_data": f"mem_del_{fact_hash}",
                },
                {
                    "text": f"✏️ {idx + 1}",
                    "callback_data": f"mem_edit_{fact_hash}",
                },
            ])
        rows.append([
            {"text": "➕ Добавить", "callback_data": "mem_add"},
            {"text": "🗑 Очистить всё", "callback_data": "mem_clear"},
        ])
        return {"inline_keyboard": rows}

    async def render_memory(self, user_id: str,
                             state: SessionState | None = None) -> ChatResponse:
        """Формирует ответ /memory: текст + кнопки управления фактами.

        Текст экранируется: факты вводит пользователь, а адаптер отправляет
        сообщение с parse_mode="HTML".
        """
        state = state or self.sessions.get(user_id)
        facts = await self._load_memory(user_id, state)

        if not facts:
            return ChatResponse(
                text=(
                    "🧠 Долгосрочная память пуста. Используйте "
                    "/remember <текст> или кнопку «Добавить»."
                ),
                reply_markup={
                    "inline_keyboard": [[
                        {"text": "➕ Добавить", "callback_data": "mem_add"},
                    ]],
                },
            )

        lines = ["🧠 Твоя память:"]
        for i, fact in enumerate(facts, 1):
            lines.append(f"{i}. {html.escape(str(fact))}")
        lines.append(
            "Удалить или изменить факт можно кнопками ниже "
            "(/forget <номер> тоже работает)."
        )
        return ChatResponse(
            text="\n".join(lines),
            reply_markup=self._build_memory_markup(facts),
        )

    async def memory_get(self, user_id: str, idx: int) -> str | None:
        """Возвращает факт по индексу (0-based) или None."""
        state = self.sessions.get(user_id)
        facts = await self._load_memory(user_id, state)
        if 0 <= idx < len(facts):
            return facts[idx]
        return None

    async def memory_find_by_hash(self, user_id: str,
                                  fact_hash: str) -> tuple[int, str] | None:
        """Ищет факт по хэшу (первые 8 символов md5 текста).

        Returns:
            (индекс, текст факта) или None, если факт уже удалён/изменён.
        """
        state = self.sessions.get(user_id)
        facts = await self._load_memory(user_id, state)
        target = (fact_hash or "").strip().lower()
        if not target:
            return None
        for idx, fact in enumerate(facts):
            if self._fact_hash(fact).lower() == target:
                return idx, fact
        return None

    async def memory_add(self, user_id: str, fact: str) -> bool:
        """Добавляет факт в долгосрочную память."""
        fact = (fact or "").strip()
        if not fact:
            return False
        state = self.sessions.get(user_id)
        facts = await self._load_memory(user_id, state)
        facts.append(fact)
        await self._save_memory(user_id, facts)
        return True

    async def memory_update(self, user_id: str, idx: int, fact: str) -> bool:
        """Заменяет факт по индексу (0-based)."""
        fact = (fact or "").strip()
        if not fact:
            return False
        state = self.sessions.get(user_id)
        facts = await self._load_memory(user_id, state)
        if not (0 <= idx < len(facts)):
            return False
        facts[idx] = fact
        await self._save_memory(user_id, facts)
        return True

    async def memory_delete(self, user_id: str, idx: int) -> bool:
        """Удаляет факт по индексу (0-based)."""
        state = self.sessions.get(user_id)
        facts = await self._load_memory(user_id, state)
        if not (0 <= idx < len(facts)):
            return False
        facts.pop(idx)
        await self._save_memory(user_id, facts)
        return True

    async def memory_delete_by_hash(self, user_id: str, fact_hash: str) -> bool:
        """Удаляет факт по хэшу. False — факт уже удалён/изменён."""
        found = await self.memory_find_by_hash(user_id, fact_hash)
        if found is None:
            return False
        idx, _fact = found
        return await self.memory_delete(user_id, idx)

    async def memory_update_by_hash(self, user_id: str, fact_hash: str,
                                     fact: str) -> bool:
        """Заменяет факт по хэшу. False — факт уже удалён/изменён."""
        found = await self.memory_find_by_hash(user_id, fact_hash)
        if found is None:
            return False
        idx, _old = found
        return await self.memory_update(user_id, idx, fact)

    async def memory_clear(self, user_id: str) -> None:
        """Полностью очищает долгосрочную память пользователя."""
        state = self.sessions.get(user_id)
        state.long_term_facts = []
        await self._save_memory(user_id, [])

    # ---------- Долгосрочная память ----------
    async def _save_memory(self, user_id: str, facts: list[str]) -> None:
        """Сохраняет долгосрочную память пользователя в Redis (TTL 30 дней).

        При отсутствии/сбое Redis просто логируем — факты остаются в
        state.long_term_facts на время жизни процесса (graceful degradation).
        """
        if self.cache_manager is None:
            return
        try:
            await self.cache_manager.set(
                f"repdix:memory:{user_id}",
                json.dumps(facts, ensure_ascii=False),
                ttl=30 * 86400,
            )
        except Exception as e:
            logger.warning("Не удалось сохранить память %s: %s", user_id, e)

    # ---------- Персистенция сессии (session:{user_id}) ----------
    async def _persist_session(self, state: SessionState, user_id: str) -> None:
        """Зеркалит current_db/current_model/summary/last_sqls в Redis.

        Ключ session:{user_id} (TTL 24 часа) дополняет repdix:history и
        repdix:memory: история и память живут отдельно, а выбранная БД,
        модель и последние SQL иначе теряются при рестарте процесса.
        При отсутствии/сбое Redis просто логируем (graceful degradation).
        """
        if self.cache_manager is None:
            return
        payload = {
            "current_db": state.current_db,
            "current_model": state.current_model,
            "summary": state.summary,
            "last_sqls": [str(s) for s in state.last_sqls],
        }
        try:
            await self.cache_manager.set(
                f"session:{user_id}",
                json.dumps(payload, ensure_ascii=False),
                ttl=86400,
            )
        except Exception as e:
            logger.warning("Не удалось сохранить сессию %s: %s", user_id, e)

    # ---------- Сохранение истории ----------
    async def _save_history(self, state: SessionState, user_id: str,
                             user_text: str, assistant_text: str) -> None:
        """Сохраняет пару user/assistant в историю и Redis.

        ВАЖНО: обрезка идёт по парам, чтобы не терять контекст. System-сообщения
        [executed_sql] остаются в истории, но при обрезке удаляются первыми.
        """
        state.history.append({"role": "user", "content": user_text})
        state.history.append({"role": "assistant", "content": assistant_text})

        # Обрезаем: оставляем последние max_history сообщений, но system-сообщения
        # [executed_sql] удаляем в первую очередь (они не несут диалоговой ценности).
        if len(state.history) > self.max_history:
            trimmed = state.history[-self.max_history:]
            # Удаляем ведущие system-сообщения (executed_sql) из начала
            while trimmed and trimmed[0].get("role") == "system":
                trimmed.pop(0)
            state.history = trimmed

        if self.cache_manager is not None:
            await self.cache_manager.set(
                f"repdix:history:{user_id}",
                json.dumps(
                    {"history": state.history, "summary": state.summary},
                    ensure_ascii=False,
                ),
                ttl=86400,
            )

    # ---------- SELECT *: авторазвёртывание ----------
    async def _expand_select_star_sql(
        self,
        sql: str,
        state: SessionState,
        user_roles: list[str],
        default_schema: str,
    ) -> str | None:
        """Пробует развернуть SELECT * в явный список разрешённых полей.

        Колонки подгружаются заранее (async), затем синхронный
        rbac.expand_select_star получает их через коллбэк-словарь.

        Returns:
            Новый SQL (ещё не проверенный RBAC) или None, если развернуть
            не удалось.
        """
        current_db = getattr(state, "current_db", None)
        if self.db_manager is None or current_db is None:
            return None

        try:
            tables = self.rbac.extract_tables(sql, default_schema)
        except Exception as e:
            logger.debug("expand_select_star: не удалось разобрать SQL: %s", e)
            return None
        if not tables:
            return None

        columns_cache: dict[str, list[str]] = {}
        for qualified in tables:
            schema_part, table_part = self._split_qualified(
                qualified, default_schema,
            )
            try:
                columns = await self.db_manager.get_table_schema(
                    current_db, table_part, schema_part or None,
                )
            except Exception as e:
                logger.debug(
                    "expand_select_star: не удалось получить поля %s: %s",
                    qualified, e,
                )
                continue
            names = [
                str(c.get("column_name", ""))
                for c in (columns or [])
                if c.get("column_name")
            ]
            if names:
                columns_cache[qualified.lower()] = names
                columns_cache.setdefault(table_part.lower(), names)

        def _getter(name: str) -> list[str] | None:
            key = str(name or "").lower()
            if not key:
                return None
            return columns_cache.get(key) or columns_cache.get(
                key.split(".")[-1]
            )

        try:
            expanded = self.rbac.expand_select_star(
                sql, user_roles, default_schema, all_columns_getter=_getter,
            )
        except Exception as e:
            logger.warning("expand_select_star: ошибка развёртывания: %s", e)
            return None

        if not expanded or expanded.strip() == (sql or "").strip():
            return None
        return expanded

    async def _try_expand_select_star(
        self,
        sql: str,
        state: SessionState,
        user_roles: list[str],
        default_schema: str,
        source: str,
    ) -> str | None:
        """Разворачивает SELECT * и повторно валидирует результат.

        Args:
            source: "llm" (tool_call) или "manual" (/sql) — для audit.log.

        Returns:
            Валидный SQL или None, если «починить» запрос не удалось.
        """
        # state может быть заглушкой без user_id (например, в тестах) —
        # доступ к атрибуту через getattr, чтобы не падать с AttributeError.
        user_id_for_log = getattr(state, "user_id", None) or "unknown"

        expanded = await self._expand_select_star_sql(
            sql, state, user_roles, default_schema,
        )
        if not expanded:
            # Это не нарушение безопасности, а неудачная попытка «починить»
            # SELECT *. Пишем в audit.log, не в security.log, чтобы не
            # засорять security-лог ложными срабатываниями.
            audit_logger.info(json.dumps({
                "timestamp": datetime.now().isoformat(),
                "type": "expand_select_star_failed",
                "user_id": user_id_for_log,
                "source": source,
                "sql": (sql or "")[:1000],
            }, ensure_ascii=False))
            return None

        ok, message = self.rbac.validate_sql(
            expanded, user_roles, default_schema,
        )
        if not ok:
            logger.info(
                "expand_select_star: развёрнутый запрос не прошёл RBAC: %s",
                message,
            )
            log_violation(
                user_id_for_log,
                expanded,
                f"expand_select_star rejected: {message}",
            )
            return None

        self._log_star_expansion(sql, expanded, source=source)
        return expanded

    # ---------- Аудит ----------
    def _log_audit(self, state: SessionState, msg: ChatMessage,
                    question: str, answer: str) -> None:
        data = {
            "timestamp": datetime.now().isoformat(),
            "user_id": msg.user_id,
            "chat_id": msg.chat_id,
            "question": question,
            "answer": answer,
            "db": state.current_db,
            "model": state.current_model or "default",
            "model_used": state.last_model_used,
        }
        audit_logger.info(json.dumps(data, ensure_ascii=False))

    def _log_sql_audit(self, sql: str, duration: float, status: str,
                        error: str | None = None, source: str = "llm") -> None:
        """Пишет SQL-аудит. source: 'llm' (из tool_call) или 'manual' (/sql)."""
        data = {
            "timestamp": datetime.now().isoformat(),
            "type": f"sql_{source}",
            "sql": sql,
            "duration_sec": round(duration, 3),
            "status": status,
        }
        if error:
            data["error"] = error
        audit_logger.info(json.dumps(data, ensure_ascii=False))

    def _log_star_expansion(self, original_sql: str, expanded_sql: str,
                            source: str = "llm") -> None:
        """Пишет в audit.log факт авторазвёртывания SELECT *."""
        audit_logger.info(json.dumps({
            "timestamp": datetime.now().isoformat(),
            "type": "select_star_expanded",
            "source": source,
            "original_sql": original_sql,
            "expanded_sql": expanded_sql,
        }, ensure_ascii=False))
