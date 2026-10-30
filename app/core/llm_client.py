# app/core/llm_client.py
dv_file_version = '261030.01'
"""Клиент для работы с LLM через OpenAI-совместимый API.

Ключевые изменения версии 261030.01:
- chat(): условие tools-fallback сделано точным. Раньше проверка
  `"tools" in err_lower or "tool" in err_lower` ловила подстроку "tool"
  почти в любом сообщении об ошибке (tool_calls, tool_choice, имя модели
  с "tool") и приводила к тихому повтору без инструментов на несвязанную
  ошибку (rate limit, timeout). Теперь tools-fallback требует либо
  "tools" в тексте ошибки, либо "tool" вместе с одним из маркеров
  ("not supported", "does not support", "unsupported",
  "unknown parameter", "invalid parameter"). В остальных случаях
  исключение пробрасывается как раньше.

Ключевые изменения версии 261030.01:
- chat(): блок передачи max_tokens сделан строже. Раньше проверка была
  `if max_tokens:` — значения 0 и "0" молча отбрасывались, а нечисловое
  значение приводило к неинформативному падению. Теперь: `is not None`,
  приведение к int, отсев неположительных значений и warning в лог при
  некорректном значении. Лимит ответа задаётся в config/models.yaml.

Ключевые изменения версии 261028.07:
- temperature вынесена в конфиг (models.yaml) с дефолтом 0.0. Раньше была
  жёстко зашита 0.0 — README обещал 0.3, а поведение не настраивалось.
  Для tool_calling_model разумно 0.0, для analysis_model — 0.2-0.3.
- get_client() больше не падает при пустом api_key для локальных моделей
  (LM Studio/Ollama): пустая строка допустима. Проверка "ASCII" применяется
  только если ключ непустой.
- get_client() поддерживает hot-reload models.yaml: если mtime изменился,
  конфиг перечитывается. Раньше правка models.yaml требовала перезапуска.
  Клиенты при этом не пересоздаются (только при изменении base_url/api_key
  для конкретной модели — см. _clients_meta).
- chat(): при supports_tools=False и переданных tools — логируется warning
  (раньше tools молча игнорировались, и модель не могла выполнить SQL,
  что выглядело как "модель не умеет").
- chat(): retry при "tool_choice not supported" теперь также покрывает
  случай "tools not supported" (некоторые локальные модели не принимают
  tools в принципе).
- chat(): добавлена поддержка response_format (для JSON-режима, если модель
  поддерживает). Пока не используется, но пригодится.
- chat(): добавлен max_tokens из конфига (некоторые локальные модели
  требуют явного лимита, иначе отвечают пустотой).
- get_context_length(): fallback не 32768, а значение из конфига
  DEFAULT_CONTEXT_LENGTH (env) — чтобы не хардкодить.
- resolve_model_name(): поддержка точного совпадения с префиксом (было),
  плюс регистронезависимое сравнение (часто пользователь пишет
  /model TOOL_CALLING_MODEL).
- close(): закрывает клиенты с try/except, чтобы один упавший клиент не
  блокировал остальные.
- _clients_meta: отслеживает base_url/api_key/temperature для каждой модели,
  чтобы при hot-reload пересоздавать клиент только при изменении.
- Docstring в шапке вместо заглушки.
"""

import asyncio
import logging
import os
from typing import Any

import yaml
from openai import AsyncOpenAI, NOT_GIVEN

logger = logging.getLogger(__name__)


# Дефолтный лимит контекста (если в models.yaml не задан context_length)
_DEFAULT_CONTEXT_LENGTH = int(os.environ.get("DEFAULT_CONTEXT_LENGTH") or "32768")


