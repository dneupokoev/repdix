# app/core/llm_client.py
dv_file_version = '261026.06'
"""Клиент для работы с LLM через OpenAI-совместимый API."""
import logging
import os

import yaml
from openai import AsyncOpenAI, NOT_GIVEN

logger = logging.getLogger(__name__)


class LLMClient:
    """Обёртка над AsyncOpenAI для работы с локальными и удалёнными LLM."""

    def __init__(self, config_path: str = "config/models.yaml", default_model: str = "tool_calling_model") -> None:
        """Инициализация LLM-клиента.

        Args:
            config_path: Путь к YAML-файлу с конфигурацией моделей.
            default_model: Имя модели по умолчанию.
        """
        self.config_path = config_path
        self.default_model = default_model
        self.config: dict = {}
        self.clients: dict[str, AsyncOpenAI] = {}
        self._context_lengths: dict[str, int] = {}
        self.load_config()

    def load_config(self) -> None:
        """Загружает конфигурацию моделей из YAML и подставляет переменные окружения."""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = yaml.safe_load(f)
        except FileNotFoundError:
            logger.warning("Config file not found: %s", self.config_path)
            self.config = {}
            return

        models = self.config.get('models', {})
        for model_name, model_conf in models.items():
            for key, value in model_conf.items():
                if isinstance(value, str) and value.startswith('${') and value.endswith('}'):
                    var_name = value[2:-1]
                    env_val = os.environ.get(var_name)
                    if env_val is None:
                        logger.warning("Environment variable %s not found for model %s", var_name, model_name)
                        model_conf[key] = ""
                    else:
                        model_conf[key] = env_val

            # Сохраняем context_length
            ctx_len = model_conf.get('context_length', 32768)
            self._context_lengths[model_name] = int(ctx_len)

    def get_model_names(self) -> list[str]:
        """Возвращает список имён моделей из конфигурации."""
        models = self.config.get('models', {})
        return list(models.keys())

    def resolve_model_name(self, short_name: str) -> str | None:
        """Разрешает сокращенное имя модели в полное имя из конфигурации.

        Args:
            short_name: Сокращенное или полное имя модели.

        Returns:
            Полное имя модели, если найдено однозначно, иначе None.
        """
        models = self.config.get('models', {})
        if short_name in models:
            return short_name

        prefix = short_name + "_"
        matches = [name for name in models.keys() if name.startswith(prefix)]

        if len(matches) == 1:
            return matches[0]

        return None

    def get_context_length(self, model_name: str | None = None) -> int:
        """Возвращает лимит контекста для указанной модели.

        Args:
            model_name: Имя модели из конфигурации. Если None — используется default_model.

        Returns:
            Целое число — лимит токенов контекста.
        """
        name = model_name or self.default_model
        return self._context_lengths.get(name, 32768)

    def get_client(self, model_name: str | None = None) -> tuple[AsyncOpenAI, str, bool]:
        """Возвращает клиент AsyncOpenAI для указанной модели.

        Args:
            model_name: Имя модели из конфигурации. Если None — используется default_model.

        Returns:
            Кортеж (client, actual_model_name, supports_tools).

        Raises:
            ValueError: Если модель не найдена в конфигурации.
        """
        name = model_name or self.default_model
        models = self.config.get('models', {})

        if name not in models:
            raise ValueError(f"Model '{name}' not found in config")

        model_conf = models[name]
        actual_model_name = model_conf.get('model_name', name)
        supports_tools = model_conf.get('supports_tools', False)

        api_key = model_conf.get('api_key', '')
        base_url = model_conf.get('base_url', '')
        if not api_key and "localhost" not in base_url and "127.0.0.1" not in base_url:
            raise ValueError(
                f"API key пуст для удалённой модели {name} ({base_url}). "
                "Укажите api_key в config/models.yaml или переменную окружения."
            )
        try:
            api_key.encode('ascii')
        except UnicodeEncodeError:
            raise ValueError(f"API key для модели {name} содержит недопустимые символы (только ASCII)")

        if name not in self.clients:
            self.clients[name] = AsyncOpenAI(
                base_url=base_url,
                api_key=api_key,
            )

        return self.clients[name], actual_model_name, supports_tools

    async def chat(
            self,
            messages: list[dict],
            tools: list[dict] | None = None,
            model_name: str | None = None,
            tool_choice: str | None = None,
    ) -> dict:
        """Отправляет запрос к LLM и возвращает ответ.

        Args:
            messages: Список сообщений в формате OpenAI.
            tools: Список инструментов для tool_calls.
            model_name: Имя модели из конфигурации.
            tool_choice: "auto", "required" или "none" (управление вызовом инструментов).

        Returns:
            Словарь с ответом модели (model_dump).
        """
        client, actual_model_name, supports_tools = self.get_client(model_name)

        kwargs: dict = {
            "model": actual_model_name,
            "messages": messages,
            "temperature": 0.0,  # 0.0 — минимизируем галлюцинации
        }

        if supports_tools and tools:
            kwargs["tools"] = tools
            if tool_choice in ("auto", "required", "none"):
                kwargs["tool_choice"] = tool_choice
        else:
            kwargs["tools"] = NOT_GIVEN

        try:
            response = await client.chat.completions.create(**kwargs)
            return response.model_dump()
        except Exception as e:
            # Защита от ошибок LM Studio / Ollama при неподдерживаемом tool_choice
            if "tool_choice" in str(e).lower() and "tool_choice" in kwargs:
                logger.warning("LLM не поддерживает tool_choice='%s', повторяем без него: %s", tool_choice, e)
                kwargs.pop("tool_choice", None)
                response = await client.chat.completions.create(**kwargs)
                return response.model_dump()
            raise

    async def close(self) -> None:
        """Закрывает все клиенты AsyncOpenAI."""
        for client in self.clients.values():
            await client.close()
        self.clients.clear()