# Telegram-адаптер (`app/interfaces/telegram_adapter.py`)

## Назначение

Модуль реализует Telegram-интерфейс проекта Repdix на базе `aiogram 3`. Версия файла: `261028.08`; в docstring перечислены ключевые изменения версии `261028.07`.

Основные задачи:

- приём сообщений и callback-кнопок Telegram;
- регистрация новых пользователей через FSM;
- модерация заявок на доступ: одобрение, отклонение, сброс;
- админ-панель `/users` для управления пользователями и ролями;
- настройка меню Telegram-команд: базовое — всем, расширенное с `/users` — только env-админам;
- маршрутизация текстовых запросов в `RepdixEngine`;
- отправка длинных ответов, изображений и уведомлений;
- отмена текущего анализа командой `/stop`;
- корректное закрытие Telegram-сессии.

Класс `TelegramAdapter` наследует `BaseChatInterface` и является адаптером между Telegram и ядром приложения.

## Ключевые особенности доступа

- `/users` доступна только env-админам из `ADMIN_USERS`.
- Меню команд: базовый набор — всем, расширенный с `/users` — только env-админам через `BotCommandScopeChat`.
- Регистрация `Command("users")` с фильтром по `F.from_user.id.in_(env_admin_ids)`; для не-env-админов хэндлер не вызывается.
- Fallback `handle_users_forbidden` ловит `/users` от не-env-админов и отвечает отказом.
- `handle_admin_callback` и `_handle_admin_role_choice` защищены проверкой `is_env_admin`.
- Модерация заявок (`approve_*`, `reject_*`, `reset_*`) по дизайну доступна всем администраторам: и env-админам, и пользователям с YAML-ролью `admin`.
- `handle_users_command` дополнительно проверяет `is_env_admin`.
- `_refresh_admin_commands()` переустанавливает per-chat меню для env-админов при старте. Если `ADMIN_USERS` изменится, нужен перезапуск бота или повторный вызов метода.

## Основные константы

- `SERVICE_COMMANDS` — служебные команды, которые не должны перехватываться основной логикой доступа: `/start`, `/help`, `/stop`, `/history`, `/compact`, `/tokens`, `/clear`, `/settings`, `/db`, `/model`, `/models`, `/sql`. `/users` в этот набор намеренно не включён.
- `ROLES_PER_PAGE = 6` — количество ролей на странице выбора.
- `USERS_PER_PAGE = 10` — количество пользователей на странице `/users`.
- `SHOW_ADMIN_ROLE = False` — роль `admin` по умолчанию скрыта в списке выбора ролей.
- `ROLE_DESC_MAX_LEN = 200` — максимальная длина описания роли в сообщении.

## FSM-состояния

- `RegistrationState`
  - `waiting_for_name` — ожидание ФИО;
  - `waiting_for_department` — ожидание отдела/должности;
  - `waiting_for_purpose` — ожидание цели доступа.
- `ApprovalState`
  - `choosing_roles` — выбор ролей при одобрении заявки.
- `AdminState`
  - `browsing_users` — просмотр списка пользователей;
  - `viewing_user` — просмотр карточки пользователя;
  - `editing_roles` — редактирование ролей пользователя.

## Класс `TelegramAdapter`

### Инициализация

`TelegramAdapter(engine: RepdixEngine)`

Сохраняет `engine`, создаёт:

- `Bot(token=os.environ["TELEGRAM_TOKEN"])`;
- `Dispatcher()`;
- `user_manager = engine.user_manager`.

Для запуска обязательна переменная окружения `TELEGRAM_TOKEN`.

### Администрирование и меню команд

- `_env_admin_ids() -> set[int]` — возвращает множество ID env-админов из `user_manager.admins`. Невалидные ID пропускаются с предупреждением.
- `_refresh_admin_commands() -> None` — устанавливает базовое меню команд всем пользователям, а расширенное меню с `/users` — только env-админам через `BotCommandScopeChat`.
- `_env_admin_ids` и `_refresh_admin_commands` используются при старте и при необходимости переустановки меню.