class LLMClient:
    """Обёртка над AsyncOpenAI для работы с локальными и удалёнными LLM."""

    def __init__(
        self,
        config_path: str = "config/models.yaml",
        default_model: str = "tool_calling_model",
    ) -> None:
        """Инициализация LLM-клиента.

        Args:
            config_path: Путь к YAML-файлу с конфигурацией моделей.
            default_model: Имя модели по умолчанию.
        """
        self.config_path = config_path
        self.default_model = default_model
        self.config: dict = {}
        self.clients: dict[str, AsyncOpenAI] = {}
        # Метаданные клиентов: чтобы при hot-reload пересоздавать только
        # те, у которых изменились base_url/api_key/temperature.
        self._clients_meta: dict[str, dict] = {}
        self._context_lengths: dict[str, int] = {}
        self._mtime: float = 0.0
        self.load_config()

    # ------------------------------------------------------------------
    # Загрузка конфига (hot-reload по mtime)
    # ------------------------------------------------------------------
    def load_config(self, force: bool = False) -> None:
        """Загружает конфигурацию моделей из YAML и подставляет ${VAR}.

        Args:
            force: Если True — перечитывает файл даже при неизменном mtime.
        """
        # Проверка mtime (hot-reload)
        if not force:
            try:
                current_mtime = os.path.getmtime(self.config_path)
            except OSError:
                current_mtime = 0.0
            if current_mtime and current_mtime == self._mtime:
                return  # ничего не изменилось

        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = yaml.safe_load(f) or {}
        except FileNotFoundError:
            logger.warning("Config file not found: %s", self.config_path)
            self.config = {}
            return
        except yaml.YAMLError as e:
            logger.error("Ошибка парсинга %s: %s", self.config_path, e)
            self.config = {}
            return

        models = self.config.get('models', {})
        if not isinstance(models, dict):
            logger.error("config['models'] должен быть словарём, получено: %r", type(models))
            self.config['models'] = {}
            return

        # Обновляем context_length и подставляем ${VAR}
        new_context_lengths: dict[str, int] = {}
        for model_name, model_conf in models.items():
            if not isinstance(model_conf, dict):
                logger.warning("Конфиг модели %s не словарь, пропускаем", model_name)
                continue
            for key, value in list(model_conf.items()):
                if isinstance(value, str) and value.startswith('${') and value.endswith('}'):
                    var_name = value[2:-1]
                    env_val = os.environ.get(var_name)
                    if env_val is None:
                        logger.warning(
                            "Environment variable %s not found for model %s (key=%s)",
                            var_name, model_name, key,
                        )
                        model_conf[key] = ""
                    else:
                        model_conf[key] = env_val

            try:
                ctx_len = int(model_conf.get('context_length', _DEFAULT_CONTEXT_LENGTH))
            except (TypeError, ValueError):
                ctx_len = _DEFAULT_CONTEXT_LENGTH
            new_context_lengths[model_name] = ctx_len

        self._context_lengths = new_context_lengths

        # Обновляем mtime только при успехе
        try:
            self._mtime = os.path.getmtime(self.config_path)
        except OSError:
            self._mtime = 0.0

        logger.info("models.yaml загружен. Моделей: %d", len(models))

    # ------------------------------------------------------------------
    # Информация о моделях
    # ------------------------------------------------------------------
    def get_model_names(self) -> list[str]:
        """Возвращает список имён моделей из конфигурации."""
        self.load_config()  # hot-reload
        models = self.config.get('models', {})
        if not isinstance(models, dict):
            return []
        return list(models.keys())

    def resolve_model_name(self, short_name: str) -> str | None:
        """Разрешает сокращённое имя модели в полное имя из конфигурации.

        Сравнение регистронезависимое (пользователь может написать
        /model TOOL_CALLING_MODEL).
        """
        self.load_config()
        models = self.config.get('models', {})
        if not isinstance(models, dict) or not short_name:
            return None

        short_lower = short_name.lower()
        # 1) Точное совпадение (без учёта регистра)
        for name in models.keys():
            if name.lower() == short_lower:
                return name
        # 2) Префикс "<short>_"
        prefix = short_lower + "_"
        matches = [name for name in models.keys() if name.lower().startswith(prefix)]
        if len(matches) == 1:
            return matches[0]
        return None

    def get_context_length(self, model_name: str | None = None) -> int:
        """Возвращает лимит контекста для указанной модели."""
        self.load_config()
        name = model_name or self.default_model
        return self._context_lengths.get(name, _DEFAULT_CONTEXT_LENGTH)

    def _get_model_conf(self, name: str) -> dict:
        """Возвращает конфиг модели или пустой dict, если её нет."""
        models = self.config.get('models', {})
        if not isinstance(models, dict):
            return {}
        conf = models.get(name)
        return conf if isinstance(conf, dict) else {}

    # ------------------------------------------------------------------
    # Клиенты
    # ------------------------------------------------------------------
    def _make_client(self, name: str, model_conf: dict) -> AsyncOpenAI:
        """Создаёт AsyncOpenAI для модели с проверками."""
        api_key = model_conf.get('api_key', '') or ''
        base_url = model_conf.get('base_url', '') or ''

        # Пустой ключ допустим только для локальных моделей
        is_local = (
            "localhost" in base_url
            or "127.0.0.1" in base_url
            or not base_url  # если base_url не задан — доверяем дефолту OpenAI
        )
        if not api_key and not is_local:
            raise ValueError(
                f"API key пуст для удалённой модели {name} ({base_url}). "
                "Укажите api_key в config/models.yaml или переменную окружения."
            )
        # Проверка ASCII — только если ключ непустой
        if api_key:
            try:
                api_key.encode('ascii')
            except UnicodeEncodeError:
                raise ValueError(
                    f"API key для модели {name} содержит недопустимые символы (только ASCII)"
                )

        return AsyncOpenAI(base_url=base_url or None, api_key=api_key or "not-needed")

    def get_client(self, model_name: str | None = None) -> tuple[AsyncOpenAI, str, bool]:
        """Возвращает клиент AsyncOpenAI для указанной модели.

        При hot-reload models.yaml пересоздаёт клиент, если изменились
        base_url / api_key.

        Returns:
            (client, actual_model_name, supports_tools)

        Raises:
            ValueError: Если модель не найдена или ключ невалиден.
        """
        self.load_config()  # hot-reload

        name = model_name or self.default_model
        model_conf = self._get_model_conf(name)
        if not model_conf:
            raise ValueError(f"Model '{name}' not found in config")

        actual_model_name = model_conf.get('model_name', name)
        supports_tools = bool(model_conf.get('supports_tools', False))

        api_key = model_conf.get('api_key', '') or ''
        base_url = model_conf.get('base_url', '') or ''

        # Метаданные для отслеживания изменений
        meta = {"base_url": base_url, "api_key": api_key}

        existing = self.clients.get(name)
        existing_meta = self._clients_meta.get(name)
        if existing is None or existing_meta != meta:
            # Создаём новый клиент (старый, если был, нужно закрыть)
            if existing is not None:
                logger.info("Модель %s изменилась в конфиге — пересоздаём клиент", name)
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(existing.close())
                except RuntimeError:
                    pass  # нет event loop (тесты, CLI) — GC закроет
            self.clients[name] = self._make_client(name, model_conf)
            self._clients_meta[name] = meta

        return self.clients[name], actual_model_name, supports_tools

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model_name: str | None = None,
        tool_choice: str | None = None,
        response_format: dict | None = None,
    ) -> dict:
        """Отправляет запрос к LLM и возвращает ответ.

        Args:
            messages: Список сообщений в формате OpenAI.
            tools: Список инструментов для tool_calls.
            model_name: Имя модели из конфигурации.
            tool_choice: "auto", "required" или "none".
            response_format: {"type": "json_object"} для JSON-режима.

        Лимит длины ответа берётся из max_tokens в config/models.yaml
        (для модели) и передаётся в API как kwargs["max_tokens"].

        Returns:
            Словарь с ответом модели (model_dump).
        """
        self.load_config()  # hot-reload

        client, actual_model_name, supports_tools = self.get_client(model_name)
        name = model_name or self.default_model
        model_conf = self._get_model_conf(name)

        # temperature: из конфига, дефолт 0.0
        try:
            temperature = float(model_conf.get("temperature", 0.0))
        except (TypeError, ValueError):
            temperature = 0.0

        kwargs: dict[str, Any] = {
            "model": actual_model_name,
            "messages": messages,
            "temperature": temperature,
        }

        # max_tokens (опционально). Ограничивает длину ответа — экономия
        # токенов/денег. Задаётся в config/models.yaml.
        max_tokens = model_conf.get("max_tokens")
        if max_tokens is not None:
            try:
                max_tokens_int = int(max_tokens)
            except (TypeError, ValueError):
                max_tokens_int = 0
                logger.warning(
                    "Некорректный max_tokens=%r для модели %s — параметр пропущен",
                    max_tokens, name,
                )
            if max_tokens_int > 0:
                kwargs["max_tokens"] = max_tokens_int

        # tools
        if supports_tools and tools:
            kwargs["tools"] = tools
            if tool_choice in ("auto", "required", "none"):
                kwargs["tool_choice"] = tool_choice
        else:
            if tools and not supports_tools:
                logger.warning(
                    "Модель %s не поддерживает tools (supports_tools=False), "
                    "но engine передал %d инструмент(ов). Инструменты будут "
                    "проигнорированы — модель не сможет выполнить SQL.",
                    name, len(tools),
                )
            kwargs["tools"] = NOT_GIVEN

        # response_format
        if response_format is not None:
            kwargs["response_format"] = response_format

        try:
            response = await client.chat.completions.create(**kwargs)
            return response.model_dump()
        except Exception as e:
            err_lower = str(e).lower()
            # Fallback: некоторые модели не принимают tool_choice="required"
            if "tool_choice" in err_lower and "tool_choice" in kwargs:
                logger.warning(
                    "LLM не поддерживает tool_choice='%s', повторяем без него: %s",
                    tool_choice, e,
                )
                kwargs.pop("tool_choice", None)
                response = await client.chat.completions.create(**kwargs)
                return response.model_dump()
            # Fallback: некоторые модели не принимают tools вообще.
            # Подстрока "tool" сама по себе слишком широкая (tool_calls,
            # tool_choice, имя модели с "tool"), поэтому для случая,
            # когда явного "tools" в тексте нет, требуем маркер
            # неподдерживаемого/неизвестного параметра.
            _unsupported_markers = (
                "not supported",
                "does not support",
                "unsupported",
                "unknown parameter",
                "invalid parameter",
            )
            tools_fallback = (
                "tools" in err_lower
                or (
                    "tool" in err_lower
                    and any(marker in err_lower for marker in _unsupported_markers)
                )
            )
            if tools_fallback and kwargs.get("tools") is not NOT_GIVEN:
                logger.warning(
                    "LLM не поддерживает tools, повторяем без них: %s", e,
                )
                kwargs["tools"] = NOT_GIVEN
                kwargs.pop("tool_choice", None)
                response = await client.chat.completions.create(**kwargs)
                return response.model_dump()
            raise

    # ------------------------------------------------------------------
    # Закрытие
    # ------------------------------------------------------------------
    async def close(self) -> None:
        """Закрывает все клиенты AsyncOpenAI.

        Ошибка закрытия одного клиента не блокирует закрытие остальных.
        """
        clients = list(self.clients.values())
        self.clients.clear()
        self._clients_meta.clear()
        for client in clients:
            try:
                await client.close()
            except Exception as e:
                logger.warning("Ошибка закрытия LLM-клиента: %s", e)
