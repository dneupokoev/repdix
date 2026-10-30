# app/core/user_manager.py
dv_file_version = '261030.06'
"""Управление пользователями repdix через config/users.yaml"""

import asyncio
import logging
import os
import re

import yaml

logger = logging.getLogger(__name__)


# Заголовок users.yaml (вынесен в константу — используется в _save_users_yaml)
_USERS_HEADER = (
    "# ==========================================================\n"
    "# База пользователей repdix\n"
    "# ==========================================================\n"
    "# Поля:\n"
    "#   id            — Telegram ID пользователя\n"
    "#   username      — Telegram логин (без @)\n"
    "#   full_name     — ФИО (вводится при регистрации, НЕ перезаписывается)\n"
    "#   telegram_name — Имя из Telegram-профиля (обновляется при /start)\n"
    "#   department    — Отдел / Должность (из регистрации)\n"
    "#   purpose       — Цель доступа (из регистрации)\n"
    "#   is_active     — true / false\n"
    "#   roles         — список ролей (admin, sales_manager...)\n"
    "#   comment       — Заметки администратора\n"
    "#   meta          — JSON с произвольными данными (db_login, branch_id...)\n"
    "# ==========================================================\n\n"
)


class UserManager:
    """Управляет пользователями через config/users.yaml с поддержкой Hot-Reload."""

    def __init__(self, users_yaml_path: str = "config/users.yaml") -> None:
        self.users_yaml_path = users_yaml_path
        self._users_cache: dict = {}
        self._users_mtime: float = 0.0
        self.admins = [
            a.strip() for a in os.environ.get("ADMIN_USERS", "").split(",") if a.strip()
        ]
        # Блокировка нужна, чтобы два админа одновременно не перезаписали файл
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Валидация
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_user_id(user_id: str) -> str | None:
        """Проверяет, что user_id — непустая строка из цифр.

        Returns:
            Нормализованный user_id (str) или None, если невалиден.
        """
        if user_id is None:
            return None
        uid = str(user_id).strip()
        if not uid or not re.fullmatch(r"\d+", uid):
            return None
        return uid

    # ------------------------------------------------------------------
    # Загрузка users.yaml (hot-reload по mtime)
    # ------------------------------------------------------------------
    def _load_users_yaml(self) -> dict:
        """Загружает users.yaml, если файл изменился.

        При ошибке чтения/парсинга возвращает ТЕКУЩИЙ кэш (не сбрасывает его).
        """
        if not os.path.exists(self.users_yaml_path):
            return self._users_cache

        try:
            current_mtime = os.path.getmtime(self.users_yaml_path)
            if current_mtime > self._users_mtime:
                with open(self.users_yaml_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}

                users_list = data.get("users", []) or []
                new_cache: dict = {}
                for u in users_list:
                    if not isinstance(u, dict) or "id" not in u:
                        continue
                    uid = str(u["id"])
                    if not uid:
                        continue
                    new_cache[uid] = u

                self._users_cache = new_cache
                self._users_mtime = current_mtime
                logger.info("users.yaml перезагружен. Пользователей: %d", len(self._users_cache))
        except Exception as e:
            logger.error("Ошибка чтения users.yaml: %s", e)
            # НЕ сбрасываем кэш при ошибке — иначе потеряем данные в памяти

        return self._users_cache

    # ------------------------------------------------------------------
    # Чтение
    # ------------------------------------------------------------------
    def get_user(self, user_id: str) -> dict | None:
        """Возвращает данные пользователя из кэша YAML."""
        users = self._load_users_yaml()
        uid = self._validate_user_id(user_id)
        if uid is None:
            return None
        return users.get(uid)

    def get_user_roles(self, user_id: str) -> list[str]:
        """Возвращает список ролей пользователя."""
        user_data = self.get_user(user_id)
        if user_data:
            roles = user_data.get("roles", [])
            return list(roles) if isinstance(roles, list) else []
        return []

    def is_user_active(self, user_id: str) -> bool:
        """True, если пользователь активен (is_active: True)."""
        user_data = self.get_user(user_id)
        if user_data:
            return bool(user_data.get("is_active", False))
        return False

    def is_admin(self, user_id: str) -> bool:
        """Проверяет env ADMIN_USERS или наличие роли 'admin' в YAML."""
        uid = str(user_id)
        if uid in self.admins:
            return True
        return "admin" in self.get_user_roles(user_id)

    def is_env_admin(self, user_id: str) -> bool:
        """Проверяет, что пользователь — админ ИМЕННО из .env (не из YAML).

        Нужно для защиты: add_user не должен перезаписывать env-админов.
        """
        return str(user_id) in self.admins

    # ------------------------------------------------------------------
    # Список пользователей (для админ-панели)
    # ------------------------------------------------------------------
    def list_users(self, exclude_admins: bool = True) -> list[dict]:
        """Возвращает список пользователей (dict).

        Args:
            exclude_admins: если True — исключает env-админов (из ADMIN_USERS).
                Админов из YAML (роль admin, но не в env) НЕ исключает —
                их можно редактировать.

        Returns:
            Список словарей, отсортированный по (is_active desc, full_name asc).
        """
        users = self._load_users_yaml()
        result: list[dict] = []
        for uid, u in users.items():
            if exclude_admins and uid in self.admins:
                continue
            result.append(u)

        # Сортировка: активные — выше, потом по ФИО
        def sort_key(u: dict):
            is_active = 0 if u.get("is_active") else 1
            name = str(u.get("full_name") or u.get("username") or u.get("id") or "")
            return (is_active, name.lower())

        result.sort(key=sort_key)
        return result

    def list_users_paginated(
        self,
        page: int,
        per_page: int = 10,
        exclude_admins: bool = True,
    ) -> tuple[list[dict], int]:
        """Возвращает страницу пользователей и общее количество.

        Returns:
            (users_on_page, total_users)
        """
        all_users = self.list_users(exclude_admins=exclude_admins)
        total = len(all_users)
        start = max(0, page) * per_page
        end = start + per_page
        return all_users[start:end], total

    def find_user(self, query: str) -> dict | None:
        """Ищет пользователя по ID или username (без @, регистронезависимо)."""
        if not query:
            return None
        q = str(query).strip().lstrip("@").lower()
        if not q:
            return None

        # 1) По ID (если цифры)
        if re.fullmatch(r"\d+", q):
            user = self.get_user(q)
            if user:
                return user

        # 2) По username
        users = self._load_users_yaml()
        for uid, u in users.items():
            uname = str(u.get("username") or "").lower()
            if uname and uname == q:
                return u

        return None

    # ------------------------------------------------------------------
    # Сохранение users.yaml (атомарное)
    # ------------------------------------------------------------------
    def _save_users_yaml(self, users_data: dict) -> bool:
        """Атомарно сохраняет словарь пользователей в YAML.

        Returns:
            True при успехе, False при ошибке (кэш при ошибке НЕ обновляется).
        """
        # Проверка целостности данных перед сохранением
        invalid_count = 0
        for uid, u in users_data.items():
            if not isinstance(u, dict) or "id" not in u:
                invalid_count += 1

        if invalid_count > 0:
            logger.warning(
                "_save_users_yaml: найдено %d некорректных записей (не dict или без id). "
                "Сохранение отменено для защиты от потери данных.", invalid_count,
            )
            return False

        users_list = list(users_data.values())
        tmp_path = self.users_yaml_path + ".tmp"

        try:
            # Создаём директорию, если её нет
            parent = os.path.dirname(self.users_yaml_path)
            if parent and not os.path.exists(parent):
                os.makedirs(parent, exist_ok=True)

            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(_USERS_HEADER)
                yaml.dump(
                    {"users": users_list},
                    f,
                    allow_unicode=True,
                    default_flow_style=False,
                    sort_keys=False,
                )
                try:
                    f.flush()
                    os.fsync(f.fileno())
                except OSError as e:
                    logger.warning("fsync users.yaml не удался: %s", e)

            os.replace(tmp_path, self.users_yaml_path)

            # Обновляем кэш ТОЛЬКО после успешной записи
            self._users_cache = users_data
            try:
                self._users_mtime = os.path.getmtime(self.users_yaml_path)
            except OSError:
                pass
            logger.info("users.yaml сохранён. Пользователей: %d", len(users_data))
            return True
        except Exception as e:
            logger.error("Ошибка записи users.yaml: %s", e)
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return False

    # ------------------------------------------------------------------
    # Изменение
    # ------------------------------------------------------------------
    async def add_user(
        self,
        user_id: str,
        username: str,
        full_name: str,
        department: str,
        purpose: str,
    ) -> bool:
        """Добавляет пользователя в YAML с is_active: False и roles: [].

        ВАЖНО: если user_id — админ (env или YAML с ролью admin), заявка
        НЕ перезаписывает запись. Иначе админ, случайно прошедший регистрацию,
        потерял бы права.

        Returns:
            True — добавлен/обновлён, False — не добавлен (админ или ошибка).
        """
        uid = self._validate_user_id(user_id)
        if uid is None:
            logger.warning("add_user: невалидный user_id=%r", user_id)
            return False

        async with self._lock:
            users = self._load_users_yaml().copy()

            # Защита админов
            if uid in self.admins:
                logger.warning(
                    "add_user: %s — env-админ, заявка проигнорирована "
                    "(права не перезаписываются)", uid,
                )
                return False
            existing = users.get(uid)
            if existing and "admin" in (existing.get("roles") or []):
                logger.warning(
                    "add_user: %s уже админ в YAML, заявка проигнорирована", uid,
                )
                return False

            users[uid] = {
                "id": uid,
                "username": username or "",
                "full_name": full_name or "",
                "telegram_name": "",
                "department": department or "",
                "purpose": purpose or "",
                "is_active": False,
                "roles": [],
                "comment": "",
                "meta": {},
            }
            return self._save_users_yaml(users)

    async def approve_user(
        self,
        user_id: str,
        role: str = "viewer",
        roles: list[str] | None = None,
    ) -> bool:
        """Ставит is_active: True и добавляет роль(и) пользователю.

        Args:
            user_id: Telegram ID.
            role: Одна роль (для обратной совместимости).
            roles: Список ролей (приоритетнее role). Если задан — используется он.

        Returns:
            True при успехе.
        """
        uid = self._validate_user_id(user_id)
        if uid is None:
            logger.warning("approve_user: невалидный user_id=%r", user_id)
            return False

        async with self._lock:
            users = self._load_users_yaml().copy()
            if uid not in users:
                logger.warning("Пользователь %s не найден для одобрения", uid)
                return False

            users[uid]["is_active"] = True
            current_roles = users[uid].get("roles", [])
            if not isinstance(current_roles, list):
                current_roles = []

            to_add = roles if roles is not None else [role]
            for r in to_add:
                if r and r not in current_roles:
                    current_roles.append(r)
            users[uid]["roles"] = current_roles

            # Предупреждение: viewer не имеет доступа к таблицам
            if current_roles == ["viewer"] or (
                "viewer" in current_roles and len(current_roles) == 1
            ):
                logger.warning(
                    "approve_user: %s одобрен с ролью viewer (нет доступа к таблицам). "
                    "Назначьте рабочую роль (sales_manager, role_c2t_queue_analysis и т.п.).",
                    uid,
                )

            return self._save_users_yaml(users)

    async def reject_user(self, user_id: str) -> bool:
        """Ставит is_active: False пользователю."""
        uid = self._validate_user_id(user_id)
        if uid is None:
            logger.warning("reject_user: невалидный user_id=%r", user_id)
            return False

        async with self._lock:
            users = self._load_users_yaml().copy()
            if uid not in users:
                logger.warning("Пользователь %s не найден для отклонения", uid)
                return False

            users[uid]["is_active"] = False
            return self._save_users_yaml(users)

    async def delete_user(self, user_id: str) -> bool:
        """Полностью удаляет пользователя из users.yaml.

        Используется при reset заявки — чтобы пользователь мог подать заявку
        заново. reject_user() этого не делает: is_active=False, но запись
        остаётся, и бот отвечает "заявка на рассмотрении".

        env-админов удалять НЕЛЬЗЯ — для них возвращаем False.

        Returns:
            True — удалён, False — не найден / админ / ошибка.
        """
        uid = self._validate_user_id(user_id)
        if uid is None:
            logger.warning("delete_user: невалидный user_id=%r", user_id)
            return False

        if uid in self.admins:
            logger.warning("delete_user: %s — env-админ, удаление запрещено", uid)
            return False

        async with self._lock:
            users = self._load_users_yaml().copy()
            if uid not in users:
                logger.warning("Пользователь %s не найден для удаления", uid)
                return False
            del users[uid]
            return self._save_users_yaml(users)

    async def update_user_profile(
        self, user_id: str, username: str, full_name: str,
    ) -> bool:
        """Обновляет username и telegram_name, НЕ трогая full_name.

        ВАЖНО (фикс бага 261028.05): раньше метод перезаписывал full_name
        на Telegram-имя при каждом /start — ФИО из регистрации терялось.
        Теперь full_name не трогается, а Telegram-имя пишется в telegram_name.

        Пустой username не перезаписывает сохранённый (в Telegram username
        опционален). Пустой full_name не перезаписывает telegram_name.
        """
        uid = self._validate_user_id(user_id)
        if uid is None:
            return False

        async with self._lock:
            users = self._load_users_yaml().copy()
            if uid not in users:
                return False

            changed = False
            if username and users[uid].get("username") != username:
                users[uid]["username"] = username
                changed = True
            if full_name and users[uid].get("telegram_name") != full_name:
                users[uid]["telegram_name"] = full_name
                changed = True

            if changed:
                return self._save_users_yaml(users)
            return True

    async def set_user_full_name(self, user_id: str, full_name: str) -> bool:
        """Устанавливает full_name (ФИО) вручную. Для админ-панели.

        В отличие от update_user_profile, здесь явно меняется full_name.
        """
        uid = self._validate_user_id(user_id)
        if uid is None or not full_name:
            return False

        async with self._lock:
            users = self._load_users_yaml().copy()
            if uid not in users:
                return False
            if users[uid].get("full_name") == full_name:
                return True
            users[uid]["full_name"] = full_name
            return self._save_users_yaml(users)

    async def set_user_roles(self, user_id: str, roles: list[str]) -> bool:
        """Заменяет список ролей пользователя (для админ-панели).

        В отличие от approve_user, здесь роли ЗАМЕНЯЮТСЯ, а не добавляются.
        Пустой список валиден (пользователь активен, но без доступа к таблицам).

        env-админов не трогаем: у них всегда должна быть роль admin.

        Returns:
            True при успехе.
        """
        uid = self._validate_user_id(user_id)
        if uid is None:
            return False
        if uid in self.admins:
            logger.warning("set_user_roles: %s — env-админ, изменение ролей запрещено", uid)
            return False

        # Нормализуем: убираем пустые/дубликаты, сохраняем порядок
        cleaned: list[str] = []
        for r in roles or []:
            r_str = str(r).strip()
            if r_str and r_str not in cleaned:
                cleaned.append(r_str)

        async with self._lock:
            users = self._load_users_yaml().copy()
            if uid not in users:
                return False
            users[uid]["roles"] = cleaned
            return self._save_users_yaml(users)

    async def toggle_user_active(self, user_id: str) -> tuple[bool, bool]:
        """Переключает is_active пользователя (для админ-панели).

        env-админов не трогаем: их нельзя заблокировать через бота.

        Returns:
            (ok, new_active):
              - ok: True при успехе.
              - new_active: новое значение is_active (False при ошибке).
        """
        uid = self._validate_user_id(user_id)
        if uid is None:
            return False, False
        if uid in self.admins:
            logger.warning("toggle_user_active: %s — env-админ, блокировка запрещена", uid)
            return False, False

        async with self._lock:
            users = self._load_users_yaml().copy()
            if uid not in users:
                return False, False
            new_active = not bool(users[uid].get("is_active", False))
            users[uid]["is_active"] = new_active
            ok = self._save_users_yaml(users)
            return ok, new_active

    async def update_user_notes(
        self,
        user_id: str,
        comment: str | None = None,
        meta: dict | None = None,
    ) -> bool:
        """Обновляет комментарий или JSON-метаданные пользователя."""
        uid = self._validate_user_id(user_id)
        if uid is None:
            return False

        async with self._lock:
            users = self._load_users_yaml().copy()
            if uid not in users:
                return False

            changed = False
            if comment is not None and users[uid].get("comment") != comment:
                users[uid]["comment"] = comment
                changed = True
            if meta is not None and users[uid].get("meta") != meta:
                users[uid]["meta"] = meta
                changed = True

            if changed:
                return self._save_users_yaml(users)
            return True

    async def sync_admins(self) -> None:
        """Синхронизирует админов из .env с users.yaml.

        Гарантирует, что все ID из .env имеют роль 'admin' и is_active: True.
        Принудительно восстанавливает права, если админ был понижен в YAML.
        При добавлении нового админа дополняет запись полями comment/meta/
        telegram_name (чтобы структура совпадала с add_user).
        """
        async with self._lock:
            users = self._load_users_yaml().copy()
            changed = False

            for admin_id in self.admins:
                uid = str(admin_id)
                if uid not in users:
                    users[uid] = {
                        "id": uid,
                        "username": "",
                        "full_name": f"Администратор {uid}",
                        "telegram_name": "",
                        "department": "Admin",
                        "purpose": "System Administrator",
                        "is_active": True,
                        "roles": ["admin"],
                        "comment": "",
                        "meta": {},
                    }
                    changed = True
                else:
                    if not users[uid].get("is_active") or "admin" not in (
                        users[uid].get("roles") or []
                    ):
                        users[uid]["is_active"] = True
                        roles = users[uid].get("roles") or []
                        if not isinstance(roles, list):
                            roles = []
                        if "admin" not in roles:
                            roles.append("admin")
                        users[uid]["roles"] = roles
                        changed = True

            if changed:
                if self._save_users_yaml(users):
                    logger.info("Администраторы синхронизированы с users.yaml")
                else:
                    logger.error("Не удалось синхронизировать администраторов")