### Запуск и регистрация хэндлеров

- `start() -> None` — регистрирует хэндлеры и запускает polling.
- Регистрируются:
  - `/start`, `/stop`;
  - `/users` с фильтром только для env-админов;
  - fallback для `/users` от не-env-админов;
  - callback-и модерации: `approve_*`, `reject_*`, `reset_*`;
  - callback-и выбора ролей: `arole_*`;
  - callback-и админ-панели: `adm_*`;
  - FSM-шаги регистрации;
  - обработчик не-текстовых сообщений;
  - общий обработчик текстовых сообщений.

### Старт, доступ и регистрация пользователей

- `handle_start_or_unknown(message: types.Message, state: FSMContext | None) -> None` — обрабатывает `/start` и незнакомые сообщения. Проверяет, является ли пользователь админом, активным пользователем или заявителем на рассмотрении. Для известных пользователей обновляет профиль. Если пользователь новый — запускает FSM-регистрацию.
- `handle_stop(message: types.Message) -> None` — отменяет текущую задачу анализа пользователя, если она есть.
- `process_name(message: types.Message, state: FSMContext)` — принимает ФИО, требует минимум два слова.
- `process_department(message: types.Message, state: FSMContext)` — принимает отдел/должность.
- `process_purpose(message: types.Message, state: FSMContext)` — принимает цель доступа, сохраняет заявку через `user_manager.add_user(...)` и отправляет уведомление всем администраторам из `user_manager.admins`. Если `ADMIN_USERS` пуст, пишет предупреждение в лог и никому не отправляет заявку.

### Роли

- `_load_roles() -> list[dict]` — загружает роли из `config/roles.yaml`. Обрабатывает отсутствие файла, ошибки YAML и некорректные записи. По умолчанию скрывает роль `admin`, если `SHOW_ADMIN_ROLE=False`. Сортирует роли: `viewer` первой, далее по имени.
- `_build_roles_keyboard(roles: list[dict], selected: set[int], page: int, total_pages: int, prefix: str = "arole_", done_label: str = "💾 Применить", cancel_label: str = "🚫 Отмена") -> InlineKeyboardMarkup` — строит универсальную клавиатуру выбора ролей с чек-боксами, пагинацией, кнопками «все на странице», «снять на странице», «применить» и «отмена».
- `_render_roles_message(user_data: dict, roles: list[dict], selected: set[int], page: int, total_pages: int, title: str = "🔔 <b>Одобрение заявки</b>", show_purpose: bool = True, show_status: bool = False) -> str` — формирует текст экрана выбора ролей. Экранирует пользовательские данные, при необходимости показывает цель и статус, обрезает описание роли до `ROLE_DESC_MAX_LEN`.

### Модерация заявок

- `handle_approval(callback: types.CallbackQuery, state: FSMContext) -> None` — обрабатывает `approve_*`, `reject_*`, `reset_*`.
  - Доступно всем администраторам (`is_admin`), включая env-админов и YAML-роли `admin`.
  - При одобрении открывает экран выбора ролей, сохраняет состояние `ApprovalState.choosing_roles`.
  - При отклонении вызывает `user_manager.reject_user(...)` и обновляет сообщение.
  - При сбросе удаляет пользователя через `_delete_user(...)`, чтобы он мог подать заявку заново.
- `handle_role_choice(callback: types.CallbackQuery, state: FSMContext) -> None` — обрабатывает выбор ролей при одобрении заявки: переключение чек-боксов, пагинацию, выбор всех/никаких, сохранение или отмену. Сохраняет роли через `user_manager.approve_user(target_user_id, roles=role_names)` и уведомляет пользователя.
- `handle_role_stale(callback: types.CallbackQuery) -> None` — fallback для устаревших callback-ов `arole_*`, сообщает об истёкшей сессии.

