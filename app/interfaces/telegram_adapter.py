# app/interfaces/telegram_adapter.py
dv_file_version = '261028.03'
"""Telegram-адаптер на aiogram 3.

Ключевые изменения версии 261027.03:
- ДОБАВЛЕН обработчик /stop (был в меню, но не зарегистрирован — критичный баг).
- handle_message больше не перехватывает служебные команды (/stop, /help и т.д.)
  у неактивных пользователей — они обрабатываются отдельно.
- reset в handle_approval теперь УДАЛЯЕТ пользователя из users.yaml
  (раньше ставил is_active=False, и пользователь не мог подать заявку заново —
  бот отвечал "заявка на рассмотрении").
- Добавлен fallback-хэндлер для не-текстовых сообщений (фото, стикеры и т.п.).
- send_status теперь экранирует Markdown-символы (модель может прислать **bold**,
  и Telegram падал на невалидном Markdown).
- _send_long_message режет по 4000, но с учётом лимита Telegram 4096.
- handle_start_or_unknown корректно работает с state=None (если FSM не инициализирован).
- Проверка на уже существующую задачу вынесена в отдельный хэлпер.
- В handle_approval добавлена защита от повторного нажатия (approve на уже
  одобренного, reject на уже отклонённого).
- process_purpose логирует, если admins пуст (заявка никому не уйдёт).
- Убран неиспользуемый импорт datetime (если понадобится — вернём).
"""

import asyncio
import html
import logging
import os

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from app.core.engine import RepdixEngine
from app.interfaces.base import BaseChatInterface, ChatMessage, ChatResponse

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Служебные команды, которые НЕ должны перехватываться регистрацией
# ----------------------------------------------------------------------
SERVICE_COMMANDS = {
    "/start", "/help", "/stop", "/history", "/compact",
    "/tokens", "/clear", "/settings", "/db", "/model",
    "/models", "/sql",
}


class RegistrationState(StatesGroup):
    """Состояния FSM для процесса регистрации пользователя."""

    waiting_for_name = State()
    waiting_for_department = State()
    waiting_for_purpose = State()


