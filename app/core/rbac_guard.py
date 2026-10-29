# app/core/rbac_guard.py
dv_file_version = '261029.07'
"""RBAC-гард: проверка SQL на соответствие ролям пользователя.

Ключевые принципы:
1. Разрешение на таблицу ищется ТОЛЬКО по полному имени schema.table.
   Короткое имя (без схемы) не матчится, если в правилах есть схема — это
   закрывает обход RBAC через смену схемы (db1.t -> db2.t).
2. Если в SQL не указана схема, подставляется default_schema (из databases.yaml).
3. SELECT * запрещён, если у таблицы есть excluded_columns: иначе исключённые
   поля утекли бы через звёздочку. Требуем явного перечисления полей.
4. sqlglot (диалект mysql) — единственный парсер. Регексп-эвристики убраны:
   они дублировали sqlglot и создавали обходы.
5. Технические таблицы/колонки (префиксы из TECHNICAL_PREFIXES) блокируются
   до проверки RBAC-правил.
"""

import logging
import os

import sqlglot
import yaml
from sqlglot import exp

from app.core.technical_filter import is_technical_qualified

logger = logging.getLogger(__name__)


class RBACGuard:
    """Проверяет SQL-запросы на соответствие ролям из config/roles.yaml."""

    def __init__(
        self,
        roles_config_path: str = "config/roles.yaml",
        technical_columns_getter=None,
    ):
        self.roles_config_path = roles_config_path
        self._config: dict = {}
        self._mtime: float = 0.0
        self._technical_columns_getter = technical_columns_getter

    # ------------------------------------------------------------------
    # Загрузка конфига (hot-reload)
    # ------------------------------------------------------------------
    def _load_config(self) -> None:
        """Загружает конфиг ролей с поддержкой hot-reload."""
        if not os.path.exists(self.roles_config_path):
            return
        try:
            current_mtime = os.path.getmtime(self.roles_config_path)
        except OSError:
            return
        if current_mtime > self._mtime:
            try:
                with open(self.roles_config_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                self._config = data.get("roles", {}) or {}
                self._mtime = current_mtime
                logger.debug("roles.yaml перезагружен. Ролей: %d", len(self._config))
            except Exception as e:
                logger.error("Ошибка чтения roles.yaml: %s", e)

    # ------------------------------------------------------------------
    # Сбор правил для набора ролей
    # ------------------------------------------------------------------
    def _collect_rules(self, user_roles: list[str]) -> dict[str, dict]:
        """Собирает правила доступа для набора ролей.

        Возвращает словарь:
            {
                "schema.table": {
                    "allowed": set[str],       # явно разрешённые поля
                    "excluded": set[str],      # всегда запрещённые поля
                    "has_wildcard": bool,      # True, если allowed_columns == "*"
                },
                "*": {...}                     # wildcard-правило (все таблицы)
            }

        Ключи нормализуются к нижнему регистру. Если в роли ключ задан без
        схемы, он трактуется как короткое имя и НЕ матчится к qualified-имени
        (см. validate_sql). Это сделано осознанно: правило без схемы не даёт
        доступа к одноимённой таблице в другой схеме.
        """
        rules: dict[str, dict] = {}

        for role_name in user_roles:
            role = self._config.get(role_name, {}) or {}
            tables = role.get("tables", {}) or {}

            for tbl_key, table_rules in tables.items():
                key = str(tbl_key).strip().lower()
                if not key:
                    continue

                entry = rules.setdefault(
                    key,
                    {"allowed": set(), "excluded": set(), "has_wildcard": False},
                )

                if not isinstance(table_rules, dict):
                    table_rules = {}

                allowed = table_rules.get("allowed_columns")
                if allowed == "*":
                    entry["has_wildcard"] = True
                elif isinstance(allowed, list):
                    entry["allowed"].update(
                        str(c).lower() for c in allowed if c
                    )

                excluded = table_rules.get("excluded_columns", [])
                if isinstance(excluded, list):
                    entry["excluded"].update(
                        str(c).lower() for c in excluded if c
                    )

        return rules

    # ------------------------------------------------------------------
    # Публичные хелперы (используются engine для построения промпта)
    # ------------------------------------------------------------------
    def get_allowed_schema_for_llm(self, user_roles: list[str]) -> str:
        """Формирует краткое текстовое описание доступных таблиц для промпта LLM.

        Примечание: engine._get_db_schema() строит более подробное описание
        (с полями из реальной БД). Этот метод оставлен для обратной
        совместимости и для случаев, когда нет доступа к DBManager.
        """
        self._load_config()
        rules = self._collect_rules(user_roles)
        if not rules:
            return "У вас нет доступа ни к одной таблице."

        lines = ["Доступные таблицы:"]
        for tbl, rule in rules.items():
            if tbl == "*":
                lines.append("- все таблицы (wildcard)")
            elif rule["has_wildcard"] and not rule["excluded"]:
                lines.append(f"- {tbl} (все поля)")
            elif rule["has_wildcard"]:
                excl = ", ".join(sorted(rule["excluded"]))
                lines.append(f"- {tbl} (все поля, кроме: {excl})")
            else:
                cols = ", ".join(sorted(rule["allowed"])) or "—"
                lines.append(f"- {tbl} (поля: {cols})")

        return "\n".join(lines)

    def get_allowed_tables(
        self,
        user_roles: list[str],
        schemas: list[str],
        default_schema: str = "",
        all_tables: list[str] | None = None,
    ) -> set[str]:
        """Возвращает множество разрешённых таблиц (в нижнем регистре).

        Qualified-ключи ("schema.table") возвращаются как есть. Короткие
        ключи нормализуются к "<default_schema>.<key>". Маркер wildcard
        возвращается как {"*"} — вызывающий код сам разворачивает его во все
        схемы из schemas.

        Правило вида "schema.*" (все таблицы схемы) разворачивается в
        конкретные fully-qualified имена ТОЛЬКО если передан all_tables.
        Без all_tables ключ "schema.*" возвращается как есть — обратная
        совместимость для вызывающего кода, который разворачивает его сам.

        Args:
            user_roles: Роли пользователя.
            schemas: Список доступных схем (для документации; wildcard
                разворачивает вызывающий код).
            default_schema: Схема для коротких ключей.
            all_tables: Реальный список таблиц из БД в формате
                "schema.table" (lower-case не обязателен). Нужен для
                разворачивания правил "schema.*" в конкретные таблицы:
                без него engine не смог бы сопоставить "db_analysis.*" с
                "db_analysis.pbi_centers_monthly". Если None — правила
                "schema.*" не разворачиваются (возвращаются как есть).
        """
        self._load_config()
        rules = self._collect_rules(user_roles)
        result: set[str] = set()
        default_schema_l = (default_schema or "").strip().lower()

        for key in rules:
            k = str(key).strip().lower()
            if not k:
                continue
            if k == "*":
                result.add("*")
            elif k.endswith(".*"):
                schema_prefix = k[:-2].strip()
                if all_tables is not None and schema_prefix:
                    prefix = f"{schema_prefix}."
                    for full in all_tables:
                        full_l = str(full).strip().lower()
                        if full_l.startswith(prefix):
                            result.add(full_l)
                else:
                    # Обратная совместимость: разворачивает вызывающий код.
                    result.add(k)
            elif "." in k:
                result.add(k)
            elif default_schema_l:
                result.add(f"{default_schema_l}.{k}")
            else:
                result.add(k)

        return result

    def get_allowed_schemas(
        self,
        user_roles: list[str],
        available_schemas: list[str],
        default_schema: str = "",
    ) -> set[str]:
        """Возвращает множество схем, к которым у ролей есть доступ.

        - wildcard-правило "*" — доступны все available_schemas;
        - qualified-ключ "schema.table" — доступна schema, если она есть
          в available_schemas;
        - короткий ключ — доступна default_schema (если она есть в списке).
        """
        self._load_config()
        rules = self._collect_rules(user_roles)
        available_l = {
            str(s).strip().lower() for s in (available_schemas or []) if s
        }

        if "*" in rules:
            return set(available_l)

        default_schema_l = (default_schema or "").strip().lower()
        result: set[str] = set()

        for key in rules:
            k = str(key).strip().lower()
            if not k or k == "*":
                continue
            if "." in k:
                schema_part = k.split(".", 1)[0].strip()
                if schema_part in available_l:
                    result.add(schema_part)
            elif default_schema_l and default_schema_l in available_l:
                result.add(default_schema_l)

        return result

    # ------------------------------------------------------------------
    # Основная валидация SQL
    # ------------------------------------------------------------------
    def validate_sql(
        self,
        sql: str,
        user_roles: list[str],
        default_schema: str = "",
    ) -> tuple[bool, str]:
        """Проверяет SQL на соответствие правам пользователя.

        Args:
            sql: SQL-запрос (только SELECT, проверка на безопасность — в validator.py).
            user_roles: Список ролей пользователя.
            default_schema: Схема, которая подставляется, если в SQL не указана.

        Returns:
            (True, sql) — если запрос разрешён.
            (False, reason) — если запрещён.
        """
        # Парсинг — ДО RBAC-правил: технические объекты должны блокироваться
        # раньше, чем сработает wildcard-правило "*" (роль admin).
        try:
            parsed = sqlglot.parse_one(sql, read="mysql")
        except Exception as e:
            return False, f"Ошибка парсинга SQL: {e}"

        if parsed is None:
            return False, "Пустой SQL-запрос."

        # ------------------------------------------------------------------
        # 0. Технические таблицы/колонки — блокируем ДО проверки RBAC.
        #    Иначе правило "*" (admin) открыло бы их.
        # ------------------------------------------------------------------
        for table in parsed.find_all(exp.Table):
            qualified_name = self._table_qualified_name(table)
            if not qualified_name:
                continue
            # Проверяем и schema, и table часть: schema.technical_x,
            # technical_schema.t и т.п. тоже должны блокироваться.
            if is_technical_qualified(qualified_name):
                return False, f"technical table access denied: {qualified_name}"

        for column in parsed.find_all(exp.Column):
            if isinstance(column.this, exp.Star):
                continue
            col_name = (column.name or "").lower()
            if not col_name:
                continue
            # Если колонка квалифицирована (alias.col / schema.table.col),
            # проверяем и полное имя, и часть после последней точки.
            qualified_col = (
                f"{column.table.name}.{col_name}".lower()
                if column.table
                else col_name
            )
            if is_technical_qualified(qualified_col):
                return False, f"technical column access denied: {col_name}"

        # SELECT * по таблице с техническими колонками — запрещён.
        # getter синхронный; в engine передаётся None (db_manager асинхронный),
        # реальная страховка — пост-фильтрация результата в engine.
        if self._technical_columns_getter is not None and any(parsed.find_all(exp.Star)):
            queried_tables = {
                self._table_qualified_name(t)
                for t in parsed.find_all(exp.Table)
                if t.name
            }
            for qualified_tbl in queried_tables:
                short_tbl = qualified_tbl.split(".")[-1]
                tech_cols: set[str] = set()
                # Getter может ожидать как qualified-, так и короткое имя —
                # опрашиваем оба варианта.
                for candidate in {qualified_tbl, short_tbl}:
                    try:
                        result = self._technical_columns_getter(candidate)
                    except Exception:
                        result = None
                    if result:
                        tech_cols.update(result)
                if tech_cols:
                    return (
                        False,
                        f"SELECT * forbidden: table {short_tbl} has technical columns",
                    )

        self._load_config()
        rules = self._collect_rules(user_roles)

        if not rules:
            return False, "У вас нет доступа ни к одной таблице."

        logger.debug(
            "RBAC validate: user_roles=%s, rules=%s, default_schema=%s",
            user_roles, list(rules.keys()), default_schema,
        )

        default_schema_l = (default_schema or "").strip().lower()

        # ------------------------------------------------------------------
        # 1. Проверка таблиц
        # ------------------------------------------------------------------
        # Карта: qualified-имя таблицы (schema.table) -> правило
        # Нужна, чтобы при проверке колонок знать, к какой таблице они относятся.
        table_rule_map: dict[str, dict] = {}

        for table in parsed.find_all(exp.Table):
            tbl_name = (table.name or "").lower()
            if not tbl_name:
                continue

            # table.db — это exp.Identifier; .name даёт строку
            raw_db = table.db
            if isinstance(raw_db, exp.Identifier):
                schema_name = (raw_db.name or "").lower()
            elif isinstance(raw_db, str):
                schema_name = raw_db.lower()
            else:
                schema_name = ""

            if not schema_name:
                schema_name = default_schema_l

            qualified = f"{schema_name}.{tbl_name}" if schema_name else tbl_name

            matched = self._match_table_rule(tbl_name, schema_name, rules)
            if matched is None:
                return False, f"Доступ к таблице '{qualified}' запрещен."

            table_rule_map[qualified] = matched
            # Также кладём короткое имя — для случая, когда колонки в SQL
            # не квалифицированы (SELECT col FROM t) и sqlglot не привязывает
            # их к таблице.
            table_rule_map.setdefault(tbl_name, matched)

        if not table_rule_map:
            return False, "В запросе не найдено ни одной таблицы."

        # ------------------------------------------------------------------
        # 2. Проверка звёздочек (SELECT *)
        # ------------------------------------------------------------------
        # Если у таблицы есть excluded_columns и разрешена звёздочка —
        # SELECT * утёк бы исключёнными полями. Запрещаем.
        for star in parsed.find_all(exp.Star):
            # Определяем, к какой таблице относится звёздочка.
            # exp.Star может быть в SELECT * (без квалификатора) или в t.*.
            # sqlglot: t.* — это Column(this=Star, table=Identifier).
            parent = star.parent
            target_rule = None

            if isinstance(parent, exp.Column) and parent.table:
                qual_tbl = (parent.table.name or "").lower()
                target_rule = table_rule_map.get(qual_tbl)

            if target_rule is None:
                # SELECT * — проверим все таблицы запроса
                for r in table_rule_map.values():
                    if r["excluded"]:
                        return (
                            False,
                            "SELECT * запрещён: у таблицы есть скрытые поля. "
                            "Перечислите разрешённые поля явно.",
                        )
                continue

            if target_rule["excluded"]:
                return (
                    False,
                    "SELECT * запрещён: у таблицы есть скрытые поля. "
                    "Перечислите разрешённые поля явно.",
                )

        # ------------------------------------------------------------------
        # 3. Проверка колонок
        # ------------------------------------------------------------------
        for column in parsed.find_all(exp.Column):
            # Пропускаем звёздочку — обработана выше
            if isinstance(column.this, exp.Star):
                continue

            col_name = (column.name or "").lower()
            if not col_name:
                continue

            # Определяем, к какой таблице относится колонка
            target_rule = None
            if column.table:
                qual_tbl = (column.table.name or "").lower()
                target_rule = table_rule_map.get(qual_tbl)

            if target_rule is None:
                # Колонка без квалификатора. Если в запросе одна таблица —
                # логично отнести её к ней. Если несколько — проверяем по
                # всем: колонка должна быть разрешена хотя бы в одной.
                # Но для безопасности требуем, чтобы она была разрешена
                # во ВСЕХ таблицах, где она потенциально может быть
                # (иначе можно обойти через одноимённое поле).
                candidates = []
                seen = set()
                for q, r in table_rule_map.items():
                    if q in seen:
                        continue
                    seen.add(q)
                    candidates.append(r)

                if not candidates:
                    return False, f"Не удалось определить таблицу для поля '{col_name}'."

                # Проверяем: колонка должна быть разрешена хотя бы в одной
                # таблице запроса. Если она excluded в любой — запрещаем.
                for r in candidates:
                    if col_name in r["excluded"]:
                        return (
                            False,
                            f"Доступ к полю '{col_name}' запрещен (скрытое поле).",
                        )

                allowed_anywhere = any(
                    r["has_wildcard"] or col_name in r["allowed"]
                    for r in candidates
                )
                if not allowed_anywhere:
                    return (
                        False,
                        f"Доступ к полю '{col_name}' запрещен "
                        f"(нет в allowed_columns).",
                    )
                continue

            # Колонка квалифицирована — проверяем строго по её таблице
            if col_name in target_rule["excluded"]:
                return (
                    False,
                    f"Доступ к полю '{col_name}' запрещен (скрытое поле).",
                )

            if not target_rule["has_wildcard"] and col_name not in target_rule["allowed"]:
                return (
                    False,
                    f"Доступ к полю '{col_name}' запрещен "
                    f"(нет в allowed_columns).",
                )

        return True, sql

    # ------------------------------------------------------------------
    # Нормализация имени таблицы
    # ------------------------------------------------------------------
    @staticmethod
    def _table_qualified_name(table: exp.Table) -> str:
        """Возвращает schema.table (lowercase) или короткое имя, если схемы нет."""
        tbl_name = (table.name or "").lower()
        raw_db = table.db
        if isinstance(raw_db, exp.Identifier):
            schema_name = (raw_db.name or "").lower()
        elif isinstance(raw_db, str):
            schema_name = raw_db.lower()
        else:
            schema_name = ""
        return f"{schema_name}.{tbl_name}" if schema_name else tbl_name

    # ------------------------------------------------------------------
    # Поиск правила для таблицы
    # ------------------------------------------------------------------
    def _match_table_rule(
        self,
        tbl_name: str,
        schema_name: str,
        rules: dict[str, dict],
    ) -> dict | None:
        """Ищет правило для таблицы.

        Порядок (строгий):
          1. Точное qualified-совпадение schema.table.
          2. Wildcard "*" (все таблицы).
          3. Wildcard схемы "schema.*" (все таблицы схемы) — если схема
             известна и такое правило задано в roles.yaml.
          4. Правило без схемы (короткое имя) — только если schema_name пуст.
             Это осознанное ограничение: правило без схемы не даёт доступа к
             одноимённой таблице в другой схеме.

        Возвращает правило или None.
        """
        qualified = f"{schema_name}.{tbl_name}" if schema_name else tbl_name

        # 1. Точное qualified-совпадение
        if qualified in rules:
            return rules[qualified]

        # 2. Wildcard
        if "*" in rules:
            return rules["*"]

        # 3. Wildcard схемы: "schema.*" применяется ко всем таблицам схемы
        if schema_name and f"{schema_name}.*" in rules:
            return rules[f"{schema_name}.*"]

        # 4. Короткое имя — только если схема не указана
        if not schema_name and tbl_name in rules:
            return rules[tbl_name]

        return None
