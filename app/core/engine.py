# app/core/engine.py
dv_file_version = '261026.08'
"""Ядро repdix: диспетчер сообщений, не зависящий от мессенджера."""
import json
import logging
import re
from datetime import datetime
from pathlib import Path

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
                    "sql": {
                        "type": "string",
                        "description": "SQL-запрос (только SELECT)",
                    }
                },
                "required": ["sql"],
            },
        },
    }
]


class RepdixEngine:
    """Главный диспетчер: принимает ChatMessage, возвращает ChatResponse."""

    def __init__(self, db_manager=None, llm_client=None, cache_manager=None, default_db: str | None = None) -> None:
        self.db_manager = db_manager
        self.llm_client = llm_client
        self.cache_manager = cache_manager
        self.default_db = default_db
        self.current_db: str | None = default_db
        self.current_model: str | None = None
        self.user_id: str | None = None
        self.history: list[dict] = []  # история диалога (role, content)
        self.max_history = 10  # максимум сообщений в истории (5 пар user/assistant)
        self.tool_model = "tool_calling_model"
        self.analysis_model = "deep_analysis_model"

    def _route_query(self, text: str) -> str:
        """Определяет модель для обработки запроса на основе его содержания."""
        if self.current_model is not None:
            return self.current_model

        text_lower = text.lower()
        analysis_markers = [
            "почему", "проанализируй", "сравни", "тренд", "динамика",
            "причина", "объясни", "прогноз", "статистика", "аналитика"
        ]

        if any(marker in text_lower for marker in analysis_markers) or len(text) > 200:
            return self.analysis_model

        return self.tool_model

    async def _get_db_schema(self) -> str:
        """Получает схему текущей БД в текстовом виде."""
        if self.current_db is None:
            return ""
        if self.db_manager is None:
            return ""

        try:
            sql = (
                "SELECT table_name, column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema = DATABASE() "
                "ORDER BY table_name, ordinal_position"
            )
            rows = await self.db_manager.execute_query(self.current_db, sql)
            if not rows:
                return ""

            tables: dict[str, list[str]] = {}
            for row in rows:
                table_name = row.get("table_name", "")
                column_name = row.get("column_name", "")
                data_type = row.get("data_type", "")
                if table_name not in tables:
                    tables[table_name] = []
                tables[table_name].append(f"{column_name} ({data_type})")

            table_names = list(tables.keys())
            limited_tables = table_names[:50]
            remaining_count = len(table_names) - len(limited_tables)

            lines = [f"Схема БД {self.current_db}:"]
            for t_name in limited_tables:
                cols = ", ".join(tables[t_name])
                lines.append(f"Таблица {t_name}: {cols}")

            if remaining_count > 0:
                lines.append(f"... и ещё {remaining_count} таблиц")

            return "\n".join(lines)
        except Exception:
            return ""

    async def process_message(self, msg: ChatMessage) -> ChatResponse:
        """Обработка входящего сообщения."""
        self.user_id = msg.user_id
        text = msg.text.strip()
        audit_question = text
        audit_answer = ""

        # Обработка команды /help
        if text == "/help":
            help_path = Path("config/help.md")
            try:
                if help_path.exists():
                    content = help_path.read_text(encoding="utf-8")
                    audit_answer = content
                else:
                    audit_answer = "Помощь временно недоступна. Используйте /db, /model, /sql, /history, /clear."
            except Exception as e:
                logger.error(f"Error reading help file: {e}")
                audit_answer = "Помощь временно недоступна. Используйте /db, /model, /sql, /history, /clear."
            
            self._log_audit(msg, audit_question, audit_answer)
            return ChatResponse(text=audit_answer)

        # Загрузка истории из кэша
        if self.cache_manager is not None:
            cache_key = f"repdix:history:{msg.user_id}"
            cached = await self.cache_manager.get(cache_key)
            if cached:
                try:
                    self.history = json.loads(cached)
                except json.JSONDecodeError:
                    self.history = []
            else:
                self.history = []

        # Обработка команды /clear
        if text == "/clear":
            self.history = []
            if self.cache_manager is not None:
                await self.cache_manager.delete(f"repdix:history:{msg.user_id}")
            audit_answer = "История диалога очищена."
            self._log_audit(msg, audit_question, audit_answer)
            return ChatResponse(text=audit_answer)

        # Обработка команды /tokens
        if text == "/tokens":
            total_chars = sum(len(str(m)) for m in self.history)
            total_tokens = total_chars // 4
            
            model_to_use = self._route_query(text)
            context_limit = 32768
            if self.llm_client is not None:
                context_limit = self.llm_client.get_context_length(model_to_use)
            
            percent = (total_tokens / context_limit * 100) if context_limit > 0 else 0
            
            audit_answer = (
                f"Сообщений в истории: {len(self.history)} из {self.max_history}. "
                f"Лимит контекста: {context_limit} токенов. Текущий размер: ~{total_tokens} токенов ({percent:.1f}%). "
                f"Использование контекста: ~{total_tokens}/{context_limit} ({percent:.1f}%)."
            )
            self._log_audit(msg, audit_question, audit_answer)
            return ChatResponse(text=audit_answer)

        # Обработка команды /history
        if text == "/history":
            if not self.history:
                audit_answer = "История диалога пуста."
                self._log_audit(msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)
            lines = []
            for m in self.history:
                role = m.get("role", "unknown")
                content = str(m.get("content", ""))[:200]
                lines.append(f"[{role}] {content}\n")
            audit_answer = "".join(lines)
            self._log_audit(msg, audit_question, audit_answer)
            return ChatResponse(text=audit_answer)

        # Обработка команды /compact
        if text == "/compact":
            if not self.history or len(self.history) < 4:
                audit_answer = "История слишком коротка для сжатия."
                self._log_audit(msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)
            if self.llm_client is None:
                audit_answer = "LLM не подключена, сжатие невозможно."
                self._log_audit(msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)
            try:
                messages = [
                    {"role": "system", "content": "Сожми следующий диалог в краткое резюме (не более 3-4 предложений). Сохрани ключевые факты, вопросы и ответы."},
                    {"role": "user", "content": json.dumps(self.history, ensure_ascii=False)}
                ]
                response = await self.llm_client.chat(messages, model_name=self.current_model)
                summary = response["choices"][0]["message"]["content"]
                self.history = [{"role": "system", "content": f"Резюме предыдущего диалога: {summary}"}]
                if self.cache_manager is not None:
                    await self.cache_manager.set(f"repdix:history:{msg.user_id}", json.dumps(self.history, ensure_ascii=False), ttl=86400)
                audit_answer = f"История сжата. Новый размер: {len(self.history)} сообщений. Резюме: {summary[:300]}..."
                self._log_audit(msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)
            except Exception as e:
                audit_answer = f"Ошибка сжатия истории: {str(e)}"
                self._log_audit(msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)

        # Обработка команды /settings
        if text == "/settings":
            active_model_display = self.current_model or f"авто-выбор (роутер: {self.tool_model} / {self.analysis_model})"
            lines = [
                f"Текущая БД: {self.current_db or 'не выбрана'}",
                f"Активная модель: {active_model_display}",
                f"Лимит истории: {self.max_history}",
            ]
            if self.llm_client is not None:
                model_for_limit = self.current_model or self.tool_model
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
            audit_answer = "\n".join(lines)
            self._log_audit(msg, audit_question, audit_answer)
            return ChatResponse(text=audit_answer)

        # Обработка команды /db
        if text.startswith("/db "):
            db_name = text[4:].strip()
            self.current_db = db_name
            audit_answer = f"БД: {self.current_db}"
            self._log_audit(msg, audit_question, audit_answer)
            return ChatResponse(text=audit_answer)

        if text == "/db":
            audit_answer = f"Текущая БД: {self.current_db}"
            self._log_audit(msg, audit_question, audit_answer)
            return ChatResponse(text=audit_answer)

        # Обработка команды /model
        if text.startswith("/model "):
            model_name = text[7:].strip()
            self.current_model = model_name
            audit_answer = f"Модель: {self.current_model}"
            self._log_audit(msg, audit_question, audit_answer)
            return ChatResponse(text=audit_answer)

        if text == "/model":
            if self.current_model is None:
                audit_answer = "Текущая модель: не выбрана (используется по умолчанию)"
            else:
                audit_answer = f"Текущая модель: {self.current_model}"
            self._log_audit(msg, audit_question, audit_answer)
            return ChatResponse(text=audit_answer)

        # Обработка команды /models
        if text == "/models":
            if self.llm_client is not None:
                names = self.llm_client.get_model_names()
                audit_answer = "Доступные модели: " + ", ".join(names)
            else:
                audit_answer = "LLM не подключена"
            self._log_audit(msg, audit_question, audit_answer)
            return ChatResponse(text=audit_answer)

        # Обработка команды /sql
        if text.startswith("/sql "):
            sql = text[5:].strip()
            is_valid, reason = validate_sql(sql)

            if not is_valid:
                log_violation(self.user_id or "unknown", sql, reason)
                audit_answer = f"SQL отклонён: {reason}"
                self._log_audit(msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)

            if self.current_db is None:
                audit_answer = "Сначала выберите БД командой /db <name>"
                self._log_audit(msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)

            try:
                rows = await self.db_manager.execute_query(self.current_db, sql)
                if rows is None:
                    rows = []
                result_json = json.dumps(rows, ensure_ascii=False, default=str, indent=2)
                audit_answer = result_json
                self._log_audit(msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)
            except Exception as e:
                audit_answer = f"Ошибка выполнения запроса: {str(e)}"
                self._log_audit(msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)

        # Детерминированный запрос имени таблицы
        if self.current_db is not None and re.fullmatch(r"[a-zA-Z0-9_]+", text):
            try:
                sql = f"SELECT column_name, data_type FROM information_schema.columns WHERE table_name = '{text}'"
                rows = await self.db_manager.execute_query(self.current_db, sql)
                if rows is None:
                    rows = []
                result_json = json.dumps(rows, ensure_ascii=False, default=str, indent=2)
                audit_answer = result_json
                self._log_audit(msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)
            except Exception as e:
                audit_answer = f"Ошибка выполнения запроса: {str(e)}"
                self._log_audit(msg, audit_question, audit_answer)
                return ChatResponse(text=audit_answer)

        # Обработка через LLM
        if self.llm_client is None:
            audit_answer = f"Эхо: {msg.text}"
            self._log_audit(msg, audit_question, audit_answer)
            return ChatResponse(text=audit_answer)

        try:
            model_to_use = self._route_query(msg.text)
            
            base_prompt = (
                "Ты — помощник по базам данных. Отвечай кратко. Для запросов к БД используй инструмент execute_sql. "
                "Разрешены только SELECT-запросы. Не используй SHOW TABLES. "
                "Для получения списка таблиц используй: SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE(). "
                "Для получения описания полей используй: SELECT column_name, data_type FROM information_schema.columns WHERE table_name = '...'."
            )
            schema = await self._get_db_schema()
            system_content = base_prompt + ("\n\n" + schema if schema else "")

            # Проверка контекста
            context_limit = self.llm_client.get_context_length(model_to_use)
            # Оценка размера: system + history + new message
            history_chars = sum(len(str(m)) for m in self.history)
            estimated_tokens = (len(system_content) + history_chars + len(msg.text)) // 3
            
            if estimated_tokens > context_limit * 0.8:
                if len(self.history) > 2:
                    logger.warning("Context overflow detected. Trimming history from %d to 2 messages.", len(self.history))
                    self.history = self.history[-2:]
                    # Пересчитываем после обрезки
                    history_chars = sum(len(str(m)) for m in self.history)
                    estimated_tokens = (len(system_content) + history_chars + len(msg.text)) // 3
                    if estimated_tokens > context_limit * 0.8:
                        audit_answer = "Контекст переполнен даже после обрезки. Используйте /compact или /clear."
                        self._log_audit(msg, audit_question, audit_answer)
                        return ChatResponse(text=audit_answer)
                else:
                    audit_answer = "Контекст переполнен. Используйте /compact или /clear."
                    self._log_audit(msg, audit_question, audit_answer)
                    return ChatResponse(text=audit_answer)

            messages: list[dict] = [
                {"role": "system", "content": system_content},
            ]
            messages.extend(self.history[-self.max_history:])
            messages.append({"role": "user", "content": msg.text})

            if model_to_use == self.analysis_model:
                # Аналитическая модель: вызов без tools
                response = await self.llm_client.chat(messages, tools=None, model_name=model_to_use)
                choice = response["choices"][0]["message"]
                final_text = choice.get("content") or ""

                self.history.append({"role": "user", "content": msg.text})
                self.history.append({"role": "assistant", "content": final_text})
                if len(self.history) > self.max_history:
                    self.history = self.history[-self.max_history:]

                if self.cache_manager is not None:
                    await self.cache_manager.set(f"repdix:history:{msg.user_id}", json.dumps(self.history, ensure_ascii=False), ttl=86400)

                audit_answer = final_text
                self._log_audit(msg, audit_question, audit_answer)
                return ChatResponse(text=final_text)

            # Исполнительная модель: вызов с tools
            response = await self.llm_client.chat(messages, tools=TOOLS_DEFINITION, model_name=model_to_use)
            choice = response["choices"][0]["message"]

            tool_calls = choice.get("tool_calls")
            if tool_calls:
                # Добавляем ассистентское сообщение с tool_calls ПЕРЕД обработкой вызовов
                messages.append({
                    "role": "assistant",
                    "content": choice.get("content") or "",
                    "tool_calls": tool_calls,
                })

                for tool_call in tool_calls:
                    if tool_call["function"]["name"] != "execute_sql":
                        continue

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
                        log_violation(self.user_id or "unknown", sql, reason)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "content": f"Ошибка: {reason}",
                        })
                        continue

                    if self.current_db is None:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "content": "Ошибка: БД не выбрана",
                        })
                        continue

                    try:
                        rows = await self.db_manager.execute_query(self.current_db, sql)
                        if rows is None:
                            rows = []
                        result_json = json.dumps(rows, ensure_ascii=False, default=str)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "content": result_json,
                        })
                    except Exception as e:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "content": f"Ошибка выполнения запроса: {str(e)}",
                        })

                # Повторный вызов LLM для финального ответа (без tools)
                final_response = await self.llm_client.chat(messages, model_name=model_to_use)
                final_choice = final_response["choices"][0]["message"]
                final_text = final_choice.get("content") or ""

                self.history.append({"role": "user", "content": msg.text})
                self.history.append({"role": "assistant", "content": final_text})
                if len(self.history) > self.max_history:
                    self.history = self.history[-self.max_history:]

                if self.cache_manager is not None:
                    await self.cache_manager.set(f"repdix:history:{msg.user_id}", json.dumps(self.history, ensure_ascii=False), ttl=86400)

                audit_answer = final_text
                self._log_audit(msg, audit_question, audit_answer)
                return ChatResponse(text=final_text)

            final_text = choice.get("content") or ""
            self.history.append({"role": "user", "content": msg.text})
            self.history.append({"role": "assistant", "content": final_text})
            if len(self.history) > self.max_history:
                self.history = self.history[-self.max_history:]

            if self.cache_manager is not None:
                await self.cache_manager.set(f"repdix:history:{msg.user_id}", json.dumps(self.history, ensure_ascii=False), ttl=86400)

            audit_answer = final_text
            self._log_audit(msg, audit_question, audit_answer)
            return ChatResponse(text=final_text)

        except Exception as e:
            audit_answer = f"Ошибка LLM: {e}"
            self._log_audit(msg, audit_question, audit_answer)
            return ChatResponse(text=audit_answer)

    def _log_audit(self, msg: ChatMessage, question: str, answer: str) -> None:
        """Записывает запись аудита в отдельный логгер."""
        data = {
            "timestamp": datetime.now().isoformat(),
            "user_id": msg.user_id,
            "chat_id": msg.chat_id,
            "question": question,
            "answer": answer,
            "db": self.current_db,
            "model": self.current_model or "default",
        }
        audit_logger.info(json.dumps(data, ensure_ascii=False))
