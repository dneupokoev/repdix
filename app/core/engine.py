# app/core/engine.py
dv_file_version = '261026.80'
"""Ядро repdix: диспетчер сообщений, не зависящий от мессенджера."""
import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta

import yaml

from app.core.session import SessionState, SessionStore
from app.core.validator import log_violation, validate_sql
from app.interfaces.base import ChatMessage, ChatResponse

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("audit")

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
    }
]

EXECUTE_TEMPLATE_TOOL = {
    "type": "function",
    "function": {
        "name": "execute_template",
        "description": "Выполнить предопределенный шаблонный запрос. Используй для типовых вопросов (кто работал, топ-N, лучшие, рейтинги), чтобы сэкономить время и избежать ошибок.",
        "parameters": {
            "type": "object",
            "properties": {
                "template_name": {"type": "string", "description": "Имя шаблона (например, who_worked или top_n_edits)"},
                "limit": {"type": "integer", "description": "Количество строк для рейтинга (если применимо, например 8)"},
                "period": {"type": "string", "description": "Период (сегодня, вчера, этот месяц, или конкретная дата YYYY-MM-DD)"}
            },
            "required": ["template_name"]
        }
    }
}

class RepdixEngine:
    """Главный диспетчер: принимает ChatMessage, возвращает ChatResponse."""

    def __init__(self, db_manager=None, llm_client=None, cache_manager=None, default_db: str | None = None) -> None:
        self.db_manager = db_manager
        self.llm_client = llm_client
        self.cache_manager = cache_manager
        self.default_db = default_db
        self.sessions = SessionStore(default_db=default_db)
        self.max_history = 10
        self.tool_model = "tool_calling_model"
        self.analysis_model = "deep_analysis_model"
        self.show_model_in_response = os.environ.get("SHOW_MODEL_IN_RESPONSE", "false").lower() == "true"
        self.max_rows_in_response = int(os.environ.get("MAX_ROWS_IN_RESPONSE") or "100")
        self.max_message_length = int(os.environ.get("MAX_MESSAGE_LENGTH") or "4000")

    # ---------- Форматирование результата SQL ----------
    def _format_sql_result(self, rows: list[dict]) -> str:
        if not rows:
            return "Результат: пусто"

        original_count = len(rows)
        display_rows = rows[:self.max_rows_in_response]
        if not display_rows:
            return "Результат: пусто"

        columns = list(display_rows[0].keys())
        num_cols = len(columns)
        lines: list[str] = []

        if num_cols < 5:
            header = "| " + " | ".join(columns) + " |"
            separator = "|" + "|".join(["---"] * num_cols) + "|"
            lines.append(header)
            lines.append(separator)
            for row in display_rows:
                vals = []
                for col in columns:
                    val = str(row.get(col, ""))
                    if len(val) > 50:
                        val = val[:47] + "..."
                    vals.append(val)
                lines.append("| " + " | ".join(vals) + " |")
        else:
            for i, row in enumerate(display_rows, 1):
                parts = []
                for col in columns:
                    val = str(row.get(col, ""))
                    if len(val) > 50:
                        val = val[:47] + "..."
                    parts.append(f"{col}: {val}")
                lines.append(f"{i}. " + ", ".join(parts))

        result_text = "\n".join(lines)

        if original_count > self.max_rows_in_response:
            result_text += f"\n... (показаны первые {self.max_rows_in_response} из {original_count})"

        if len(result_text) > self.max_message_length:
            result_text = result_text[:self.max_message_length - 30] + "\n... (сокращено)"

        return result_text

    # ---------- Роутинг моделей ----------
    def _route_query(self, text: str, state: SessionState) -> str:
        if state.current_model is not None:
            return state.current_model

        text_lower = text.lower()
        analysis_markers = [
            "почему", "проанализируй", "сравни", "тренд", "динамика",
            "причина", "объясни", "прогноз", "статистика", "аналитика"
        ]

        if any(marker in text_lower for marker in analysis_markers) or len(text) > 200:
            return self.analysis_model

        return self.tool_model

    # ---------- Извлечение имени таблицы из истории ----------
    def _extract_last_table_from_history(self, state: SessionState) -> str | None:
        for msg in reversed(state.history):
            content = str(msg.get("content", ""))
            match = re.search(r"\b([a-zA-Z0-9_]{3,})\b", content)
            if match:
                candidate = match.group(1)
                if candidate.lower() not in {"select", "from", "where", "table", "db", "model", "help", "sql"}:
                    return candidate
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
            sql = f"SELECT MAX({col}) AS last_date FROM {table_name}"
            try:
                rows = await self.db_manager.execute_query(state.current_db, sql)
                if rows and rows[0].get("last_date") is not None:
                    return str(rows[0]["last_date"])
            except Exception:
                continue
        return None

    async def _run_yaml_template(self, tpl_name: str, limit: int | None, period: str | None, state: SessionState) -> str:
        if state.current_db is None or self.db_manager is None:
            return "БД не выбрана."

        try:
            with open("config/templates.yaml", "r", encoding="utf-8") as f:
                templates = yaml.safe_load(f) or []
        except Exception as e:
            return f"Ошибка загрузки шаблонов: {e}"

        tpl = next((t for t in templates if t["name"] == tpl_name), None)
        if not tpl:
            return f"Шаблон '{tpl_name}' не найден."

        group_by_field = tpl.get("group_by_field", "reviewer")
        table = tpl.get("table", "c2t_asr_training_review")
        final_limit = limit or tpl.get("limit")

        where_clause = "1=1"
        if period:
            period_lower = period.lower()
            if period_lower == "сегодня":
                where_clause = f"{tpl.get('time_field', 'created_at')} >= CURDATE()"
            elif period_lower == "вчера":
                where_clause = f"{tpl.get('time_field', 'created_at')} >= DATE_SUB(CURDATE(), INTERVAL 1 DAY) AND {tpl.get('time_field', 'created_at')} < CURDATE()"
            elif period_lower == "этот месяц":
                where_clause = f"{tpl.get('time_field', 'created_at')} >= DATE_FORMAT(CURDATE(), '%Y-%m-01')"
            elif period_lower == "этот год":
                where_clause = f"{tpl.get('time_field', 'created_at')} >= DATE_FORMAT(CURDATE(), '%Y-01-01')"
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", period):
                # Если LLM передала конкретную дату
                where_clause = f"{tpl.get('time_field', 'created_at')} >= '{period} 00:00:00'"
            else:
                # Если LLM передала что-то другое, пробуем вытащить хотя бы год
                year_match = re.search(r'\d{4}', period)
                if year_match:
                    year = year_match.group(0)
                    where_clause = f"{tpl.get('time_field', 'created_at')} >= '{year}-01-01'"

        if tpl.get("action_field"):
            where_clause += f" AND {tpl['action_field']} = '{tpl['action_value']}'"

        sql = f"SELECT {group_by_field}, COUNT(*) AS cnt FROM {table} WHERE {where_clause} GROUP BY {group_by_field} ORDER BY cnt DESC"
        if final_limit:
            sql += f" LIMIT {final_limit}"

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
    def _get_relations_text(self) -> str:
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

        lines = ["Связи таблиц:"]
        for table_name, rels in relations.items():
            for rel in rels:
                col = rel.get("column", "")
                ref = rel.get("references", "")
                desc = rel.get("description", "")
                lines.append(f"- {table_name}.{col} -> {ref} ({desc})")

        return "\n".join(lines)

    # ---------- Схема БД (RAG) ----------
    async def _get_db_schema(self, state: SessionState) -> str:
        if state.current_db is None or self.db_manager is None:
            return ""
        try:
            table_names = await self.db_manager.get_table_list(state.current_db)
            if not table_names:
                return ""

            allowed_prefixes = ("c2t_queue", "c2t_employee", "c2t_asr_training_review", "c2t_queue_analysis")
            filtered_tables = [t for t in table_names if any(t.startswith(p) for p in allowed_prefixes)]
            limited_tables = filtered_tables[:15]
            remaining_count = len(filtered_tables) - len(limited_tables)

            lines = [f"Доступные таблицы в БД {state.current_db}:"]
            for t_name in limited_tables:
                lines.append(f"- {t_name}")

            if remaining_count > 0:
                lines.append(f"... и ещё {remaining_count} таблиц")

            relations_text = self._get_relations_text()
            if relations_text:
                lines.append("")
                lines.append(relations_text)

            return "\n".join(lines)
        except Exception:
            return ""

    # ---------- Главный обработчик ----------
    async def process_message(self, msg: ChatMessage) -> ChatResponse:
        user_id = msg.user_id
        state = self.sessions.get(user_id)
        lock = state.lock

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
                    f"Лимит контекста: {context_limit} токенов. Текущий размер: ~{total_tokens} токенов ({percent:.1f}%). "
                    f"Использование контекста: ~{total_tokens}/{context_limit} ({percent:.1f}%)."
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
                active_model_display = state.current_model or f"авто-выбор (роутер: {self.tool_model} / {self.analysis_model})"
                lines = [
                    f"Текущая БД: {state.current_db or 'не выбрана'}",
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
                    db_names = self.db_manager.get_db_names()
                    lines.append(f"Доступные БД: {', '.join(db_names) if db_names else 'нет'}")
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
                    audit_answer = f"БД '{db_name}' не найдена. Доступные: {', '.join(available) if available else 'нет'}"
                else:
                    state.current_db = db_name
                    audit_answer = f"БД: {state.current_db}"
                if self.show_model_in_response:
                    audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                self._log_audit(state, msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)

            if text == "/db":
                state.last_model_used = "algorithm (command)"
                audit_answer = f"Текущая БД: {state.current_db}"
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
                    audit_answer = "Текущая модель: не выбрана (используется по умолчанию)"
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
                    audit_answer = self._format_sql_result(rows)
                    self._log_sql_audit(sql, duration, "success")
                    if self.show_model_in_response:
                        audit_answer += f"\n\n_Ответ от модели: {state.last_model_used}_"
                    self._log_audit(state, msg, audit_question, audit_answer)
                    return ChatResponse(text=audit_answer)
                except Exception as e:
                    duration = time.perf_counter() - start_time
                    err_str = str(e)
                    self._log_sql_audit(sql, duration, "error", err_str)
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

                structure_markers = ["описание", "поля", "структура", "метаданные"]
                time_markers = ["когда", "дата", "время", "последние", "последняя", "свежие", "newest", "latest"]

                has_structure_context = any(marker in recent_history_text for marker in structure_markers)
                has_time_context = any(marker in recent_history_text for marker in time_markers)

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

                    audit_answer = self._format_sql_result(rows)
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

                base_prompt = (
                    "Ты — старший аналитик данных (Senior Data Analyst). Твоя задача — не просто находить цифры, а находить ПРИЧИНЫ явлений (почему упали продажи, почему выросла нагрузка, кто виноват в простое).\n"
                    "У тебя есть доступ к базе данных через инструменты:\n"
                    "1. execute_sql — выполнить любой SELECT-запрос.\n"
                    "2. get_table_schema — узнать поля таблицы.\n"
                    "3. get_relations — узнать связи таблиц.\n\n"
                    "ПРАВИЛА АНАЛИЗА:\n"
                    "- Если пользователь задает сложный вопрос (например, 'почему упали продажи?', 'в чем причина?', 'сравни'), ты ДОЛЖЕН выполнить НЕСКОЛЬКО SQL-запросов подряд, чтобы собрать данные.\n"
                    "- Например, сначала запроси общее количество продаж за прошлый месяц, затем за этот. Найди разницу.\n"
                    "- Затем запроси детализацию по операторам или по дням, чтобы найти аномалии (кто перестал продавать, в какой день был провал).\n"
                    "- Только получив все данные, напиши аналитический вывод: 'Продажи упали на X%. Основная причина — падение активности оператора Y (с 10 до 2 продаж). Возможно, он был в отпуске или у него технические проблемы.'\n\n"
                    "ПРАВИЛА ВЫВОДА:\n"
                    "- Не выводи сырые SQL-запросы, если не просят. Выводи готовый анализ.\n"
                    "- Не выводи Markdown-таблицы. Используй списки и абзацы.\n"
                    "- Если данных нет — так и скажи.\n\n"
                    "КРИТИЧЕСКОЕ ПРАВИЛО: Ты ОБЯЗАН перед КАЖДЫМ вызовом инструмента (execute_sql, get_table_schema, execute_template) написать пользователю краткий комментарий о том, что ты собираешься сделать. НАПРИМЕР: 'Сейчас посмотрю структуру таблицы...', 'Выполню запрос для группировки по дням...'. Это обязательно. Без этого текст не будет отправлен пользователю, и он подумает, что бот завис."
                )
                schema = await self._get_db_schema(state)
                _now = datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")
                system_content = base_prompt + f"\n\nСегодня: {_now} (локальное время сервера). Используй эту дату для 'сегодня', 'вчера', 'на этой неделе' и т.п." + (
                    "\n\n" + schema if schema else "")

                # Подсказка о последней таблице, если запрос касается времени
                text_lower = msg.text.lower()
                time_markers = ["когда", "дата", "время", "последние", "последняя", "свежие", "newest", "latest"]
                if any(marker in text_lower for marker in time_markers):
                    last_table = self._extract_last_table_from_history(state)
                    if last_table:
                        system_content += f"\n\nПодсказка: Вероятно, речь о таблице '{last_table}'. Если имя таблицы не указано явно, используй её для запросов о датах."

                # Проверка контекста
                context_limit = self.llm_client.get_context_length(model_to_use)
                history_chars = sum(len(str(m)) for m in state.history)
                estimated_tokens = (len(system_content) + history_chars + len(msg.text)) // 3

                if estimated_tokens > context_limit * 0.8:
                    if len(state.history) > 2:
                        logger.warning("Context overflow detected. Trimming history from %d to 2 messages.", len(state.history))
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

                messages: list[dict] = [
                    {"role": "system", "content": system_content},
                ]
                messages.extend(state.history[-self.max_history:])
                messages.append({"role": "user", "content": msg.text})

                if model_to_use == self.analysis_model:
                    # Аналитическая модель: вызов без tools
                    response = await self.llm_client.chat(messages, tools=None, model_name=model_to_use)
                    choice = response["choices"][0]["message"]
                    final_text = choice.get("content") or ""

                    await self._save_history(state, user_id, msg.text, final_text)

                    audit_answer = final_text
                    self._log_audit(state, msg, audit_question, audit_answer)

                    if self.show_model_in_response:
                        final_text += f"\n\n_Ответ от модели: {state.last_model_used}_"

                    return ChatResponse(text=final_text)

                # ---------- ЦИКЛ АГЕНТА (до 5 шагов) ----------
                max_steps = 5
                step = 0
                final_text = ""
                tools_arg = TOOLS_DEFINITION + [EXECUTE_TEMPLATE_TOOL]

                while step < max_steps:
                    step += 1

                    # Отправляем текущие сообщения в LLM
                    response = await self.llm_client.chat(
                        messages,
                        tools=tools_arg,
                        model_name=model_to_use,
                        tool_choice="auto",
                    )
                    choice = response["choices"][0]["message"]
                    tool_calls = choice.get("tool_calls")

                    # Принудительно отправляем статус, если модель написала текст
                    if choice.get("content") and msg.status_callback:
                        clean_text = choice.get("content").strip()
                        if clean_text:
                            await msg.status_callback(f"🤖 {clean_text}")
                    
                    # Если модель не написала текст, но вызвала инструмент — отправляем заглушку
                    elif choice.get("tool_calls") and msg.status_callback:
                        tool_name = choice["tool_calls"][0]["function"]["name"]
                        await msg.status_callback(f"🤖 Выполняю {tool_name}...")

                    # Если модель не вызвала инструменты — значит, она готова дать финальный ответ
                    if not tool_calls:
                        final_text = choice.get("content") or ""
                        break

                    # Если вызвала, добавляем их в историю
                    messages.append({
                        "role": "assistant",
                        "content": choice.get("content") or "",
                        "tool_calls": tool_calls,
                    })

                    # Обрабатываем все вызовы в этом шаге
                    for tool_call in tool_calls:
                        tool_name = tool_call["function"]["name"]

                        if tool_name not in {"execute_sql", "get_table_schema", "get_relations", "execute_template"}:
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
                                tpl_name = args.get("template_name")
                                tpl_limit = args.get("limit")
                                tpl_period = args.get("period")

                                result_text = await self._run_yaml_template(tpl_name, tpl_limit, tpl_period, state)

                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_call["id"],
                                    "content": result_text,
                                })
                            except Exception as e:
                                logger.error("Ошибка выполнения шаблона: %s", e)
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_call["id"],
                                    "content": f"Ошибка выполнения шаблона: {e}",
                                })
                            continue

                        if tool_name == "get_relations":
                            try:
                                args = json.loads(tool_call["function"]["arguments"])
                                table_name = args.get("table_name", "")
                            except (json.JSONDecodeError, TypeError):
                                messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": "Ошибка: некорректные аргументы инструмента"})
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

                        elif tool_name == "execute_sql":
                            try:
                                args = json.loads(tool_call["function"]["arguments"])
                                sql = args.get("sql", "")
                            except (json.JSONDecodeError, TypeError):
                                messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": "Ошибка: некорректные аргументы инструмента"})
                                continue
                            is_valid, reason = validate_sql(sql)
                            if not is_valid:
                                log_violation(user_id or "unknown", sql, reason)
                                messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": f"Ошибка: {reason}"})
                                continue
                            if state.current_db is None:
                                messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": "Ошибка: БД не выбрана"})
                                continue
                            start_time = time.perf_counter()
                            try:
                                rows = await self.db_manager.execute_query(state.current_db, sql)
                                duration = time.perf_counter() - start_time
                                if rows is None:
                                    rows = []
                                result_text = self._format_sql_result(rows)
                                messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": result_text})
                                self._log_sql_audit(sql, duration, "success")
                                logger.info("Executed SQL: %s", sql)
                                # Запоминаем реальный SQL в истории для будущих уточнений
                                state.history.append({"role": "system", "content": f"[executed_sql] {sql}"})
                            except Exception as e:
                                duration = time.perf_counter() - start_time
                                err_str = str(e)
                                self._log_sql_audit(sql, duration, "error", err_str)
                                if "timeout" in err_str.lower() or "MAX_EXECUTION_TIME" in err_str:
                                    timeout_val = os.environ.get('SQL_QUERY_TIMEOUT', '15')
                                    error_msg = f"Запрос выполнялся слишком долго (лимит {timeout_val} сек) и был прерван."
                                else:
                                    error_msg = f"Ошибка выполнения запроса: {err_str}"
                                messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": error_msg})

                # После цикла (или если он прервался по max_steps)
                if not final_text:
                    # Если мы сделали 5 шагов и модель всё еще вызывает инструменты,
                    # просим её дать финальный ответ на основе того, что есть
                    messages.append({"role": "system", "content": "Ты уже собрал достаточно данных. НАПИШИ ФИНАЛЬНЫЙ ОТВЕТ ПОЛЬЗОВАТЕЛЮ. Больше не вызывай инструменты."})
                    final_response = await self.llm_client.chat(messages, tools=None, model_name=model_to_use)
                    final_text = final_response["choices"][0]["message"].get("content") or ""

                # ГЛОБАЛЬНАЯ ЗАЩИТА ОТ ПУСТОГО ОТВЕТА
                if not final_text or not final_text.strip():
                    final_text = "⚠️ Я собрал данные, но не смог сформулировать вывод. Попробуйте уточнить вопрос."

                # Убираем псевдо-вызовы
                final_text = re.sub(r"execute_sql\s*\([^)]*\)", "", final_text).strip()

                await self._save_history(state, user_id, msg.text, final_text)

                audit_answer = final_text
                self._log_audit(state, msg, audit_question, audit_answer)

                if self.show_model_in_response:
                    final_text += f"\n\n_Ответ от модели: {state.last_model_used}_"

                return ChatResponse(text=final_text)
                # ---------- КОНЕЦ ЦИКЛА ----------

            except Exception as e:
                audit_answer = f"Ошибка LLM: {e}"
                self._log_audit(state, msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)
        finally:
            lock.release()

    # ---------- Проверка, требует ли запрос данные ----------
    def _should_force_tool(self, text: str) -> bool:
        """Проверяет, является ли запрос таким, что LLM ОБЯЗАНА вызвать execute_sql."""
        text_lower = text.lower()

        meta_markers = [
            "покажи запрос", "какой запрос", "каким запросом", "как ты получил",
            "как ты это получил", "покажи sql", "какой sql", "покажи код",
            "каким образом", "что ты выполнил", "что ты запустил",
        ]
        if any(marker in text_lower for marker in meta_markers):
            return False

        confirm_markers = ["да", "выведи", "все", "всё", "проверку", "посчитай", "считай"]
        if any(marker in text_lower for marker in confirm_markers):
            return True

        data_markers = [
            "сколько", "кто", "какие", "какой", "какая",
            "выведи", "список", "за сегодня", "сегодня", "вчера",
            "на этой неделе", "на прошлой неделе",
            "правки", "правок", "внес", "внесли", "работал", "работали",
            "записей", "записи", "строк", "строки", "count(", "sum(",
            "total", "итого", "итог", "количество", "топ", "лучш", "худш", "рейтинг", "лидер"
        ]
        return any(marker in text_lower for marker in data_markers)

    # ---------- Сохранение истории ----------
    async def _save_history(self, state: SessionState, user_id: str, user_text: str, assistant_text: str) -> None:
        state.history.append({"role": "user", "content": user_text})
        state.history.append({"role": "assistant", "content": assistant_text})
        if len(state.history) > self.max_history:
            state.history = state.history[-self.max_history:]
        if self.cache_manager is not None:
            await self.cache_manager.set(
                f"repdix:history:{user_id}",
                json.dumps(state.history, ensure_ascii=False),
                ttl=86400,
            )

    # ---------- Аудит ----------
    def _log_audit(self, state: SessionState, msg: ChatMessage, question: str, answer: str) -> None:
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

    def _log_sql_audit(self, sql: str, duration: float, status: str, error: str | None = None) -> None:
        data = {
            "timestamp": datetime.now().isoformat(),
            "type": "sql_llm",
            "sql": sql,
            "duration_sec": round(duration, 3),
            "status": status,
        }
        if error:
            data["error"] = error
        audit_logger.info(json.dumps(data, ensure_ascii=False))
