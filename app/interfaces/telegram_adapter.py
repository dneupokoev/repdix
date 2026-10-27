# app/interfaces/telegram_adapter.py
dv_file_version = '261027.03'
"""Telegram-адаптер на aiogram 3."""
import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton

from app.core.engine import RepdixEngine
from app.interfaces.base import BaseChatInterface, ChatMessage, ChatResponse


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

    async def start(self) -> None:
        """Регистрация хэндлеров и запуск polling."""
        self.dp.message.register(self.handle_start_or_unknown, Command("start"))
        self.dp.callback_query.register(
            self.handle_approval,
            F.data.startswith("approve_") | F.data.startswith("reject_") | F.data.startswith("reset_"),
        )

        # FSM состояния
        self.dp.message.register(self.process_name, RegistrationState.waiting_for_name)
        self.dp.message.register(self.process_department, RegistrationState.waiting_for_department)
        self.dp.message.register(self.process_purpose, RegistrationState.waiting_for_purpose)

        self.dp.message.register(self.handle_message, F.text)
        await self.dp.start_polling(self.bot)

    async def handle_start_or_unknown(self, message: types.Message, state: FSMContext) -> None:
        """Обработка команды /start и незнакомых сообщений с проверкой доступа."""
        user_id = str(message.from_user.id)
        # user_id = "888888888"  # ВРЕМЕННЫЙ ID ДЛЯ ТЕСТА

        # Если админ — пропускаем сразу
        if self.user_manager.is_admin(user_id):
            await message.answer("👑 Привет, Администратор! Бот готов к работе.")
            await state.clear()
            return

        user = await self.user_manager.get_user(user_id)
        status = user["status"] if user else "none"

        if status == "approved":
            await message.answer("👋 С возвращением! Вы можете задавать вопросы.")
        elif status == "pending":
            await message.answer("⏳ Ваша заявка на рассмотрении у администратора.")
        elif status == "rejected":
            await message.answer("⛔ Вам отказано в доступе. Обратитесь к администратору для сброса статуса.")
        else:
            await message.answer(
                "👋 Привет! Я корпоративный аналитик. Для доступа к базе данных мне нужно знать, кто вы.\n\n"
                "Напишите ваше **ФИО**:"
            )
            await state.set_state(RegistrationState.waiting_for_name)

    async def process_name(self, message: types.Message, state: FSMContext) -> None:
        """Обработка ввода ФИО пользователя."""
        name = message.text.strip()
        if len(name.split()) < 2:
            await message.answer("⚠️ Пожалуйста, укажите вашу настоящую Фамилию и Имя (например: Иванов Иван).")
            return
        await state.update_data(full_name=name)
        await message.answer("Отлично. Теперь напишите ваш **Отдел / Должность**:")
        await state.set_state(RegistrationState.waiting_for_department)

    async def process_department(self, message: types.Message, state: FSMContext) -> None:
        """Обработка ввода отдела пользователя."""
        dep = message.text.strip()
        if len(dep) < 2:
            await message.answer("⚠️ Пожалуйста, укажите ваш отдел (например: Отдел продаж).")
            return
        await state.update_data(department=dep)
        await message.answer("И последнее. Опишите **зачем** вам нужен доступ (например: 'формирую отчеты по звонкам'):")
        await state.set_state(RegistrationState.waiting_for_purpose)

    async def process_purpose(self, message: types.Message, state: FSMContext) -> None:
        """Обработка ввода цели доступа и отправка заявки администраторам."""
        purpose = message.text.strip()
        if len(purpose) < 5:
            await message.answer("⚠️ Пожалуйста, опишите цель подробнее (минимум 5 символов).")
            return

        data = await state.get_data()
        user_id = str(message.from_user.id)

        await self.user_manager.add_user(
            user_id=user_id,
            username=message.from_user.username or "",
            full_name=data.get("full_name"),
            department=data.get("department"),
            purpose=purpose,
        )

        await state.clear()
        await message.answer("✅ Заявка отправлена администраторам. Ожидайте подтверждения.")

        # Отправляем уведомление админам
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_{user_id}"),
                    InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user_id}"),
                ]
            ]
        )

        admin_text = (
            f"🔔 **Новая заявка на доступ**\n\n"
            f"ID: `{user_id}`\n"
            f"Username: @{message.from_user.username or 'нет'}\n"
            f"ФИО: {data.get('full_name')}\n"
            f"Отдел: {data.get('department')}\n"
            f"Цель: {purpose}"
        )

        for admin_id in self.user_manager.admins:
            try:
                await self.bot.send_message(
                    chat_id=int(admin_id),
                    text=admin_text,
                    reply_markup=kb,
                    parse_mode="Markdown",
                )
            except Exception as e:
                logging.error(f"Не удалось отправить сообщение админу {admin_id}: {e}")

    async def handle_approval(self, callback: types.CallbackQuery) -> None:
        """Обработка кнопок одобрения/отклонения/сброса заявки."""
        action, user_id = callback.data.split("_", 1)

        if not self.user_manager.is_admin(str(callback.from_user.id)):
            await callback.answer("⛔ Только администраторы могут это делать.", show_alert=True)
            return

        if action == "approve":
            success = await self.user_manager.update_status(user_id, "approved")
            if success:
                await callback.message.edit_text(
                    callback.message.text + f"\n\n✅ **ОДОБРЕНО** (админ: @{callback.from_user.username})"
                )
                try:
                    await self.bot.send_message(
                        chat_id=int(user_id),
                        text="🎉 Ваша заявка одобрена! Теперь вы можете задавать вопросы. Напишите /start для начала.",
                    )
                except Exception:
                    pass
            else:
                await callback.answer("Эта заявка уже была обработана.", show_alert=True)

        elif action == "reject":
            success = await self.user_manager.update_status(user_id, "rejected")
            if success:
                await callback.message.edit_text(
                    callback.message.text + f"\n\n❌ **ОТКЛОНЕНО** (админ: @{callback.from_user.username})"
                )
                # Добавляем кнопку "Сбросить"
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
                await callback.message.edit_reply_markup(reply_markup=kb)
            else:
                await callback.answer("Эта заявка уже была обработана.", show_alert=True)

        elif action == "reset":
            await self.user_manager.reset_user(user_id)
            await callback.message.edit_text(
                callback.message.text + "\n\n🔄 **СТАТУС СБРОШЕН. Пользователь может подать заявку заново.**"
            )
            await callback.answer("Статус сброшен.")

        await callback.answer()

    async def handle_message(self, message: types.Message) -> None:
        """Обработка входящего текстового сообщения."""
        user_id = str(message.from_user.id)
        if not self.user_manager.is_admin(user_id):
            status = await self.user_manager.get_user_status(user_id)
            if status != "approved":
                # Если не одобрен, отправляем в обработчик регистрации
                await self.handle_start_or_unknown(message, None)
                return

        async def send_status(text: str):
            try:
                await message.answer(text)
            except Exception as e:
                logging.getLogger(__name__).warning("Failed to send status: %s", e)

        msg = ChatMessage(
            user_id=str(message.from_user.id),
            chat_id=str(message.chat.id),
            text=message.text or "",
            status_callback=send_status
        )

        # Получаем состояние пользователя, чтобы сохранить задачу
        state = self.engine.sessions.get(str(message.from_user.id))

        async def run_and_reply():
            try:
                result: ChatResponse = await self.engine.process_message(msg)
                await self._send_long_message(message, result.text)
            except asyncio.CancelledError:
                await message.answer("⛔ Выполнение прервано командой /stop.")
            except Exception as e:
                logging.getLogger(__name__).error("Error in background task: %s", e)
                await message.answer("⚠️ Произошла ошибка при выполнении запроса.")

        # Если уже есть активная задача — не запускаем новую
        if state.current_task and not state.current_task.done():
            await message.answer("⏳ Пожалуйста, дождитесь ответа или отправьте /stop для отмены.")
            return

        # Запускаем задачу в фоне и сохраняем ссылку
        task = asyncio.create_task(run_and_reply())
        state.current_task = task

    async def _send_long_message(self, message: types.Message, text: str) -> None:
        """Отправляет сообщение, разбивая его на части, если оно длиннее 4096 символов."""
        max_length = 4000
        if len(text) <= max_length:
            await message.answer(text)
            return

        # Разбиваем по 4000 символов, стараясь резать по переносам строк
        parts = []
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
                await asyncio.sleep(0.5) # Небольшая пауза, чтобы не получить Flood Control

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