### Админ-панель `/users`

- `handle_users_forbidden(message: types.Message) -> None` — отвечает не-env-админам, что `/users` доступна только администраторам из `.env`.
- `handle_users_command(message: types.Message, state: FSMContext) -> None` — обрабатывает `/users` и `/users [id|@username]`. Дополнительно проверяет `is_env_admin`. Если передан запрос, ищет пользователя через `find_user`, иначе показывает список.
- `_show_users_list(state: FSMContext, page: int, edit: bool, message: types.Message | None = None, callback: types.CallbackQuery | None = None) -> None` — отображает постраничный список пользователей, устанавливает состояние `AdminState.browsing_users`.
- `_render_users_list(users: list[dict], total: int, page: int, total_pages: int) -> str` — формирует текст списка пользователей: статус, ФИО, username, количество ролей, номер страницы.
- `_build_users_keyboard(users: list[dict], page: int, total_pages: int) -> InlineKeyboardMarkup` — строит клавиатуру списка пользователей: кнопки открытия карточки по 2 в ряд и пагинация.
- `_open_user_card(state: FSMContext, user_data: dict, edit: bool, message: types.Message | None = None, callback: types.CallbackQuery | None = None) -> None` — открывает карточку пользователя, устанавливает состояние `AdminState.viewing_user`.
- `_render_user_card(u: dict) -> str` — формирует текст карточки пользователя: ФИО, Telegram-имя, username, ID, отдел, цель, статус, роли.
- `_build_user_card_keyboard(u: dict) -> InlineKeyboardMarkup` — строит клавиатуру карточки: изменение ролей, блокировка/разблокировка, удаление, возврат к списку. Для env-админов редактирование, блокировка и удаление запрещены.
- `_open_role_editor(state: FSMContext, user_data: dict, edit: bool, callback: types.CallbackQuery) -> None` — открывает редактор ролей существующего пользователя, устанавливает состояние `AdminState.editing_roles`, использует префикс `adm_role_`.
- `handle_admin_callback(callback: types.CallbackQuery, state: FSMContext) -> None` — обрабатывает все `adm_*` callback-и. Доступен только env-админам. Обрабатывает пагинацию списка, открытие карточки, изменение ролей, блокировку/разблокировку, удаление с подтверждением, редактор ролей.
- `_handle_admin_role_choice(callback: types.CallbackQuery, state: FSMContext) -> None` — обрабатывает `adm_role_*`: переключение ролей, пагинацию, выбор всех/никаких, сохранение, отмену. Сохраняет роли через `user_manager.set_user_roles(...)` и уведомляет пользователя.
- `_delete_user(user_id: str) -> None` — удаляет пользователя из `users.yaml` через `user_manager.delete_user(...)`; используется при сбросе статуса заявки.

### Обработка сообщений

- `handle_non_text(message: types.Message) -> None` — отвечает на не-текстовые сообщения.
- `handle_message(message: types.Message, state: FSMContext) -> None` — основной обработчик текста.
  - Для служебных команд из `SERVICE_COMMANDS` пропускает проверку активности пользователя.
  - Для остальных сообщений проверяет, что пользователь админ или активен; иначе вызывает `handle_start_or_unknown`.
  - Формирует `ChatMessage(user_id, chat_id, text, status_callback)` и вызывает `engine.process_message(msg)`.
  - Запускает обработку в отдельной `asyncio.Task`, сохраняя её в `session_state.current_task`.
  - Если у пользователя уже есть активная задача — просит дождаться ответа или отправить `/stop`.
  - В фоновой задаче обрабатывает `asyncio.CancelledError`, общие исключения и очищает `current_task` в `finally`.
- `_send_long_message(message: types.Message, text: str) -> None` — отправляет длинный ответ частями до 4000 символов, стараясь разбивать по переводу строки, с задержкой между частями.

### Публичные методы

