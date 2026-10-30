# Telegram-адаптер (`app/interfaces/telegram_adapter.py`)

## Назначение

Модуль реализует Telegram-интерфейс проекта Repdix на базе `aiogram 3`. Версия файла: `261030.02`; в docstring перечислены ключевые изменения версии.

Основные задачи:

- приём сообщений и callback-кнопок Telegram;
- регистрация новых пользователей через FSM;
- модерация заявок на доступ: одобрение, отклонение, сброс;
- админ-панель `/users` для управления пользователями и ролями;
- настройка меню Telegram-команд: базовое — всем, расширенное с `/users` — только env-админам;
- маршрутизация текстовых запросов в `RepdixEngine`;
- отправка длинных ответов, изображений и уведомлений;
- отмена текущего анализа командой `/stop`;
- управление долгосрочной памятью через `/memory`, `/remember`, `/forget`, `/reset` и inline-callback’и `mem_*`;
- санитизация HTML в ответах LLM и статусах перед отправкой в Telegram;
- корректное закрытие Telegram-сессии.

Класс `TelegramAdapter` наследует `BaseChatInterface` и является адаптером между Telegram и ядром приложения.

## Ключевые особенности текущей версии

- `handle_memory_callback` явно проверяет `callback.from_user`; если Telegram не передал контекст пользователя, callback отклоняется.
- `user_id` для памяти всегда берётся из `callback.from_user.id`, а не из `callback_data`.
- Кнопки памяти используют `mem_edit_<hash>` / `mem_del_<hash>` (хэш текста факта), а не индекс. В FSM сохраняются `memory_edit_hash` и `memory_edit_fact`.
- Если факт уже удалён или изменён, пользователь получает сообщение «Факт уже удалён или изменён».
- Ответы LLM и статусы проходят через `sanitize_telegram_html()`: безопасные теги Telegram сохраняются, потенциально опасные конструкции экранируются.
- При отказе Telegram от HTML-разметки отправка повторяется без `parse_mode`.
- `_send_long_message` / `_answer_html` принимают `reply_markup` и крепят клавиатуру к последней части длинного сообщения.

## Ключевые особенности доступа

- `/users` доступна только env-админам из `ADMIN_USERS`.
- Меню команд: базовый набор — всем, расширенный с `/users` — только env-админам через `BotCommandScopeChat`.
- Регистрация `Command("users")` с фильтром по `F.from_user.id.in_(env_admin_ids)`; для не-env-админов хэндлер не вызывается.
- Fallback `handle_users_forbidden` ловит `/users` от не-env-админов и отвечает отказом.
- `handle_admin_callback` и `_handle_admin_role_choice` защищены проверкой `is_env_admin`.
- Модерация заявок (`approve_*`, `reject_*`, `reset_*`) по дизайну доступна всем администраторам: и env-админам, и пользователям с YAML-ролью `admin`.
- `handle_users_command` дополнительно проверяет `is_env_admin`.
- `_refresh_admin_commands()` переустанавливает per-chat меню для env-админов при старте. Если `ADMIN_USERS` изменится, нужен перезапуск бота или повторный вызов метода.
- `handle_memory_callback` не проверяет `is_admin` / `is_user_active`; идентичность пользователя берётся из `callback.from_user.id`.

## Санитизация HTML

- `sanitize_telegram_html(text: str) -> str` — экранирует HTML, оставляя только безопасные теги Telegram.
- Разрешённые теги: `b`, `strong`, `i`, `em`, `u`, `s`, `code`, `pre`, `blockquote`, `tg-spoiler`, `a`.
- Для `<a>` разрешаются только ссылки `http://`, `https://`, `tg://`; остальные варианты превращаются в обычный текст.
- Атрибуты безопасных тегов, кроме разрешённого `href` у `<a>`, отбрасываются.
- `_answer_html()` и `send_message()` прогоняют текст через санитайзер.
- `_send_long_message()` идёт через `_answer_html()`, поэтому санитизация применяется к каждой части длинного сообщения.
- `send_status()` в `handle_message()` также санитизируется, так как статус формирует LLM.
- Полный `html.escape()` для ответов LLM сознательно не применяется, чтобы не ломать безопасные теги вроде `<b>`.
- Если Telegram всё равно отвергает разметку, выполняется fallback-отправка без `parse_mode`.

