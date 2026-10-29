# app/interfaces/telegram_adapter.py
dv_file_version = '261029.02'
"""Telegram-адаптер на aiogram 3.

Ключевые изменения версии 261029.02:
- Статусы (send_status внутри handle_message) отправляются с
  parse_mode="HTML": прежний html.escape() превращал <b> модели в
  &lt;b&gt;, и разметка показывалась текстом. html.escape убран — статус
  формирует LLM, а не пользователь. При ошибке разметки (незакрытый тег,
  одиночный '<') статус дублируется без parse_mode — бот не падает.

Ключевые изменения версии 261029.01:
- Исходящие ответы движка отправляются с parse_mode="HTML":
  _send_long_message (через новый хелпер _answer_html) и send_message.
  Модель по промпту отдаёт <b>/<code>, а не Markdown.
- При ошибке разметки (TelegramBadRequest: одиночный '<', разорванный
  при нарезке тег и т.п.) сообщение дублируется без parse_mode — пользователь
  видит текст, а не сообщение об ошибке в логе.

Ключевые изменения версии 261028.07:
- /users теперь доступна ТОЛЬКО env-админам (из ADMIN_USERS):
    * Меню команд: базовый набор — для всех, расширенный (с /users) —
      только для env-админов через BotCommandScopeChat.
    * Регистрация Command("users") с фильтром F.from_user.id.in_(env_admin_ids):
      для не-env-админов хэндлер НЕ вызывается.
    * Fallback-хэндлер handle_users_forbidden ловит /users от не-админов
      и отвечает «⛔ Команда доступна только администраторам из .env».
- Добавлен геттер _env_admin_ids(): возвращает set[int] из ADMIN_USERS.
- Метод _refresh_admin_commands() — переустанавливает per-chat меню для
  всех env-админов при старте. Если ADMIN_USERS изменится — достаточно
  перезапустить бота (или вызвать метод).
- handle_admin_callback теперь тоже защищён: только is_env_admin может
  нажимать кнопки adm_*. Если пользователь был admin в YAML (роль), но
  не в ADMIN_USERS — кнопки не работают (защита от того, что кто-то
  отредактирует users.yaml и получит доступ к админке).
- handle_approval (модерация заявок) по-прежнему доступен всем админам
  (env + YAML с ролью admin). Это by design: модерация — не «админка
  пользователей», а workflow одобрения новых заявок.
- handle_users_command дополнительно проверяет is_env_admin.
"""

import asyncio
import html
import logging
import os

import yaml
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from app.core.engine import RepdixEngine
from app.interfaces.base import BaseChatInterface, ChatMessage, ChatResponse

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Константы
# ----------------------------------------------------------------------
# Служебные команды, которые НЕ должны перехватываться регистрацией.
# ВНИМАНИЕ: /users сюда НЕ включён — он обрабатывается отдельным
# хэндлером с фильтром по env-админам, а для остальных ловится
# handle_users_forbidden (тоже отдельным хэндлером).
SERVICE_COMMANDS = {
    "/start", "/help", "/stop", "/history", "/compact",
    "/tokens", "/clear", "/settings", "/db", "/model",
    "/models", "/sql",
}

# Сколько ролей показывать на одной странице (3 ряда по 2)
ROLES_PER_PAGE = 6

# Сколько пользователей на странице в /users
USERS_PER_PAGE = 10

# Показывать ли роль "admin" в списке выбора.
SHOW_ADMIN_ROLE = False

# Максимальная длина описания роли в тексте сообщения
ROLE_DESC_MAX_LEN = 200


# ----------------------------------------------------------------------
# FSM-состояния
# ----------------------------------------------------------------------
class RegistrationState(StatesGroup):
    """Состояния FSM для процесса регистрации пользователя."""

    waiting_for_name = State()
    waiting_for_department = State()
    waiting_for_purpose = State()


class ApprovalState(StatesGroup):
    """Состояние выбора ролей при одобрении заявки."""

    choosing_roles = State()


class AdminState(StatesGroup):
    """Состояния FSM для админ-панели /users."""

    browsing_users = State()
    viewing_user = State()
    editing_roles = State()