- `send_message(chat_id: str, text: str) -> None` — отправляет текст в указанный чат.
- `send_image(chat_id: str, image_path: str) -> None` — отправляет изображение через `FSInputFile`.
- `close() -> None` — закрывает сессию бота.

## Зависимости

### Внешние библиотеки

- `asyncio`, `html`, `logging`, `os` — стандартная библиотека Python.
- `yaml` — чтение `config/roles.yaml`.
- `aiogram` 3:
  - `Bot`, `Dispatcher`, `F`, `types`;
  - `Command`;
  - `FSMContext`, `State`, `StatesGroup`;
  - `BotCommand`, `BotCommandScopeChat`;
  - `FSInputFile`, `InlineKeyboardButton`, `InlineKeyboardMarkup`.

### Внутренние модули проекта

- `app.core.engine.RepdixEngine` — ядро приложения.
- `app.interfaces.base.BaseChatInterface`, `ChatMessage`, `ChatResponse` — базовый интерфейс и модели сообщений/ответов.

### Внешние ресурсы и окружение

- `TELEGRAM_TOKEN` — обязательная переменная окружения для создания `Bot`.
- `config/roles.yaml` — список ролей и их описаний.
- `user_manager.admins` — список env-админов, предположительно из `ADMIN_USERS`.
- `users.yaml` — косвенно используется через методы `user_manager`.

## Взаимодействие с другими модулями проекта

`TelegramAdapter` реализует `BaseChatInterface`, поэтому ожидаемо создаётся на уровне запуска приложения с готовым `RepdixEngine` и затем запускается через `start()`.

Внутри модуль использует:

- `engine.process_message(ChatMessage)` — для обработки пользовательских запросов;
- `engine.sessions.get(user_id)` и `engine.sessions.peek(user_id)` — для доступа к состоянию сессии и текущей задаче;
- `engine.user_manager` — для проверки доступа, регистрации, модерации, управления пользователями и ролями.

Другие модули могут использовать публичные методы адаптера:

- `send_message(chat_id, text)`;
- `send_image(chat_id, image_path)`;
- `close()`.

Обратных импортов данного модуля в самом файле не видно; ожидаемое использование — импорт `TelegramAdapter` из точки входа приложения.

## Важные ограничения и особенности

- `/users` доступна только env-админам из `ADMIN_USERS`. Для не-env-админов срабатывает `handle_users_forbidden`.
- Callback-и `adm_*` защищены проверкой `is_env_admin`, чтобы YAML-роль `admin` не давала доступ к админ-панели пользователей.
- Модерация заявок (`approve_*`, `reject_*`, `reset_*`) по дизайну доступна всем администраторам: и env-админам, и пользователям с YAML-ролью `admin`.
- Если `ADMIN_USERS` пуст:
  - `/users` не будет доступна никому;
  - уведомления о новых заявках не будут отправлены;
  - расширенное меню команд не установится.
- Невалидные ID в `ADMIN_USERS` пропускаются с предупреждением.
- Если в `config/roles.yaml` нет ролей, одобрение заявки и редактор ролей не смогут работать.
- Роль `admin` по умолчанию не показывается в списке ролей (`SHOW_ADMIN_ROLE=False`).
- У пользователя может быть только одна активная задача анализа; повторные сообщения во время выполнения получают предупреждение.
- `/stop` отменяет текущую задачу через `asyncio.Task.cancel()`.
- Длинные ответы разбиваются на части до 4000 символов с задержкой между отправками.
- Пользовательские данные в HTML-сообщениях экранируются через `html.escape`.
- Ошибки редактирования сообщений логируются; ошибка `message is not modified` в ряде мест игнорируется.
- Не-текстовые сообщения получают ответ о том, что бот понимает только текст.
- env-админы в админ-панели защищены от изменения ролей, блокировки и удаления.
- `_refresh_admin_commands()` может не установить меню для конкретного админа, если тот ещё не начинал диалог с ботом; ошибка логируется как предупреждение.