## Основные константы

- `SERVICE_COMMANDS` — служебные команды, которые не должны перехватываться основной логикой доступа: `/start`, `/help`, `/stop`, `/history`, `/compact`, `/tokens`, `/clear`, `/settings`, `/db`, `/model`, `/models`, `/sql`, `/remember`, `/memory`, `/forget`, `/reset`. `/users` в этот набор намеренно не включён.
- `ROLES_PER_PAGE = 6` — количество ролей на странице выбора.
- `USERS_PER_PAGE = 10` — количество пользователей на странице `/users`.
- `SHOW_ADMIN_ROLE = False` — роль `admin` по умолчанию скрыта в списке выбора ролей.
- `ROLE_DESC_MAX_LEN = 200` — максимальная длина описания роли в сообщении.
- В `_send_long_message` используется локальный лимит длины части сообщения — `4000` символов.

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
- `MemoryState`
  - `editing` — ожидание нового текста существующего факта памяти;
  - `adding` — ожидание текста нового факта памяти.

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
- В базовое меню включены команды памяти: `/remember`, `/memory`, `/forget`, `/reset`.
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
  - callback-и долгосрочной памяти: `mem_*`;
  - FSM-шаги регистрации;
  - FSM-шаги памяти: `process_memory_edit`, `process_memory_add`;
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

### Долгосрочная память

- `_to_markup(markup) -> InlineKeyboardMarkup | None` — статический метод. Приводит `reply_markup` из ядра к `InlineKeyboardMarkup`; принимает уже готовый `InlineKeyboardMarkup` или dict вида `{"inline_keyboard": [...]}`. Некорректная разметка логируется и не ломает отправку ответа.
- `_send_memory_screen(message: types.Message, user_id: str) -> None` — получает экран памяти через `engine.render_memory(user_id)` и отправляет его через `_answer_html(...)` с `reply_markup`.
- `_render_memory_message(callback: types.CallbackQuery, user_id: str) -> None` — перерисовывает существующее сообщение с памятью через `engine.render_memory(user_id)` и `callback.message.edit_text(...)`; при ошибке отправляет новое сообщение.
- `handle_memory_callback(callback: types.CallbackQuery, state: FSMContext) -> None` — обрабатывает `mem_*`:
  - проверяет `callback.from_user`;
  - берёт `user_id` из `callback.from_user.id`;
  - `mem_add` — устанавливает `MemoryState.adding` и просит ввести текст факта;
  - `mem_clear` — очищает память через `engine.memory_clear(user_id)`, сбрасывает FSM и перерисовывает экран;
  - `mem_edit_<hash>` — ищет факт через `engine.memory_find_by_hash(user_id, fact_hash)`, сохраняет `memory_edit_hash` и `memory_edit_fact`, устанавливает `MemoryState.editing`;
  - `mem_del_<hash>` — удаляет факт через `engine.memory_delete_by_hash(user_id, fact_hash)` и перерисовывает экран;
  - если факт не найден, показывает «Факт уже удалён или изменён».
- `process_memory_add(message: types.Message, state: FSMContext) -> None` — FSM-обработчик нового факта. Если текст начинается с `/`, сбрасывает состояние и передаёт сообщение в `handle_message`. Иначе проверяет непустой текст, вызывает `engine.memory_add(user_id, text)`, сбрасывает FSM, отвечает «Запомнил.» и отправляет экран памяти.
- `process_memory_edit(message: types.Message, state: FSMContext) -> None` — FSM-обработчик изменения факта. Если текст начинается с `/`, сбрасывает состояние и передаёт сообщение в `handle_message`. Проверяет `memory_edit_hash`, вызывает `engine.memory_update_by_hash(user_id, fact_hash, text)`, сбрасывает FSM и при успехе отправляет обновлённый экран памяти.

### Обработка сообщений