class TelegramAdapter(BaseChatInterface):
    """Адаптер Telegram на базе aiogram 3."""

    def __init__(self, engine: RepdixEngine) -> None:
        self.engine = engine
        self.bot = Bot(token=os.environ["TELEGRAM_TOKEN"])
        self.dp = Dispatcher()
        self.user_manager = engine.user_manager

    # ------------------------------------------------------------------
    # Env-админы (для /users и меню)
    # ------------------------------------------------------------------
    def _env_admin_ids(self) -> set[int]:
        """Возвращает set[int] ID админов из ADMIN_USERS.

        Невалидные (не цифры) ID пропускаются с предупреждением.
        """
        result: set[int] = set()
        for a in self.user_manager.admins:
            try:
                result.add(int(str(a).strip()))
            except (TypeError, ValueError):
                logger.warning("ADMIN_USERS: невалидный ID %r, пропущен", a)
        return result

    async def _refresh_admin_commands(self) -> None:
        """Устанавливает меню команд: базовое — всем, расширенное — env-админам."""
        public_commands = [
            BotCommand(command="start", description="Запустить бота / Регистрация"),
            BotCommand(command="help", description="Справка по всем командам"),
            BotCommand(command="stop", description="Остановить текущий анализ"),
            BotCommand(command="history", description="Показать историю диалога"),
            BotCommand(command="compact", description="Сжать историю (очистить контекст)"),
            BotCommand(command="tokens", description="Показать расход токенов контекста"),
            BotCommand(command="clear", description="Полностью очистить историю"),
            BotCommand(command="settings", description="Текущие настройки (БД, модель, лимиты)"),
        ]
        admin_commands = public_commands + [
            BotCommand(command="users", description="Управление пользователями"),
        ]

        # 1. Базовый набор для всех
        try:
            await self.bot.set_my_commands(public_commands)
        except Exception as e:
            logger.error("Не удалось установить базовое меню команд: %s", e)

        # 2. Расширенный набор — только для env-админов
        for admin_id in self._env_admin_ids():
            try:
                await self.bot.set_my_commands(
                    admin_commands,
                    scope=BotCommandScopeChat(chat_id=admin_id),
                )
            except Exception as e:
                # Telegram возвращает ошибку, если бот не может установить
                # команды для конкретного чата (например, пользователь
                # ещё не начинал диалог с ботом).
                logger.warning(
                    "Не удалось установить admin-меню для %s: %s", admin_id, e,
                )

    # ------------------------------------------------------------------
    # Запуск
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Регистрация хэндлеров и запуск polling."""

        # Меню команд: базовое всем, расширенное env-админам
        await self._refresh_admin_commands()

        # --- Служебные команды ---
        self.dp.message.register(self.handle_start_or_unknown, Command("start"))
        self.dp.message.register(self.handle_stop, Command("stop"))

        # --- /users: только env-админы ---
        env_admin_ids = self._env_admin_ids()
        self.dp.message.register(
            self.handle_users_command,
            Command("users"),
            F.from_user.id.in_(env_admin_ids) if env_admin_ids else F.from_user.id.in_(set()),
        )
        # Fallback: все остальные (не env-админы) при попытке /users
        self.dp.message.register(
            self.handle_users_forbidden,
            Command("users"),
        )

        # --- Callback: модерация (approve / reject / reset) ---
        self.dp.callback_query.register(
            self.handle_approval,
            F.data.startswith("approve_")
            | F.data.startswith("reject_")
            | F.data.startswith("reset_"),
        )

        # --- Callback: выбор ролей при одобрении ---
        self.dp.callback_query.register(
            self.handle_role_choice,
            ApprovalState.choosing_roles,
            F.data.startswith("arole_"),
        )
        self.dp.callback_query.register(
            self.handle_role_stale,
            F.data.startswith("arole_"),
        )

        # --- Callback: админ-панель ---
        self.dp.callback_query.register(
            self.handle_admin_callback,
            F.data.startswith("adm_"),
        )

        # --- FSM-регистрация ---
        self.dp.message.register(self.process_name, RegistrationState.waiting_for_name)
        self.dp.message.register(self.process_department, RegistrationState.waiting_for_department)
        self.dp.message.register(self.process_purpose, RegistrationState.waiting_for_purpose)

        # --- Не-текстовые сообщения ---
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

        if self.user_manager.is_admin(user_id) or self.user_manager.is_user_active(user_id):
            tg_name = message.from_user.full_name
            tg_username = message.from_user.username or ""
            try:
                await self.user_manager.update_user_profile(user_id, tg_username, tg_name)
            except Exception as e:
                logger.warning("Не удалось обновить профиль %s: %s", user_id, e)

        if self.user_manager.is_admin(user_id):
            await message.answer("👑 Привет, Администратор! AI-аналитик готов к работе.")
            if state is not None:
                await state.clear()
            return

        if self.user_manager.is_user_active(user_id):
            await message.answer("👋 С возвращением! Вы можете задавать вопросы.")
            if state is not None:
                await state.clear()
            return

        if self.user_manager.get_user(user_id):
            await message.answer("⏳ Ваша заявка на рассмотрении у администратора.")
            if state is not None:
                await state.clear()
            return

        await message.answer(
            "👋 Привет! Я AI-аналитик. Для доступа к базе данных мне нужно знать, кто вы.\n\n"
            "Напишите ваше **ФИО**:",
            parse_mode=None,
        )
        if state is not None:
            await state.set_state(RegistrationState.waiting_for_name)

    # ------------------------------------------------------------------
    # /stop
    # ------------------------------------------------------------------
    async def handle_stop(self, message: types.Message) -> None:
        user_id = str(message.from_user.id)
        session_state = self.engine.sessions.peek(user_id)

        if session_state is None:
            await message.answer("Нет активного анализа. Нечего останавливать.")
            return

        if session_state.current_task and not session_state.current_task.done():
            session_state.current_task.cancel()
            logger.info("Пользователь %s прервал анализ командой /stop", user_id)
        else:
            await message.answer("Нет активного анализа. Нечего останавливать.")

    # ------------------------------------------------------------------
    # FSM-регистрация
    # ------------------------------------------------------------------
    async def process_name(self, message: types.Message, state: FSMContext) -> None:
        name = (message.text or "").strip()
        if len(name.split()) < 2:
            await message.answer("⚠️ Пожалуйста, укажите вашу настоящую Фамилию и Имя (например: Иванов Иван).")
            return
        await state.update_data(full_name=name)
        await message.answer("Отлично. Теперь напишите ваш **Отдел / Должность**:", parse_mode=None)
        await state.set_state(RegistrationState.waiting_for_department)

    async def process_department(self, message: types.Message, state: FSMContext) -> None:
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
    # Загрузка ролей из config/roles.yaml
    # ------------------------------------------------------------------
    def _load_roles(self) -> list[dict]:
        """Загружает роли из config/roles.yaml."""
        try:
            with open("config/roles.yaml", "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            roles_dict = data.get("roles", {}) or {}
        except FileNotFoundError:
            logger.warning("config/roles.yaml не найден")
            return []
        except yaml.YAMLError as e:
            logger.error("Ошибка парсинга roles.yaml: %s", e)
            return []
        except Exception as e:
            logger.error("Не удалось загрузить roles.yaml: %s", e)
            return []

        result: list[dict] = []
        for name, conf in roles_dict.items():
            if not isinstance(conf, dict):
                continue
            name_str = str(name).strip()
            if not name_str:
                continue
            if name_str == "admin" and not SHOW_ADMIN_ROLE:
                continue
            desc = str(conf.get("description", "") or "").strip()
            result.append({"name": name_str, "description": desc})

        priority = {"viewer": 0}
        result.sort(key=lambda r: (priority.get(r["name"], 10), r["name"].lower()))
        return result

    # ------------------------------------------------------------------
    # Универсальная клавиатура ролей (одобрение + админка)
    # ------------------------------------------------------------------
    def _build_roles_keyboard(
        self,
        roles: list[dict],
        selected: set[int],
        page: int,
        total_pages: int,
        prefix: str = "arole_",
        done_label: str = "💾 Применить",
        cancel_label: str = "🚫 Отмена",
    ) -> InlineKeyboardMarkup:
        """Строит клавиатуру с чек-боксами для выбора ролей."""
        rows: list[list[InlineKeyboardButton]] = []

        start = page * ROLES_PER_PAGE
        end = min(start + ROLES_PER_PAGE, len(roles))

        current_row: list[InlineKeyboardButton] = []
        for idx in range(start, end):
            role = roles[idx]
            check = "☑" if idx in selected else "☐"
            label = role["name"]
            if len(label) > 30:
                label = label[:27] + "..."
            current_row.append(InlineKeyboardButton(
                text=f"{check} {label}",
                callback_data=f"{prefix}toggle_{idx}",
            ))
            if len(current_row) == 2:
                rows.append(current_row)
                current_row = []
        if current_row:
            rows.append(current_row)

        if total_pages > 1:
            nav: list[InlineKeyboardButton] = []
            if page > 0:
                nav.append(InlineKeyboardButton(
                    text="◀", callback_data=f"{prefix}page_{page - 1}",
                ))
            nav.append(InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}", callback_data=f"{prefix}noop",
            ))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton(
                    text="▶", callback_data=f"{prefix}page_{page + 1}",
                ))
            rows.append(nav)

        rows.append([
            InlineKeyboardButton(text="✅ Все на стр.", callback_data=f"{prefix}all"),
            InlineKeyboardButton(text="❌ Снять на стр.", callback_data=f"{prefix}none"),
        ])

        rows.append([
            InlineKeyboardButton(text=done_label, callback_data=f"{prefix}done"),
            InlineKeyboardButton(text=cancel_label, callback_data=f"{prefix}cancel"),
        ])

        return InlineKeyboardMarkup(inline_keyboard=rows)

    # ------------------------------------------------------------------
    # Универсальный текст экрана ролей
    # ------------------------------------------------------------------
    def _render_roles_message(
        self,
        user_data: dict,
        roles: list[dict],
        selected: set[int],
        page: int,
        total_pages: int,
        title: str = "🔔 <b>Одобрение заявки</b>",
        show_purpose: bool = True,
        show_status: bool = False,
    ) -> str:
        """Формирует текст экрана выбора ролей."""
        uid = html.escape(str(user_data.get("id", "—")))
        full_name = html.escape(str(user_data.get("full_name", "—")))
        department = html.escape(str(user_data.get("department", "—")))

        lines = [title + "\n", f"ID: <code>{uid}</code>", f"ФИО: {full_name}"]

        if show_purpose:
            purpose = html.escape(str(user_data.get("purpose", "—"))[:300])
            lines.append(f"Отдел: {department}")
            lines.append(f"Цель: {purpose}\n")
        else:
            lines.append(f"Отдел: {department}")

        if show_status:
            is_active = bool(user_data.get("is_active"))
            status = "✅ активен" if is_active else "⏸ заблокирован"
            lines.append(f"Статус: {status}")

        lines.append("")

        if not roles:
            lines.append("⚠️ В <code>config/roles.yaml</code> нет ролей.")
            return "\n".join(lines)

        start = page * ROLES_PER_PAGE
        end = min(start + ROLES_PER_PAGE, len(roles))

        lines.append(f"<b>Роли (стр. {page + 1}/{total_pages}):</b>")
        for idx in range(start, end):
            role = roles[idx]
            check = "☑" if idx in selected else "☐"
            name = html.escape(role["name"])
            desc_raw = role.get("description", "")
            desc = html.escape(desc_raw[:ROLE_DESC_MAX_LEN])
            if len(desc_raw) > ROLE_DESC_MAX_LEN:
                desc += "…"
            if desc:
                lines.append(f"{check} <b>{name}</b> — {desc}")
            else:
                lines.append(f"{check} <b>{name}</b>")

        lines.append(f"\n<b>Выбрано: {len(selected)}</b>")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Модерация (approve / reject / reset) — доступна всем админам
    # ------------------------------------------------------------------
    async def handle_approval(self, callback: types.CallbackQuery, state: FSMContext) -> None:
        """Обработка кнопок одобрения/отклонения/сброса заявки.

        Доступно всем is_admin (env + YAML role=admin): модерация заявок —
        не то же самое, что админ-панель /users.
        """
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
            if user_data.get("is_active"):
                await callback.answer("Пользователь уже одобрен.", show_alert=True)
                return

            roles = self._load_roles()
            if not roles:
                await callback.answer(
                    "В config/roles.yaml нет ролей. Добавьте роли и попробуйте снова.",
                    show_alert=True,
                )
                return

            selected: set[int] = {0}
            total_pages = max(1, (len(roles) + ROLES_PER_PAGE - 1) // ROLES_PER_PAGE)

            await state.set_state(ApprovalState.choosing_roles)
            await state.update_data(
                target_user_id=user_id,
                roles=roles,
                selected=selected,
                page=0,
                total_pages=total_pages,
                user_data=user_data,
            )

            text = self._render_roles_message(user_data, roles, selected, 0, total_pages)
            kb = self._build_roles_keyboard(roles, selected, 0, total_pages, prefix="arole_")

            if callback.message:
                try:
                    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
                except Exception as e:
                    logger.debug("edit_text не удался (%s), отправляю новое", e)
                    try:
                        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
                    except Exception as e2:
                        logger.error("Не удалось отправить экран выбора ролей: %s", e2)
                        await callback.answer("Ошибка отображения. Попробуйте /start.", show_alert=True)
                        return

            await callback.answer()
            return

        elif action == "reject":
            if not user_data:
                await callback.answer("Пользователь не найден.", show_alert=True)
                return
            if not user_data.get("is_active"):
                await callback.answer("Пользователь уже отклонён.", show_alert=True)
                return
            await self.user_manager.reject_user(user_id)
            if callback.message and callback.message.text:
                try:
                    await callback.message.edit_text(
                        callback.message.text + f"\n\n❌ <b>ОТКЛОНЕНО</b> (админ: @{html.escape(callback.from_user.username or '')})",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
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
                try:
                    await callback.message.edit_reply_markup(reply_markup=kb)
                except Exception:
                    pass

        elif action == "reset":
            await self._delete_user(user_id)
            if callback.message and callback.message.text:
                try:
                    await callback.message.edit_text(
                        callback.message.text + "\n\n🔄 <b>СТАТУС СБРОШЕН. Пользователь может подать заявку заново.</b>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        await callback.answer()

    # ------------------------------------------------------------------
    # Выбор ролей при одобрении
    # ------------------------------------------------------------------
    async def handle_role_choice(self, callback: types.CallbackQuery, state: FSMContext) -> None:
        """Обработка кликов по чек-боксам / страницам / кнопкам применения."""
        if not self.user_manager.is_admin(str(callback.from_user.id)):
            await callback.answer("⛔ Только администраторы.", show_alert=True)
            return

        data = await state.get_data()
        if not data or "roles" not in data or "target_user_id" not in data:
            await callback.answer(
                "Сессия выбора ролей истекла. Откройте заявку заново.",
                show_alert=True,
            )
            await state.clear()
            return

        roles: list[dict] = data["roles"]
        selected: set[int] = set(data.get("selected", {0}))
        page: int = int(data.get("page", 0))
        total_pages: int = int(data.get("total_pages", 1))
        user_data: dict = data.get("user_data", {})
        target_user_id: str | None = data.get("target_user_id")

        cbd = callback.data or ""

        if cbd.startswith("arole_toggle_"):
            try:
                idx = int(cbd[len("arole_toggle_"):])
            except ValueError:
                await callback.answer("Некорректный индекс.", show_alert=True)
                return
            if 0 <= idx < len(roles):
                if idx in selected:
                    selected.discard(idx)
                else:
                    selected.add(idx)
            await state.update_data(selected=selected)
            await callback.answer()

        elif cbd.startswith("arole_page_"):
            try:
                new_page = int(cbd[len("arole_page_"):])
            except ValueError:
                await callback.answer("Некорректная страница.", show_alert=True)
                return
            page = max(0, min(new_page, total_pages - 1))
            await state.update_data(page=page)
            await callback.answer()

        elif cbd == "arole_all":
            start = page * ROLES_PER_PAGE
            end = min(start + ROLES_PER_PAGE, len(roles))
            for i in range(start, end):
                selected.add(i)
            await state.update_data(selected=selected)
            await callback.answer(f"Выбрано: {len(selected)}")

        elif cbd == "arole_none":
            start = page * ROLES_PER_PAGE
            end = min(start + ROLES_PER_PAGE, len(roles))
            for i in range(start, end):
                selected.discard(i)
            await state.update_data(selected=selected)
            await callback.answer(f"Выбрано: {len(selected)}")

        elif cbd == "arole_noop":
            await callback.answer()
            return

        elif cbd == "arole_cancel":
            await state.clear()
            if callback.message and callback.message.text:
                try:
                    await callback.message.edit_text(
                        callback.message.text + "\n\n🚫 <b>Одобрение отменено.</b>",
                        parse_mode="HTML",
                        reply_markup=None,
                    )
                except Exception:
                    pass
            await callback.answer("Отменено.")
            return

        elif cbd == "arole_done":
            if not selected:
                await callback.answer(
                    "Выберите хотя бы одну роль или нажмите «Отмена».",
                    show_alert=True,
                )
                return
            if not target_user_id:
                await callback.answer("Ошибка: target_user_id не найден.", show_alert=True)
                await state.clear()
                return

            role_names = [roles[i]["name"] for i in sorted(selected)]

            try:
                ok = await self.user_manager.approve_user(target_user_id, roles=role_names)
            except Exception as e:
                logger.exception("Ошибка approve_user для %s", target_user_id)
                await callback.answer(f"Ошибка: {e}", show_alert=True)
                await state.clear()
                return

            await state.clear()

            if not ok:
                await callback.answer(
                    "Не удалось одобрить (пользователь не найден или ошибка записи).",
                    show_alert=True,
                )
                return

            roles_html = ", ".join(f"<code>{html.escape(r)}</code>" for r in role_names)
            admin_name = html.escape(callback.from_user.username or "")

            if callback.message and callback.message.text:
                try:
                    await callback.message.edit_text(
                        callback.message.text
                        + f"\n\n✅ <b>ОДОБРЕНО</b> с ролями: {roles_html}\n"
                        + f"(админ: @{admin_name})",
                        parse_mode="HTML",
                        reply_markup=None,
                    )
                except Exception as e:
                    logger.debug("Не удалось отредактировать сообщение (ок): %s", e)

            try:
                await self.bot.send_message(
                    chat_id=int(target_user_id),
                    text=(
                        "🎉 Ваша заявка одобрена!\n\n"
                        f"Назначенные роли: {', '.join(role_names)}\n\n"
                        "Напишите /start, чтобы начать."
                    ),
                )
            except Exception as e:
                logger.warning("Не удалось уведомить пользователя %s: %s", target_user_id, e)

            await callback.answer("Одобрено.")
            return

        else:
            await callback.answer()
            return

        try:
            text = self._render_roles_message(user_data, roles, selected, page, total_pages)
            kb = self._build_roles_keyboard(roles, selected, page, total_pages, prefix="arole_")
            if callback.message:
                try:
                    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
                except Exception as e:
                    if "message is not modified" not in str(e).lower():
                        logger.warning("Не удалось перерисовать экран ролей: %s", e)
        except Exception as e:
            logger.error("Ошибка перерисовки экрана ролей: %s", e)

    # ------------------------------------------------------------------
    # Fallback для устаревших arole_*
    # ------------------------------------------------------------------
    async def handle_role_stale(self, callback: types.CallbackQuery) -> None:
        await callback.answer(
            "Сессия выбора ролей истекла. Откройте заявку заново.",
            show_alert=True,
        )

    # ------------------------------------------------------------------
    # /users — админ-панель (ТОЛЬКО env-админы)
    # ------------------------------------------------------------------
    async def handle_users_forbidden(self, message: types.Message) -> None:
        """Fallback для /users от не-env-админов."""
        await message.answer(
            "⛔ Команда /users доступна только администраторам из .env (ADMIN_USERS)."
        )

    async def handle_users_command(self, message: types.Message, state: FSMContext) -> None:
        """Обработка /users [id|@username]. Только env-админы."""
        user_id = str(message.from_user.id)
        if not self.user_manager.is_env_admin(user_id):
            await self.handle_users_forbidden(message)
            return

        parts = (message.text or "").split(maxsplit=1)
        if len(parts) > 1:
            query = parts[1].strip()
            user_data = self.user_manager.find_user(query)
            if not user_data:
                await message.answer(f"Пользователь '{html.escape(query)}' не найден.")
                return
            await self._open_user_card(state, user_data, edit=False, message=message)
            return

        await self._show_users_list(state, page=0, edit=False, message=message)

    # ------------------------------------------------------------------
    # Список пользователей
    # ------------------------------------------------------------------
    async def _show_users_list(
        self,
        state: FSMContext,
        page: int,
        edit: bool,
        message: types.Message | None = None,
        callback: types.CallbackQuery | None = None,
    ) -> None:
        users, total = self.user_manager.list_users_paginated(page, USERS_PER_PAGE)
        total_pages = max(1, (total + USERS_PER_PAGE - 1) // USERS_PER_PAGE)

        await state.set_state(AdminState.browsing_users)
        await state.update_data(users_page=page)

        text = self._render_users_list(users, total, page, total_pages)
        kb = self._build_users_keyboard(users, page, total_pages)

        if edit and callback and callback.message:
            try:
                await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            except Exception as e:
                if "message is not modified" not in str(e).lower():
                    logger.warning("edit_text (users list): %s", e)
        elif message is not None:
            await message.answer(text, parse_mode="HTML", reply_markup=kb)

    def _render_users_list(
        self, users: list[dict], total: int, page: int, total_pages: int,
    ) -> str:
        lines = [f"📋 <b>Управление пользователями</b> (всего: {total})\n"]
        if not users:
            lines.append("Пользователей пока нет.")
            return "\n".join(lines)

        start_num = page * USERS_PER_PAGE
        for i, u in enumerate(users, 1):
            num = start_num + i
            uname = u.get("username")
            uname_part = f" (@{html.escape(uname)})" if uname else ""
            full_name = html.escape(str(u.get("full_name") or "—"))
            status = "✅" if u.get("is_active") else "⏸"
            roles = u.get("roles") or []
            roles_count = len(roles) if isinstance(roles, list) else 0
            roles_word = "роль" if roles_count == 1 else ("роли" if 1 < roles_count < 5 else "ролей")
            lines.append(f"{num}. {status} {full_name}{uname_part} — {roles_count} {roles_word}")

        if total_pages > 1:
            lines.append(f"\nСтраница {page + 1}/{total_pages}")
        return "\n".join(lines)

    def _build_users_keyboard(
        self, users: list[dict], page: int, total_pages: int,
    ) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []

        current_row: list[InlineKeyboardButton] = []
        for u in users:
            uid = str(u.get("id", ""))
            if not uid:
                continue
            label_name = str(u.get("full_name") or u.get("username") or uid)
            if len(label_name) > 25:
                label_name = label_name[:22] + "..."
            status = "✅" if u.get("is_active") else "⏸"
            current_row.append(InlineKeyboardButton(
                text=f"{status} {label_name}",
                callback_data=f"adm_user_open_{uid}",
            ))
            if len(current_row) == 2:
                rows.append(current_row)
                current_row = []
        if current_row:
            rows.append(current_row)

        if total_pages > 1:
            nav: list[InlineKeyboardButton] = []
            if page > 0:
                nav.append(InlineKeyboardButton(text="◀", callback_data=f"adm_users_list_{page - 1}"))
            nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="adm_noop"))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton(text="▶", callback_data=f"adm_users_list_{page + 1}"))
            rows.append(nav)

        return InlineKeyboardMarkup(inline_keyboard=rows)

    # ------------------------------------------------------------------
    # Карточка пользователя
    # ------------------------------------------------------------------
    async def _open_user_card(
        self,
        state: FSMContext,
        user_data: dict,
        edit: bool,
        message: types.Message | None = None,
        callback: types.CallbackQuery | None = None,
    ) -> None:
        uid = str(user_data.get("id", ""))
        if not uid:
            return

        await state.set_state(AdminState.viewing_user)
        await state.update_data(target_user_id=uid)

        text = self._render_user_card(user_data)
        kb = self._build_user_card_keyboard(user_data)

        if edit and callback and callback.message:
            try:
                await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            except Exception as e:
                if "message is not modified" not in str(e).lower():
                    logger.warning("edit_text (user card): %s", e)
        elif message is not None:
            await message.answer(text, parse_mode="HTML", reply_markup=kb)

    def _render_user_card(self, u: dict) -> str:
        uid = html.escape(str(u.get("id", "—")))
        full_name = html.escape(str(u.get("full_name") or "—"))
        tg_name = u.get("telegram_name")
        tg_part = f" ({html.escape(str(tg_name))})" if tg_name else ""
        username = u.get("username")
        uname_part = f"\n@{html.escape(username)}" if username else ""
        department = html.escape(str(u.get("department") or "—"))
        purpose = html.escape(str(u.get("purpose") or "—")[:300])
        is_active = bool(u.get("is_active"))
        status = "✅ активен" if is_active else "⏸ заблокирован"
        roles = u.get("roles") or []
        roles_str = ", ".join(html.escape(str(r)) for r in roles) if roles else "—"

        return (
            f"👤 <b>{full_name}</b>{tg_part}{uname_part}\n\n"
            f"ID: <code>{uid}</code>\n"
            f"Отдел: {department}\n"
            f"Цель: {purpose}\n"
            f"Статус: {status}\n"
            f"Роли: {roles_str}"
        )

    def _build_user_card_keyboard(self, u: dict) -> InlineKeyboardMarkup:
        uid = str(u.get("id", ""))
        is_active = bool(u.get("is_active"))
        is_env_admin = self.user_manager.is_env_admin(uid)

        rows: list[list[InlineKeyboardButton]] = []

        if not is_env_admin:
            block_label = "⏸ Заблокировать" if is_active else "▶️ Разблокировать"
            rows.append([
                InlineKeyboardButton(text="🎭 Изменить роли", callback_data=f"adm_user_roles_{uid}"),
                InlineKeyboardButton(text=block_label, callback_data=f"adm_user_block_{uid}"),
            ])
            rows.append([
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm_user_del_{uid}"),
            ])
        else:
            rows.append([
                InlineKeyboardButton(text="ℹ️ env-админ (не редактируется)", callback_data="adm_noop"),
            ])

        rows.append([
            InlineKeyboardButton(text="◀ Назад к списку", callback_data="adm_users_list_0"),
        ])

        return InlineKeyboardMarkup(inline_keyboard=rows)

    # ------------------------------------------------------------------
    # Редактор ролей для существующего пользователя
    # ------------------------------------------------------------------
    async def _open_role_editor(
        self,
        state: FSMContext,
        user_data: dict,
        edit: bool,
        callback: types.CallbackQuery,
    ) -> None:
        uid = str(user_data.get("id", ""))
        roles = self._load_roles()
        if not roles:
            await callback.answer(
                "В config/roles.yaml нет ролей.",
                show_alert=True,
            )
            return

        current_user_roles = set(user_data.get("roles") or [])
        selected: set[int] = {
            i for i, r in enumerate(roles) if r["name"] in current_user_roles
        }

        total_pages = max(1, (len(roles) + ROLES_PER_PAGE - 1) // ROLES_PER_PAGE)

        await state.set_state(AdminState.editing_roles)
        await state.update_data(
            target_user_id=uid,
            roles=roles,
            selected=selected,
            page=0,
            total_pages=total_pages,
            user_data=user_data,
        )

        text = self._render_roles_message(
            user_data, roles, selected, 0, total_pages,
            title=f"🎭 <b>Роли для: {html.escape(str(user_data.get('full_name') or uid))}</b>",
            show_purpose=False,
            show_status=True,
        )
        kb = self._build_roles_keyboard(
            roles, selected, 0, total_pages,
            prefix="adm_role_",
            done_label="💾 Сохранить",
            cancel_label="🚫 Отмена",
        )

        if callback.message:
            try:
                await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            except Exception as e:
                if "message is not modified" not in str(e).lower():
                    logger.warning("edit_text (role editor): %s", e)

    # ------------------------------------------------------------------
    # Общий обработчик adm_* (ТОЛЬКО env-админы)
    # ------------------------------------------------------------------
    async def handle_admin_callback(self, callback: types.CallbackQuery, state: FSMContext) -> None:
        """Обработчик всех adm_* callback'ов. Только env-админы."""
        cbd = callback.data or ""

        # ВАЖНО: только env-админы, а не все is_admin (защита от того, что
        # кто-то отредактирует users.yaml и добавит себе role: admin).
        if not self.user_manager.is_env_admin(str(callback.from_user.id)):
            await callback.answer(
                "⛔ Админ-панель доступна только администраторам из .env.",
                show_alert=True,
            )
            return

        if cbd == "adm_noop":
            await callback.answer()
            return

        # --- Список: пагинация ---
        if cbd.startswith("adm_users_list_"):
            try:
                page = int(cbd[len("adm_users_list_"):])
            except ValueError:
                await callback.answer("Некорректная страница.", show_alert=True)
                return
            await self._show_users_list(state, page=page, edit=True, callback=callback)
            await callback.answer()
            return

        # --- Открыть карточку ---
        if cbd.startswith("adm_user_open_"):
            uid = cbd[len("adm_user_open_"):]
            user_data = self.user_manager.get_user(uid)
            if not user_data:
                await callback.answer("Пользователь не найден.", show_alert=True)
                return
            await self._open_user_card(state, user_data, edit=True, callback=callback)
            await callback.answer()
            return

        # --- Изменить роли ---
        if cbd.startswith("adm_user_roles_"):
            uid = cbd[len("adm_user_roles_"):]
            user_data = self.user_manager.get_user(uid)
            if not user_data:
                await callback.answer("Пользователь не найден.", show_alert=True)
                return
            if self.user_manager.is_env_admin(uid):
                await callback.answer("env-админ: роли не редактируются.", show_alert=True)
                return
            await self._open_role_editor(state, user_data, edit=True, callback=callback)
            await callback.answer()
            return

        # --- Заблокировать / Разблокировать ---
        if cbd.startswith("adm_user_block_"):
            uid = cbd[len("adm_user_block_"):]
            user_data = self.user_manager.get_user(uid)
            if not user_data:
                await callback.answer("Пользователь не найден.", show_alert=True)
                return
            if self.user_manager.is_env_admin(uid):
                await callback.answer("env-админ: блокировка запрещена.", show_alert=True)
                return

            ok, new_active = await self.user_manager.toggle_user_active(uid)
            if not ok:
                await callback.answer("Не удалось изменить статус.", show_alert=True)
                return

            try:
                if new_active:
                    await self.bot.send_message(
                        chat_id=int(uid),
                        text="✅ Ваш доступ восстановлен. Можете задавать вопросы.",
                    )
                else:
                    await self.bot.send_message(
                        chat_id=int(uid),
                        text="⏸ Ваш доступ приостановлен администратором.",
                    )
            except Exception as e:
                logger.debug("Не удалось уведомить пользователя %s: %s", uid, e)

            fresh = self.user_manager.get_user(uid)
            if fresh:
                await self._open_user_card(state, fresh, edit=True, callback=callback)
            await callback.answer("Готово.")
            return

        # --- Удалить: подтверждение ---
        if cbd.startswith("adm_user_del_"):
            uid = cbd[len("adm_user_del_"):]
            user_data = self.user_manager.get_user(uid)
            if not user_data:
                await callback.answer("Пользователь не найден.", show_alert=True)
                return
            if self.user_manager.is_env_admin(uid):
                await callback.answer("env-админ: удаление запрещено.", show_alert=True)
                return

            full_name = html.escape(str(user_data.get("full_name") or uid))
            text = (
                f"⚠️ <b>Удалить пользователя?</b>\n\n"
                f"<b>{full_name}</b>\n"
                f"ID: <code>{html.escape(uid)}</code>\n\n"
                f"Запись будет полностью удалена из users.yaml.\n"
                f"Пользователь сможет подать заявку заново."
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"adm_user_del_confirm_{uid}"),
                    InlineKeyboardButton(text="❌ Отмена", callback_data=f"adm_user_open_{uid}"),
                ]
            ])
            if callback.message:
                try:
                    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
                except Exception as e:
                    if "message is not modified" not in str(e).lower():
                        logger.warning("edit_text (delete confirm): %s", e)
            await callback.answer()
            return

        # --- Удалить: подтверждено ---
        if cbd.startswith("adm_user_del_confirm_"):
            uid = cbd[len("adm_user_del_confirm_"):]
            ok = await self.user_manager.delete_user(uid)
            if not ok:
                await callback.answer("Не удалось удалить.", show_alert=True)
                return
            await self._show_users_list(state, page=0, edit=True, callback=callback)
            await callback.answer("Удалён.")
            return

        # --- Редактор ролей: toggle / page / all / none / cancel / done ---
        if cbd.startswith("adm_role_"):
            await self._handle_admin_role_choice(callback, state)
            return

        await callback.answer()

    async def _handle_admin_role_choice(self, callback: types.CallbackQuery, state: FSMContext) -> None:
        """Обработка adm_role_* — аналог handle_role_choice, но для админки."""
        # Дополнительная защита (handle_admin_callback уже проверил)
        if not self.user_manager.is_env_admin(str(callback.from_user.id)):
            await callback.answer("⛔ Только env-админы.", show_alert=True)
            return

        data = await state.get_data()
        if not data or "roles" not in data or "target_user_id" not in data:
            await callback.answer(
                "Сессия редактирования ролей истекла. Откройте пользователя заново.",
                show_alert=True,
            )
            await state.clear()
            return

        roles: list[dict] = data["roles"]
        selected: set[int] = set(data.get("selected", set()))
        page: int = int(data.get("page", 0))
        total_pages: int = int(data.get("total_pages", 1))
        user_data: dict = data.get("user_data", {})
        target_user_id: str | None = data.get("target_user_id")

        cbd = callback.data or ""

        if cbd.startswith("adm_role_toggle_"):
            try:
                idx = int(cbd[len("adm_role_toggle_"):])
            except ValueError:
                await callback.answer("Некорректный индекс.", show_alert=True)
                return
            if 0 <= idx < len(roles):
                if idx in selected:
                    selected.discard(idx)
                else:
                    selected.add(idx)
            await state.update_data(selected=selected)
            await callback.answer()

        elif cbd.startswith("adm_role_page_"):
            try:
                new_page = int(cbd[len("adm_role_page_"):])
            except ValueError:
                await callback.answer("Некорректная страница.", show_alert=True)
                return
            page = max(0, min(new_page, total_pages - 1))
            await state.update_data(page=page)
            await callback.answer()

        elif cbd == "adm_role_all":
            start = page * ROLES_PER_PAGE
            end = min(start + ROLES_PER_PAGE, len(roles))
            for i in range(start, end):
                selected.add(i)
            await state.update_data(selected=selected)
            await callback.answer(f"Выбрано: {len(selected)}")

        elif cbd == "adm_role_none":
            start = page * ROLES_PER_PAGE
            end = min(start + ROLES_PER_PAGE, len(roles))
            for i in range(start, end):
                selected.discard(i)
            await state.update_data(selected=selected)
            await callback.answer(f"Выбрано: {len(selected)}")

        elif cbd == "adm_role_noop":
            await callback.answer()
            return

        elif cbd == "adm_role_cancel":
            if target_user_id:
                fresh = self.user_manager.get_user(target_user_id)
                if fresh:
                    await self._open_user_card(state, fresh, edit=True, callback=callback)
                    await callback.answer("Отменено.")
                    return
            await state.clear()
            await callback.answer("Отменено.")
            return

        elif cbd == "adm_role_done":
            if not target_user_id:
                await callback.answer("Ошибка: target_user_id не найден.", show_alert=True)
                await state.clear()
                return

            role_names = [roles[i]["name"] for i in sorted(selected)]
            ok = await self.user_manager.set_user_roles(target_user_id, role_names)
            if not ok:
                await callback.answer("Не удалось сохранить роли.", show_alert=True)
                return

            roles_str = ", ".join(role_names) if role_names else "—"
            try:
                await self.bot.send_message(
                    chat_id=int(target_user_id),
                    text=f"🎭 Ваши роли обновлены: {roles_str}",
                )
            except Exception as e:
                logger.debug("Не удалось уведомить пользователя %s: %s", target_user_id, e)

            fresh = self.user_manager.get_user(target_user_id)
            if fresh:
                await self._open_user_card(state, fresh, edit=True, callback=callback)
            await callback.answer("Роли сохранены.")
            return

        else:
            await callback.answer()
            return

        try:
            text = self._render_roles_message(
                user_data, roles, selected, page, total_pages,
                title=f"🎭 <b>Роли для: {html.escape(str(user_data.get('full_name') or target_user_id or ''))}</b>",
                show_purpose=False,
                show_status=True,
            )
            kb = self._build_roles_keyboard(
                roles, selected, page, total_pages,
                prefix="adm_role_",
                done_label="💾 Сохранить",
                cancel_label="🚫 Отмена",
            )
            if callback.message:
                try:
                    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
                except Exception as e:
                    if "message is not modified" not in str(e).lower():
                        logger.warning("Не удалось перерисовать редактор ролей: %s", e)
        except Exception as e:
            logger.error("Ошибка перерисовки редактора ролей: %s", e)

    # ------------------------------------------------------------------
    # Удаление (reset из модерации)
    # ------------------------------------------------------------------
    async def _delete_user(self, user_id: str) -> None:
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
        await message.answer("⚠️ Я понимаю только текстовые сообщения. Напишите вопрос словами.")

    # ------------------------------------------------------------------
    # Обработка входящего текста
    # ------------------------------------------------------------------
    async def handle_message(self, message: types.Message, state: FSMContext) -> None:
        """Обработка входящего текстового сообщения."""
        user_id = str(message.from_user.id)
        text = (message.text or "").strip()

        command_word = text.split()[0].lower() if text else ""
        is_service = command_word in SERVICE_COMMANDS

        if not is_service:
            if not self.user_manager.is_admin(user_id) and not self.user_manager.is_user_active(user_id):
                await self.handle_start_or_unknown(message, state)
                return

        async def send_status(status_text: str):
            # ВАЖНО: html.escape здесь НЕ применяется. status_text формирует
            # LLM (по промпту — <b>/<code>), экранирование превратило бы
            # теги в &lt;b&gt; и они показались бы пользователю текстом.
            try:
                await message.answer(status_text, parse_mode="HTML")
            except Exception as e:
                # Fallback: некорректная разметка (незакрытый тег, одиночный
                # '<') — шлём без parse_mode, чтобы бот не упал.
                logger.warning(
                    "Failed to send status with HTML (%s), retrying as plain text",
                    e,
                )
                try:
                    await message.answer(status_text, parse_mode=None)
                except Exception as e2:
                    logger.warning("Failed to send status: %s", e2)

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
                try:
                    await message.answer("⛔ Выполнение прервано командой /stop.")
                except Exception:
                    pass
                raise
            except Exception as e:
                logger.exception("Error in background task: %s", e)
                try:
                    await message.answer("⚠️ Произошла ошибка при выполнении запроса.")
                except Exception:
                    pass
            finally:
                if session_state.current_task is asyncio.current_task():
                    session_state.current_task = None

        if session_state.current_task and not session_state.current_task.done():
            await message.answer("⏳ Пожалуйста, дождитесь ответа или отправьте /stop для отмены.")
            return

        task = asyncio.create_task(run_and_reply())
        session_state.current_task = task

    # ------------------------------------------------------------------
    # Отправка длинных сообщений
    # ------------------------------------------------------------------
    async def _answer_html(self, message: types.Message, text: str) -> None:
        """Отправляет текст с parse_mode="HTML".

        Если Telegram отверг разметку (одиночный '<', разорванный тег и т.п.),
        повторяем отправку без parse_mode — пользователь должен получить ответ.
        """
        try:
            await message.answer(text, parse_mode="HTML")
        except TelegramBadRequest as e:
            logger.warning("HTML-разметка отклонена Telegram (%s), шлю как текст", e)
            await message.answer(text, parse_mode=None)

    async def _send_long_message(self, message: types.Message, text: str) -> None:
        max_length = 4000
        if len(text) <= max_length:
            await self._answer_html(message, text)
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
            await self._answer_html(message, part)
            if i < len(parts) - 1:
                await asyncio.sleep(0.3)

    # ------------------------------------------------------------------
    # Публичные методы (интерфейс BaseChatInterface)
    # ------------------------------------------------------------------
    async def send_message(self, chat_id: str, text: str) -> None:
        try:
            await self.bot.send_message(
                chat_id=int(chat_id), text=text, parse_mode="HTML",
            )
        except TelegramBadRequest as e:
            logger.warning("HTML-разметка отклонена Telegram (%s), шлю как текст", e)
            await self.bot.send_message(
                chat_id=int(chat_id), text=text, parse_mode=None,
            )

    async def send_image(self, chat_id: str, image_path: str) -> None:
        await self.bot.send_photo(
            chat_id=int(chat_id),
            photo=FSInputFile(image_path),
        )

    async def close(self) -> None:
        await self.bot.session.close()
