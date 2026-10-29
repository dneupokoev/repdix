# app/core/engine.py
dv_file_version = '261029.14'
"""Ядро repdix: диспетчер сообщений, не зависящий от мессенджера.

Ключевые изменения версии 261029.14:
- В промпт добавлен отдельный блок "ПРАВИЛА SQL": имена таблиц, колонок и
  алиасов в MySQL обязательно оборачиваются в обратные кавычки (`) — защита
  от зарезервированных слов (order, group, interval, key) и спецсимволов.

Ключевые изменения версии 261029.13:
- Правило об источниках цифр переформулировано: имена таблиц и полей
  упоминаются ТОЛЬКО в сложных аналитических ответах ("почему", "как дела",
  "проанализируй", "когда получим"). Для простых запросов (топ-N, список,
  сколько записей) ответ содержит только данные, без SQL-схемы.
- В "ПРАВИЛА ВЫВОДА" добавлен прямой запрет упоминать имена таблиц/полей
  в обычных ответах (исключение — пользователь сам спросил про источник).

Ключевые изменения версии 261029.12:
- В "ПРАВИЛА ВЫВОДА" добавлено требование форматировать ответ в HTML
  (<b> для ключевых цифр, <code> для имён таблиц/полей) и прямой запрет
  Markdown (**жирный**, *курсив*, `код`): Telegram не рендерит его без
  parse_mode, а с parse_mode ломается на подчёркиваниях в именах таблиц.
- Пример выделения ключевых цифр переведён с Markdown на <b>, чтобы не
  противоречить новому правилу.

Ключевые изменения версии 261029.11:
- Удалена вторая модель (deep_analysis_model / analysis_model): остаётся
  только tool_calling_model. _route_query всегда возвращает выбранную
  пользователем модель или tool_model. Удалены блок "analysis_model: без tools"
  в process_message, константа ANALYSIS_MARKERS и упоминание в /settings.

Ключевые изменения версии 261029.10:
- Расширены "ПРАВИЛА АНАЛИЗА" (сравнение с предыдущим периодом, поиск
  узкого места, указание источника цифр, предупреждение об устаревших
  данных) и "ПРАВИЛА ВЫВОДА" (блок "Что делать", выделение ключевых
  цифр, структурные разделы, запрет воды). Структура промпта не менялась.

Ключевые изменения версии 261029.09:
- Автоматический LIMIT больше не добавляется, если SQL содержит GROUP BY
  или агрегатную функцию (COUNT/SUM/MIN/MAX/AVG): лимит для агрегатов
  бессмыслен и только засорял лог.

Ключевые изменения версии 261029.06:
- Multi-schema: _get_db_schema() фильтрует схемы через
  rbac.get_allowed_schemas(), таблицы — через rbac.get_allowed_tables();
  таблицы выводятся fully-qualified (schema.table).
- _describe_table() принимает schema и table раздельно.
- _extract_table_from_sql() возвращает (schema, table).
- _format_sql_result() принимает schema= (для корректного get_table_schema).
- _extract_last_table_from_history() возвращает "schema.table", если схема
  была указана в SQL.
- /db, /db <name>, /settings показывают описания БД (get_db_display_name),
  доступные схемы (с учётом RBAC) и схему по умолчанию.
- _build_base_prompt(db_type, schemas, default_schema) — в промпт добавлен
  список доступных схем и требование указывать схему перед таблицей.

Ключевые изменения версии 261027.02:
- Убран get_table_schema из инструментов и промпта (см. README: "Не возвращайте!").
- Единый список TOOLS: execute_sql + get_relations + execute_template.
- base_prompt переписан: убраны дубли, противоречия, упоминания несуществующих
  инструментов, добавлена параметризация по типу БД (MySQL/PostgreSQL/SQLite).
- RBAC теперь вызывается через validate_sql (алиас validate_and_fix_sql удалён
  из engine, но оставлен в rbac_guard для обратной совместимости).
- _run_yaml_template прогоняет собранный SQL через RBAC-гард (закрыта дыра:
  шаблон мог обратиться к недоступной таблице).
- _extract_last_table_from_history ищет таблицу в [executed_sql] или после FROM,
  а не первое попавшееся слово.
- _get_db_schema для админа показывает и поля (как для обычных пользователей),
  а не только список таблиц.
- _route_query: длинный вопрос (len>200) больше НЕ отправляется в analysis_model
  без tools. analysis_model выбирается только по явным аналитическим маркерам.
- Жёсткая обрезка ответа (2000) заменена на self.max_message_length.
- Удалён мёртвый код _should_force_tool, _get_default_schema (дублировал логику).
- _format_sql_result больше не выводит Markdown-таблицы (противоречило промпту).
- _log_sql_audit различает ручной (/sql) и LLM-инициированный SQL.
- _save_history больше не засоряет историю system-сообщениями [executed_sql]
  (они копятся отдельно в state.last_sqls и НЕ обрезаются вместе с историей).
- Мелочи: единый time_markers, нормализация default_schema, try/except вокруг
  get_table_list, context overflow — обрезка по парам user/assistant.
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime

import yaml

from app.core.session import SessionState, SessionStore
from app.core.technical_filter import is_technical, is_technical_qualified
from app.core.validator import log_violation, validate_sql
from app.core.rbac_guard import RBACGuard
from app.interfaces.base import ChatMessage, ChatResponse

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("audit")


# ----------------------------------------------------------------------
# Константы
# ----------------------------------------------------------------------
TIME_MARKERS = ["когда", "дата", "время", "последние", "последняя", "свежие", "newest", "latest"]

STRUCTURE_MARKERS = ["описание", "поля", "структура", "метаданные"]


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
    {
        "type": "function",
        "function": {
            "name": "execute_template",
            "description": (
                "Выполнить предопределенный шаблонный запрос. Используй для типовых "
                "вопросов (кто работал, топ-N, лучшие, рейтинги), чтобы сэкономить "
                "время и избежать ошибок."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "template_name": {
                        "type": "string",
                        "description": "Имя шаблона (например, who_worked или top_n_edits)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Количество строк для рейтинга (если применимо, например 8)",
                    },
                    "period": {
                        "type": "string",
                        "description": "Период (сегодня, вчера, этот месяц, или конкретная дата YYYY-MM-DD)",
                    },
                },
                "required": ["template_name"],
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
        "2. get_relations — узнать связи таблиц.\n"
        "3. execute_template — выполнить предопределённый шаблонный запрос "
        "(топ-N, кто работал, рейтинги).\n\n"
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
        "данных 5 недель нет'. Это важнее абсолютных цифр.\n\n"
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

    # ---------- Хелперы ----------
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
        """Извлекает (schema, table) из первого FROM-выражения.

        Returns:
            (schema, table) или None, если таблица не найдена. schema=None,
            если в SQL схема не указана.
        """
        match = re.search(r'\bFROM\s+([a-zA-Z0-9_\.]+)', sql, re.IGNORECASE)
        if not match:
            return None
        raw = match.group(1)
        if "." in raw:
            schema, table = raw.split(".", 1)
            return (schema or None), table
        return None, raw

    @staticmethod
    def _split_qualified(name: str, default_schema: str = "") -> tuple[str, str]:
        """Делит "schema.table" на (schema, table); короткое имя — default_schema."""
        raw = str(name or "")
        if "." in raw:
            schema, table = raw.split(".", 1)
            return schema.lower(), table
        return (default_schema or "").lower(), raw

    def _shorten_text(self, text: str, max_len: int = 120) -> str:
        """Обрезает текст до max_len символов, удаляя вступления."""
        if not text:
            return ""
        text = re.sub(r'\s+', ' ', text).strip()
        for prefix in ["извините, ", "к сожалению, ", "прошу прощения, ", "я не могу, "]:
            if text.lower().startswith(prefix):
                text = text[len(prefix):]
        if len(text) > max_len:
            text = text[:max_len].rsplit(' ', 1)[0] + "..."
        return text

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
    async def _try_get_last_date(self, state: SessionState, table_name: str) -> str | None:
        if state.current_db is None or self.db_manager is None:
            return None
        try:
            columns = await self.db_manager.get_table_schema(state.current_db, table_name)
        except Exception:
            return None
        column_names = {c.get("column_name", "").lower() for c in columns}
        date_columns = ["created_at", "updated_at", "date", "timestamp", "inserted_at"]
        for col in date_columns:
            if col not in column_names:
                continue
            # Валидация имени колонки (оно берётся из БД, но перестрахуемся)
            if not re.fullmatch(r"[a-zA-Z0-9_]+", col):
                continue
            sql = f"SELECT MAX({col}) AS last_date FROM {table_name}"
            try:
                rows = await self.db_manager.execute_query(state.current_db, sql)
                if rows and rows[0].get("last_date") is not None:
                    return str(rows[0]["last_date"])
            except Exception:
                continue
        return None

    # ---------- Выполнение YAML-шаблона ----------
    async def _run_yaml_template(self, tpl_name: str, limit: int | None,
                                  period: str | None, state: SessionState) -> str:
        """Выполняет шаблон из config/templates.yaml.

        ВАЖНО: собранный SQL прогоняется через RBAC-гард — иначе шаблон мог бы
        обратиться к таблице, недоступной пользователю (это была дыра).
        """
        if state.current_db is None or self.db_manager is None:
            return "БД не выбрана."

        db_type = self._get_db_type(state)
        if db_type != "mysql":
            return f"Шаблоны доступны только для MySQL (текущая БД: {db_type})."

        try:
            with open("config/templates.yaml", "r", encoding="utf-8") as f:
                templates = yaml.safe_load(f) or []
        except Exception as e:
            return f"Ошибка загрузки шаблонов: {e}"

        tpl = next((t for t in templates if t.get("name") == tpl_name), None)
        if not tpl:
            return f"Шаблон '{tpl_name}' не найден."

        # Технические таблицы недоступны даже через шаблоны (иначе admin
        # через execute_template увидел бы скрытые данные в обход RBAC).
        tpl_table_raw = str(tpl.get("table") or "")
        if is_technical(tpl_table_raw):
            logger.warning(
                "Шаблон '%s' ссылается на техническую таблицу '%s' — отказ.",
                tpl_name, tpl_table_raw,
            )
            return "Шаблон ссылается на техническую таблицу и недоступен."

        # Валидация имён полей/таблиц из шаблона (защита от инъекции через конфиг)
        def _safe_ident(x: str | None, default: str) -> str:
            if not x or not re.fullmatch(r"[a-zA-Z0-9_\.]+", x):
                return default
            return x

        group_by_field = _safe_ident(tpl.get("group_by_field"), "reviewer")
        table = _safe_ident(tpl.get("table"), "c2t_asr_training_review")
        time_field = _safe_ident(tpl.get("time_field"), "created_at")
        action_field = tpl.get("action_field")
        if action_field is not None:
            action_field = _safe_ident(action_field, "")
        action_value = tpl.get("action_value")

        final_limit = limit or tpl.get("limit")
        try:
            final_limit = int(final_limit) if final_limit else None
        except (TypeError, ValueError):
            final_limit = None

        where_clause = "1=1"
        if period:
            period_lower = period.lower()
            if period_lower == "сегодня":
                where_clause = f"{time_field} >= CURDATE()"
            elif period_lower == "вчера":
                where_clause = f"{time_field} >= DATE_SUB(CURDATE(), INTERVAL 1 DAY) AND {time_field} < CURDATE()"
            elif period_lower == "этот месяц":
                where_clause = f"{time_field} >= DATE_FORMAT(CURDATE(), '%Y-%m-01')"
            elif period_lower == "этот год":
                where_clause = f"{time_field} >= DATE_FORMAT(CURDATE(), '%Y-01-01')"
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", period):
                where_clause = f"{time_field} >= '{period} 00:00:00'"
            else:
                year_match = re.search(r'\d{4}', period)
                if year_match:
                    year = year_match.group(0)
                    where_clause = f"{time_field} >= '{year}-01-01'"

        if action_field and action_value is not None:
            # action_value — строка, экранируем кавычки
            safe_value = str(action_value).replace("'", "''")
            where_clause += f" AND {action_field} = '{safe_value}'"

        sql = (
            f"SELECT {group_by_field}, COUNT(*) AS cnt FROM {table} "
            f"WHERE {where_clause} GROUP BY {group_by_field} ORDER BY cnt DESC"
        )
        if final_limit:
            sql += f" LIMIT {final_limit}"

        # --- RBAC-проверка (закрываем дыру) ---
        # Проверка выполняется ВСЕГДА (как в /sql и execute_sql): админ
        # обходит только обычные RBAC-правила, но не технический фильтр.
        if self.user_manager:
            user_roles = self.user_manager.get_user_roles(state.user_id) or []
            default_schema = self._get_default_schema(state)
            rbac_ok, rbac_msg = self.rbac.validate_sql(sql, user_roles, default_schema)
            technical_denied = (not rbac_ok) and "technical" in (rbac_msg or "").lower()
            if not rbac_ok and ("admin" in user_roles) and not technical_denied:
                logger.info("template: admin bypass for non-technical SQL")
            elif not rbac_ok:
                log_violation(state.user_id, sql, f"RBAC(template): {rbac_msg}")
                return f"⛔ Доступ запрещён: {rbac_msg}"

        try:
            rows = await self.db_manager.execute_query(state.current_db, sql)
            if not rows:
                return "За указанный период нет данных."

            lines = ["Результаты:"]
            for i, row in enumerate(rows, 1):
                name = row.get(group_by_field) or "—"
                count = row.get("cnt", 0)
                lines.append(f"{i}. {name}: {count}")
            return "\n".join(lines)
        except Exception as e:
            return f"Ошибка выполнения SQL: {e}"

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
    async def _get_db_schema(self, state: SessionState) -> str:
        """Формирует схему БД ТОЛЬКО из разрешённых схем, таблиц и полей.

        Multi-schema: таблицы выводятся как fully-qualified (schema.table).
        Схемы фильтруются через rbac.get_allowed_schemas(), таблицы — через
        rbac.get_allowed_tables(). Для admin разрешены все схемы из
        db_manager.get_schemas().
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

        # 4) Текст для промпта
        schemas_str = ", ".join(sorted(allowed_schemas)) or "—"
        lines = [
            f"Доступные схемы: {schemas_str}. "
            f"Схема по умолчанию: {default_schema or '—'}."
        ]

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

        return "\n".join(lines)

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

    # ---------- Главный обработчик ----------
    async def process_message(self, msg: ChatMessage) -> ChatResponse:
        user_id = msg.user_id
        state = self.sessions.get(user_id)
        lock = state.lock

        default_schema = self._get_default_schema(state)

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
                        state.history = json.loads(cached)
                    except json.JSONDecodeError:
                        state.history = []
                else:
                    state.history = []

            # ---------- /clear ----------
            if text == "/clear":
                state.last_model_used = "algorithm (command)"
                state.history = []
                if self.cache_manager is not None:
                    await self.cache_manager.delete(f"repdix:history:{user_id}")
                audit_answer = "История диалога очищена."
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
                    response = await self.llm_client.chat(messages, model_name=state.current_model)
                    summary = response["choices"][0]["message"]["content"]
                    state.history = [{"role": "system", "content": f"Резюме предыдущего диалога: {summary}"}]
                    if self.cache_manager is not None:
                        await self.cache_manager.set(
                            f"repdix:history:{user_id}",
                            json.dumps(state.history, ensure_ascii=False),
                            ttl=86400,
                        )
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
                    if not rbac_ok and ("admin" in user_roles) and not technical_denied:
                        logger.info("/sql: admin bypass for non-technical SQL")
                    elif not rbac_ok:
                        log_violation(user_id or "unknown", sql, f"RBAC: {rbac_msg}")
                        audit_answer = f"⛔ Доступ запрещен: {rbac_msg}"
                        if self.show_model_in_response:
                            audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                        self._log_audit(state, msg, audit_question, audit_answer)
                        return ChatResponse(text=audit_answer)
                    else:
                        sql = rbac_msg

                if state.current_db is None:
                    audit_answer = "Сначала выберите БД командой /db <name>"
                    if self.show_model_in_response:
                        audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                    self._log_audit(state, msg, audit_question, audit_answer)
                    return ChatResponse(text=audit_answer)

                start_time = time.perf_counter()
                try:
                    rows = await self.db_manager.execute_query(state.current_db, sql)
                    duration = time.perf_counter() - start_time
                    if rows is None:
                        rows = []
                    extracted = self._extract_table_from_sql(sql)
                    schema_part, table_part = extracted if extracted else (None, None)
                    audit_answer = await self._format_sql_result(
                        rows, db_name=state.current_db,
                        schema=schema_part, table_name=table_part,
                    )
                    self._log_sql_audit(sql, duration, "success", source="manual")
                    if self.show_model_in_response:
                        audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                    self._log_audit(state, msg, audit_question, audit_answer)
                    return ChatResponse(text=audit_answer)
                except Exception as e:
                    duration = time.perf_counter() - start_time
                    err_str = str(e)
                    self._log_sql_audit(sql, duration, "error", err_str, source="manual")
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
                schema = await self._get_db_schema(state)
                _now = datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")
                system_content = (
                    base_prompt
                    + f"\n\nСегодня: {_now} (локальное время сервера). "
                    + "Используй эту дату для 'сегодня', 'вчера', 'на этой неделе' и т.п."
                    + (("\n\n" + schema) if schema else "")
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
                max_steps = 5
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
                        clean_text = (choice.get("content") or "").strip()
                        if clean_text:
                            short_text = self._shorten_text(clean_text, max_len=80)
                            await msg.status_callback(f"🤖 {short_text}")
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

                        if tool_name not in {"execute_sql", "get_relations", "execute_template"}:
                            logger.warning("Unknown tool_call from LLM: %s", tool_name)
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call["id"],
                                "content": f"Ошибка: неизвестный инструмент '{tool_name}'",
                            })
                            continue

                        if tool_name == "execute_template":
                            try:
                                args = json.loads(tool_call["function"]["arguments"])
                            except (json.JSONDecodeError, TypeError):
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_call["id"],
                                    "content": "Ошибка: некорректные аргументы инструмента",
                                })
                                continue
                            tpl_name = args.get("template_name")
                            tpl_limit = args.get("limit")
                            tpl_period = args.get("period")

                            result_text = await self._run_yaml_template(tpl_name, tpl_limit, tpl_period, state)
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call["id"],
                                "content": result_text,
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
                            try:
                                with open("config/relations.yaml", "r", encoding="utf-8") as f:
                                    relations_data = (yaml.safe_load(f) or {}).get("relations", {})
                            except (FileNotFoundError, yaml.YAMLError):
                                relations_data = {}
                            table_relations = relations_data.get(table_name, [])
                            if not table_relations:
                                result_text = f"Связи для таблицы {table_name} не описаны."
                            else:
                                lines = [f"Связи для {table_name}:"]
                                for rel in table_relations:
                                    lines.append(f"- {rel['column']} -> {rel['references']} ({rel.get('description', '')})")
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
                            if not rbac_ok and ("admin" in user_roles) and not technical_denied:
                                logger.info("tool_call: admin bypass for non-technical SQL")
                            elif not rbac_ok:
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
                                sql = rbac_msg

                        if state.current_db is None:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call["id"],
                                "content": "Ошибка: БД не выбрана",
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

                        start_time = time.perf_counter()
                        try:
                            rows = await self.db_manager.execute_query(state.current_db, sql)
                            duration = time.perf_counter() - start_time
                            if rows is None:
                                rows = []
                            extracted = self._extract_table_from_sql(sql)
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
                            self._log_sql_audit(sql, duration, "success", source="llm")
                            logger.info("Executed SQL: %s", sql)
                            # Запоминаем реальный SQL отдельно (для будущих уточнений)
                            state.last_sqls.append(sql)
                            if len(state.last_sqls) > 20:
                                state.last_sqls = state.last_sqls[-20:]
                        except Exception as e:
                            duration = time.perf_counter() - start_time
                            err_str = str(e)
                            self._log_sql_audit(sql, duration, "error", err_str, source="llm")
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
                json.dumps(state.history, ensure_ascii=False),
                ttl=86400,
            )

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
