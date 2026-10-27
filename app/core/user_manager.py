# app/core/user_manager.py
dv_file_version = '261027.01'
import aiosqlite
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)


class UserManager:
    def __init__(self, db_path: str = "data/users.db"):
        self.db_path = db_path
        self.admins = [a.strip() for a in os.environ.get("ADMIN_USERS", "").split(",") if a.strip()]

    async def init_db(self):
        os.makedirs("data", exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    department TEXT,
                    purpose TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            await db.commit()

    async def get_user(self, user_id: str) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def get_user_status(self, user_id: str) -> str:
        user = await self.get_user(user_id)
        return user["status"] if user else "none"

    async def add_user(self, user_id: str, username: str, full_name: str, department: str, purpose: str):
        now = datetime.now().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO users 
                (user_id, username, full_name, department, purpose, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """, (user_id, username, full_name, department, purpose, now, now))
            await db.commit()

    async def update_status(self, user_id: str, status: str, admin_id: str = "") -> bool:
        """Обновляет статус. Возвращает False, если статус уже был изменен другим админом."""
        async with aiosqlite.connect(self.db_path) as db:
            # Проверка на гонку: обновляем только если статус всё ещё pending
            cursor = await db.execute(
                "UPDATE users SET status = ?, updated_at = ? WHERE user_id = ? AND status = 'pending'",
                (status, datetime.now().isoformat(), user_id)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def reset_user(self, user_id: str):
        """Полный сброс пользователя (удаление заявки)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            await db.commit()

    def is_admin(self, user_id: str) -> bool:
        return str(user_id) in self.admins