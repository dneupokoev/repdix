# app/core/technical_filter.py
dv_file_version = '261029.05'

import os
import re

DEFAULT_PREFIXES = ("technical_",)

def get_technical_prefixes() -> tuple[str, ...]:
    """Читает TECHNICAL_PREFIXES из env, иначе возвращает default."""
    raw = os.environ.get("TECHNICAL_PREFIXES", "")
    if raw.strip():
        return tuple(p.strip().lower() for p in raw.split(",") if p.strip())
    return DEFAULT_PREFIXES

def is_technical(name: str) -> bool:
    """Проверяет, начинается ли имя (таблица/колонка) с технического префикса."""
    if not name:
        return False
    lowered = name.lower()
    return any(lowered.startswith(p) for p in get_technical_prefixes())

def is_technical_qualified(name: str) -> bool:
    """Проверяет fully-qualified имя (schema.table / schema.table.column).

    Техничным считается имя, у которого ЛЮБАЯ часть (разделённая точками)
    начинается с технического префикса, например:
    "technical_schema.users", "db.technical_logs", "db.technical_foo.bar".
    Для короткого имени поведение совпадает с is_technical().
    """
    if not name:
        return False
    raw = str(name)
    if is_technical(raw):
        return True
    return any(is_technical(part) for part in raw.split("."))