- `handle_non_text(message: types.Message) -> None` — отвечает на не-текстовые сообщения.
- `handle_message(message: types.Message, state: FSMContext) -> None` — основной обработчик текста.
  - Для служебных команд из `SERVICE_COMMANDS`, включая команды памяти, пропускает проверку активности пользователя.
  - Для остальных сообщений проверяет, что пользователь админ или активен; иначе вызывает `handle_start_or_unknown`.
  - Внутренний `send_status(status_text)` санитизирует статус через `sanitize_telegram_html()` и отправляет с `parse_mode="HTML"`; при ошибке повторяет отправку без `parse_mode`.
  - Формирует `ChatMessage(user_id, chat_id, text, status_callback)` и вызывает `engine.process_message(msg)`.
  - Запускает обработку в отдельной `asyncio.Task`, сохраняя её в `session_state.current_task`.
  - Если у пользователя уже есть активная задача — просит дождаться ответа или отправить `/stop`.
  - В фоновой задаче обрабатывает `asyncio.CancelledError`, общие исключения и очищает `current_task` в `finally`.
  - Ответ движка отправляет через `_send_long_message(message, result.text, reply_markup=result.reply_markup)`.

### Отправка длинных сообщений

- `_answer_html(message: types.Message, text: str, reply_markup=None) -> None` — отправляет текст с `parse_mode="HTML"` и опциональной клавиатурой. Текст санитизируется через `sanitize_telegram_html()`, `reply_markup` приводится через `_to_markup()`. При `TelegramBadRequest` повторяет отправку без `parse_mode`, сохраняя клавиатуру.
- `_send_long_message(message: types.Message, text: str, reply_markup=None) -> None` — отправляет длинный ответ частями до `4000` символов, стараясь разбивать по переводу строки, с задержкой между частями. `reply_markup` крепится только к последней части.

### Публичные методы

- `send_message(chat_id: str, text: str) -> None` — отправляет текст в указанный чат. Текст санитизируется, отправляется с `parse_mode="HTML"`; при `TelegramBadRequest` повторяется отправка без `parse_mode`.
- `send_image(chat_id: str, image_path: str) -> None` — отправляет изображение через `FSInputFile`.
- `close() -> None` — закрывает сессию бота.

## Зависимости

### Внешние библиотеки

- `asyncio`, `html`, `logging`, `os`, `re` — стандартная библиотека Python.
- `yaml` — чтение `config/roles.yaml`.
- `aiogram` 3:
  - `Bot`, `Dispatcher`, `F`, `types`;
  - `TelegramBadRequest`;
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
- `engine.user_manager` — для проверки доступа, регистрации, модерации, управления пользователями и ролями;
- `engine.render_memory(user_id)` — для отрисовки экрана долгосрочной памяти;
- `engine.memory_clear(user_id)`, `engine.memory_find_by_hash(user_id, fact_hash)`, `engine.memory_delete_by_hash(user_id, fact_hash)`, `engine.memory_add(user_id, text)`, `engine.memory_update_by_hash(user_id, fact_hash, text)` — для управления фактами памяти.

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
- Длинные ответы разбиваются на части до `4000` символов с задержкой между отправками; `reply_markup` крепится к последней части.
- Пользовательские данные в административных HTML-сообщениях экранируются через `html.escape`.
- Ответы LLM и статусы не экранируются полностью, а проходят через `sanitize_telegram_html()`: безопасные теги Telegram сохраняются, опасные теги и атрибуты превращаются в текст.
- Если Telegram отвергает HTML-разметку, ответ отправляется без `parse_mode`, чтобы пользователь получил текст.
- Ошибки редактирования сообщений логируются; ошибка `message is not modified` в ряде мест игнорируется.
- Не-текстовые сообщения получают ответ о том, что бот понимает только текст.
- env-админы в админ-панели защищены от изменения ролей, блокировки и удаления.
- `_refresh_admin_commands()` может не установить меню для конкретного админа, если тот ещё не начинал диалог с ботом; ошибка логируется как предупреждение.
- Callback-и памяти используют хэш текста факта, а не индекс, поэтому после удаления/добавления фактов кнопки не «съезжают» на чужой факт.
- В `handle_memory_callback` при отсутствии `callback.from_user` возвращается ошибка без обращения к `engine`.
- В текущем порядке проверок `handle_admin_callback` ветка `adm_user_del_*` стоит раньше `adm_user_del_confirm_*`; callback подтверждения удаления вида `adm_user_del_confirm_<id>` может перехватываться общей веткой удаления и не находить пользователя с ID `confirm_<id>`. Это стоит учитывать при доработке админ-панели.