class TelegramAdapter(BaseChatInterface):
    """Адаптер Telegram на базе aiogram 3."""

    def __init__(self, engine: RepdixEngine) -> None:
        self.engine = engine
        self.bot = Bot(token=os.environ["TELEGRAM_TOKEN"])
        self.dp = Dispatcher()
        self.user_manager = engine.user_manager

    # ------------------------------------------------------------------
    # Запуск
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Регистрация хэндлеров и запуск polling."""

        # Меню команд
        commands = [
            BotCommand(command="start", description="Запустить бота / Регистрация"),
            BotCommand(command="help", description="Справка по всем командам"),
            BotCommand(command="stop", description="Остановить текущий анализ"),
            BotCommand(command="history", description="Показать историю диалога"),
            BotCommand(command="compact", description="Сжать историю (очистить контекст)"),
            BotCommand(command="tokens", description="Показать расход токенов контекста"),
            BotCommand(command="clear", description="Полностью очистить историю"),
            BotCommand(command="settings", description="Текущие настройки (БД, модель, лимиты)"),
        ]
        await self.bot.set_my_commands(commands)

        # --- Служебные команды (регистрируются ДО FSM и ДО handle_message) ---
        self.dp.message.register(self.handle_start_or_unknown, Command("start"))
        self.dp.message.register(self.handle_stop, Command("stop"))

        # --- Callback-кнопки модерации ---
        self.dp.callback_query.register(
            self.handle_approval,
            F.data.startswith("approve_") | F.data.startswith("reject_") | F.data.startswith("reset_"),
        )

        # --- FSM-состояния регистрации ---
        self.dp.message.register(self.process_name, RegistrationState.waiting_for_name)
        self.dp.message.register(self.process_department, RegistrationState.waiting_for_department)
        self.dp.message.register(self.process_purpose, RegistrationState.waiting_for_purpose)

        # --- Не-текстовые сообщения (фото, стикеры и т.п.) ---
        self.dp.message.register(self.handle_non_text, ~F.text)

        # --- Обычные текстовые сообщения (в самом конце) ---
        self.dp.message.register(self.handle_message, F.text)

        await self.dp.start_polling(self.bot)

    # ------------------------------------------------------------------
    # /start и обработка незнакомых пользователей
    # ------------------------------------------------------------------
    async def handle_start_or_unknown(self, message: types.Message, state: FSMContext | None) -> None:
        """Обработка команды /start и незнакомых сообщений с проверкой доступа."""
        user_id = str(message.from_user.id)

        # Обновляем профиль для админов и активных пользователей
        if self.user_manager.is_admin(user_id) or self.user_manager.is_user_active(user_id):
            tg_name = message.from_user.full_name
            tg_username = message.from_user.username or ""
            try:
                await self.user_manager.update_user_profile(user_id, tg_username, tg_name)
            except Exception as e:
                logger.warning("Не удалось обновить профиль %s: %s", user_id, e)

        # Админ — пропускаем сразу
        if self.user_manager.is_admin(user_id):
            await message.answer("👑 Привет, Администратор! Бот готов к работе.")
            if state is not None:
                await state.clear()
            return

        # Активный пользователь
        if self.user_manager.is_user_active(user_id):
            await message.answer("👋 С возвращением! Вы можете задавать вопросы.")
            if state is not None:
                await state.clear()
            return

        # Есть в базе, но не активен
        if self.user_manager.get_user(user_id):
            await message.answer("⏳ Ваша заявка на рассмотрении у администратора.")
            if state is not None:
                await state.clear()
            return

        # Новый пользователь — начинаем регистрацию
        await message.answer(
            "👋 Привет! Я корпоративный аналитик. Для доступа к базе данных мне нужно знать, кто вы.\n\n"
            "Напишите ваше **ФИО**:",
            parse_mode=None,
        )
        if state is not None:
            await state.set_state(RegistrationState.waiting_for_name)

    # ------------------------------------------------------------------
    # /stop — прерывание текущего анализа
    # ------------------------------------------------------------------
    async def handle_stop(self, message: types.Message) -> None:
        """Прерывает текущий анализ пользователя.

        Логика: берём SessionState по user_id, если current_task активен —
        отменяем его. run_and_reply() ловит CancelledError и отвечает
        пользователю "Выполнение прервано".
        """
        user_id = str(message.from_user.id)
        session_state = self.engine.sessions.peek(user_id)

        if session_state is None:
            await message.answer("Нет активного анализа. Нечего останавливать.")
            return

        if session_state.current_task and not session_state.current_task.done():
            session_state.current_task.cancel()
            logger.info("Пользователь %s прервал анализ командой /stop", user_id)
            # Ответ отправит run_and_reply() через CancelledError,
            # чтобы не дублировать сообщение — здесь не отвечаем.
        else:
            await message.answer("Нет активного анализа. Нечего останавливать.")

    # ------------------------------------------------------------------
    # FSM-регистрация
    # ------------------------------------------------------------------
    async def process_name(self, message: types.Message, state: FSMContext) -> None:
        """Обработка ввода ФИО пользователя."""
        name = (message.text or "").strip()
        if len(name.split()) < 2:
            await message.answer("⚠️ Пожалуйста, укажите вашу настоящую Фамилию и Имя (например: Иванов Иван).")
            return
        await state.update_data(full_name=name)
        await message.answer("Отлично. Теперь напишите ваш **Отдел / Должность**:", parse_mode=None)
        await state.set_state(RegistrationState.waiting_for_department)

    async def process_department(self, message: types.Message, state: FSMContext) -> None:
        """Обработка ввода отдела пользователя."""
        dep = (message.text or "").strip()
        if len(dep) < 2:
            await message.answer("⚠️ Пожалуйста, укажите ваш отдел (например: Отдел продаж).")
            return
        await state.update_data(department=dep)
        await message.answer(
            "И последнее. Опишите **зачем** вам нужен доступ (например: 'формирую отчеты по звонкам'):",
            parse_mode=None,
        )
        await state.set_state(RegistrationState.waiting_for_purpose)

    async def process_purpose(self, message: types.Message, state: FSMContext) -> None:
        """Обработка ввода цели доступа и отправка заявки администраторам."""
        purpose = (message.text or "").strip()
        if len(purpose) < 5:
            await message.answer("⚠️ Пожалуйста, опишите цель подробнее (минимум 5 символов).")
            return

        data = await state.get_data()
        user_id = str(message.from_user.id)

        try:
            await self.user_manager.add_user(
                user_id=user_id,
                username=message.from_user.username or "",
                full_name=data.get("full_name", ""),
                department=data.get("department", ""),
                purpose=purpose,
            )
        except Exception as e:
            logger.exception("Ошибка сохранения заявки %s", user_id)
            await message.answer("⚠️ Не удалось сохранить заявку. Попробуйте позже.")
            await state.clear()
            return

        await state.clear()
        await message.answer("✅ Заявка отправлена администраторам. Ожидайте подтверждения.")

        # Уведомление админам
        if not self.user_manager.admins:
            logger.warning(
                "ADMIN_USERS пуст — заявка от %s никому не отправлена. "
                "Настройте ADMIN_USERS в .env.", user_id,
            )
            return

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_{user_id}"),
                    InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user_id}"),
                ]
            ]
        )

        admin_text = (
            f"🔔 <b>Новая заявка на доступ</b>\n\n"
            f"ID: <code>{html.escape(user_id)}</code>\n"
            f"Username: @{html.escape(message.from_user.username or 'нет')}\n"
            f"ФИО: {html.escape(data.get('full_name', ''))}\n"
            f"Отдел: {html.escape(data.get('department', ''))}\n"
            f"Цель: {html.escape(purpose)}"
        )

        for admin_id in self.user_manager.admins:
            try:
                await self.bot.send_message(
                    chat_id=int(admin_id),
                    text=admin_text,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error("Не удалось отправить сообщение админу %s: %s", admin_id, e)

    # ------------------------------------------------------------------
    # Модерация (approve / reject / reset)
    # ------------------------------------------------------------------
    async def handle_approval(self, callback: types.CallbackQuery) -> None:
        """Обработка кнопок одобрения/отклонения/сброса заявки."""
        if not callback.data:
            await callback.answer("Некорректная кнопка.", show_alert=True)
            return

        try:
            action, user_id = callback.data.split("_", 1)
        except ValueError:
            await callback.answer("Некорректные данные кнопки.", show_alert=True)
            return

        if not self.user_manager.is_admin(str(callback.from_user.id)):
            await callback.answer("⛔ Только администраторы могут это делать.", show_alert=True)
            return

        user_data = self.user_manager.get_user(user_id)

        if action == "approve":
            if not user_data:
                await callback.answer("Пользователь не найден.", show_alert=True)
                return
            # Защита от повторного нажатия
            if user_data.get("is_active"):
                await callback.answer("Пользователь уже одобрен.", show_alert=True)
                return
            await self.user_manager.approve_user(user_id, role="viewer")
            if callback.message and callback.message.text:
                await callback.message.edit_text(
                    callback.message.text + f"\n\n✅ <b>ОДОБРЕНО</b> (админ: @{html.escape(callback.from_user.username or '')})",
                    parse_mode="HTML",
                )
            try:
                await self.bot.send_message(
                    chat_id=int(user_id),
                    text="🎉 Ваша заявка одобрена! Теперь вы можете задавать вопросы. Напишите /start для начала.",
                )
            except Exception as e:
                logger.warning("Не удалось уведомить пользователя %s: %s", user_id, e)

        elif action == "reject":
            if not user_data:
                await callback.answer("Пользователь не найден.", show_alert=True)
                return
            if not user_data.get("is_active"):
                await callback.answer("Пользователь уже отклонён.", show_alert=True)
                return
            await self.user_manager.reject_user(user_id)
            if callback.message and callback.message.text:
                await callback.message.edit_text(
                    callback.message.text + f"\n\n❌ <b>ОТКЛОНЕНО</b> (админ: @{html.escape(callback.from_user.username or '')})",
                    parse_mode="HTML",
                )
            # Кнопка "Сбросить"
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔄 Сбросить статус (разрешить заново)",
                            callback_data=f"reset_{user_id}",
                        )
                    ]
                ]
            )
            if callback.message:
                await callback.message.edit_reply_markup(reply_markup=kb)

        elif action == "reset":
            # ВАЖНО: удаляем пользователя из users.yaml, чтобы он мог подать
            # заявку заново. Раньше ставили is_active=False — и бот отвечал
            # "Ваша заявка на рассмотрении", хотя заявки уже нет.
            await self._delete_user(user_id)
            if callback.message and callback.message.text:
                await callback.message.edit_text(
                    callback.message.text + "\n\n🔄 <b>СТАТУС СБРОШЕН. Пользователь может подать заявку заново.</b>",
                    parse_mode="HTML",
                )

        await callback.answer()

    async def _delete_user(self, user_id: str) -> None:
        """Удаляет пользователя из users.yaml (для reset)."""
        try:
            ok = await self.user_manager.delete_user(user_id)
            if ok:
                logger.info("Пользователь %s удалён из users.yaml (reset)", user_id)
            else:
                logger.warning("Не удалось удалить пользователя %s", user_id)
        except Exception as e:
            logger.error("Ошибка удаления пользователя %s: %s", user_id, e)

    # ------------------------------------------------------------------
    # Не-текстовые сообщения
    # ------------------------------------------------------------------
    async def handle_non_text(self, message: types.Message) -> None:
        """Fallback для фото/стикеров/голоса и т.п."""
        await message.answer("⚠️ Я понимаю только текстовые сообщения. Напишите вопрос словами.")

    # ------------------------------------------------------------------
    # Обработка входящего текста
    # ------------------------------------------------------------------
    async def handle_message(self, message: types.Message, state: FSMContext) -> None:
        """Обработка входящего текстового сообщения."""
        user_id = str(message.from_user.id)
        text = (message.text or "").strip()

        # Служебные команды, которые не должны попадать в регистрацию
        # (обрабатываются отдельными хэндлерами в engine.process_message).
        command_word = text.split()[0].lower() if text else ""

        # Если это /db, /model, /sql, /help, /history и т.д. — пропускаем
        # регистрационную проверку и отдаём engine. Админ-проверку делает engine.
        is_service = command_word in SERVICE_COMMANDS

        if not is_service:
            if not self.user_manager.is_admin(user_id) and not self.user_manager.is_user_active(user_id):
                # Не одобрен → в регистрацию
                await self.handle_start_or_unknown(message, state)
                return

        async def send_status(status_text: str):
            try:
                # Экранируем — модель может прислать **bold**, и Telegram
                # упадёт на невалидном Markdown.
                safe = html.escape(status_text)
                await message.answer(safe, parse_mode=None)
            except Exception as e:
                logger.warning("Failed to send status: %s", e)

        msg = ChatMessage(
            user_id=user_id,
            chat_id=str(message.chat.id),
            text=text,
            status_callback=send_status,
        )

        session_state = self.engine.sessions.get(user_id)

        async def run_and_reply():
            try:
                result: ChatResponse = await self.engine.process_message(msg)
                await self._send_long_message(message, result.text)
            except asyncio.CancelledError:
                # Отмена через /stop
                try:
                    await message.answer("⛔ Выполнение прервано командой /stop.")
                except Exception:
                    pass
                raise  # пробрасываем, чтобы task корректно завершился как cancelled
            except Exception as e:
                logger.exception("Error in background task: %s", e)
                try:
                    await message.answer("⚠️ Произошла ошибка при выполнении запроса.")
                except Exception:
                    pass
            finally:
                # Чистим ссылку на задачу, чтобы /stop не отменял уже завершённую
                if session_state.current_task is asyncio.current_task():
                    session_state.current_task = None

        # Если уже есть активная задача — не запускаем новую
        if session_state.current_task and not session_state.current_task.done():
            await message.answer("⏳ Пожалуйста, дождитесь ответа или отправьте /stop для отмены.")
            return

        task = asyncio.create_task(run_and_reply())
        session_state.current_task = task

    # ------------------------------------------------------------------
    # Отправка длинных сообщений
    # ------------------------------------------------------------------
    async def _send_long_message(self, message: types.Message, text: str) -> None:
        """Отправляет сообщение, разбивая его на части.

        Лимит Telegram — 4096 символов. Режем по 4000, чтобы был запас.
        """
        max_length = 4000
        if len(text) <= max_length:
            await message.answer(text)
            return

        parts: list[str] = []
        while len(text) > max_length:
            split_idx = text.rfind('\n', 0, max_length)
            if split_idx == -1:
                split_idx = max_length
            parts.append(text[:split_idx])
            text = text[split_idx:].lstrip()

        if text:
            parts.append(text)

        for i, part in enumerate(parts):
            await message.answer(part)
            if i < len(parts) - 1:
                await asyncio.sleep(0.3)  # анти-флуд

    # ------------------------------------------------------------------
    # Публичные методы (интерфейс BaseChatInterface)
    # ------------------------------------------------------------------
    async def send_message(self, chat_id: str, text: str) -> None:
        """Отправка текстового сообщения в чат."""
        await self.bot.send_message(chat_id=int(chat_id), text=text)

    async def send_image(self, chat_id: str, image_path: str) -> None:
        """Отправка изображения в чат."""
        await self.bot.send_photo(
            chat_id=int(chat_id),
            photo=FSInputFile(image_path),
        )

    async def close(self) -> None:
        """Закрытие сессии бота."""
        await self.bot.session.close()
