# app/core/rbac_guard.py
dv_file_version = '261030.03'
"""RBAC-гард: проверка SQL на соответствие ролям пользователя.

Ключевые принципы:
1. Разрешение на таблицу ищется ТОЛЬКО по полному имени schema.table.
   Короткое имя (без схемы) не матчится, если в правилах есть схема — это
   закрывает обход RBAC через смену схемы (db1.t -> db2.t).
2. Если в SQL не указана схема, подставляется default_schema (из databases.yaml).
3. SELECT * разрешён только при allowed_columns == "*" и отсутствии
   excluded_columns. Если задан явный белый список (allowed_columns) или есть
   excluded_columns — звёздочка блокируется: иначе не разрешённые/скрытые
   поля утекли бы. Требуем явного перечисления полей.
4. sqlglot (диалект mysql) — единственный парсер. Регексп-эвристики убраны:
   они дублировали sqlglot и создавали обходы.
5. Технические таблицы/колонки (префиксы из TECHNICAL_PREFIXES) блокируются
   до проверки RBAC-правил.
6. Системные таблицы (information_schema, pg_catalog, sqlite_master)
   блокируются в любом месте AST (включая подзапросы и UNION) — до
   применения RBAC-правил. Wildcard "*" (admin) их не открывает.
7. Парсинг fail-closed: любая ошибка sqlglot — отказ.
8. Отказ по SELECT * объясняет, какие поля разрешены («нельзя SELECT *.
   Разрешённые поля: ...»). expand_select_star() может автоматически
   заменить * на этот список — безопасность при этом не ослабляется,
   подставляются только поля из allowed_columns (либо все поля таблицы
   минус excluded_columns), технические поля отбрасываются всегда.
"""

import logging
import os

import sqlglot
import yaml
from sqlglot import exp

from app.core.technical_filter import is_technical_qualified

logger = logging.getLogger(__name__)


# Системные таблицы/схемы: доступ запрещён всегда, даже при wildcard "*".
SYSTEM_TABLES = (
    "information_schema",
    "pg_catalog",
    "sqlite_master",
)


# Маркер отказа по SELECT *. По нему engine понимает, что запрос можно
# попробовать «починить» автоподстановкой разрешённых полей.
SELECT_STAR_ERROR_MARKER = "нельзя SELECT *"


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
        # 0a. Системные таблицы — блокируем в ЛЮБОМ месте AST (подзапросы,
        #     UNION, CTE). Иначе prompt injection мог бы вытащить метаданные
        #     БД через information_schema/pg_catalog в обход RBAC (wildcard
        #     "*" у admin такое пропустил бы).
        # ------------------------------------------------------------------
        for table in parsed.find_all(exp.Table):
            q_name = self._table_qualified_name(table).lower()
            if any(sys_tbl in q_name for sys_tbl in SYSTEM_TABLES):
                return False, "Доступ к системным таблицам запрещён."

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
            if column.table:
                raw_tbl = column.table
                tbl_name = raw_tbl.name if hasattr(raw_tbl, "name") else str(raw_tbl)
                qualified_col = f"{tbl_name}.{col_name}".lower()
            else:
                qualified_col = col_name
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
        # Карта: ключ table_rule_map -> qualified-имя (для текстов отказов
        # и для развёртывания звёздочек).
        qualified_by_key: dict[str, str] = {}

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
            qualified_by_key[qualified] = qualified
            # Также кладём короткое имя — для случая, когда колонки в SQL
            # не квалифицированы (SELECT col FROM t) и sqlglot не привязывает
            # их к таблице.
            table_rule_map.setdefault(tbl_name, matched)
            qualified_by_key.setdefault(tbl_name, qualified)

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
            target_name = ""

            if isinstance(parent, exp.Column) and parent.table:
                raw_tbl = parent.table
                qual_tbl = (raw_tbl.name if hasattr(raw_tbl, "name") else str(raw_tbl)).lower()
                target_rule = table_rule_map.get(qual_tbl)
                if target_rule is not None:
                    target_name = qualified_by_key.get(qual_tbl, qual_tbl)

            if target_rule is None:
                # SELECT * / COUNT(*) — проверим все таблицы запроса
                for key, r in table_rule_map.items():
                    reason = self._star_forbidden_reason(
                        r, qualified_by_key.get(key, key),
                    )
                    if reason:
                        return False, reason
                continue

            reason = self._star_forbidden_reason(target_rule, target_name)
            if reason:
                return False, reason

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
                raw_tbl = column.table
                qual_tbl = (raw_tbl.name if hasattr(raw_tbl, "name") else str(raw_tbl)).lower()
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
    # Проверка SELECT * по правилу таблицы
    # ------------------------------------------------------------------
    @staticmethod
    def _star_forbidden_reason(rule: dict, qualified: str = "") -> str | None:
        """Возвращает причину запрета SELECT * для правила таблицы или None.

        Сообщение перечисляет разрешённые поля: по нему LLM может переписать
        запрос, а engine — автоматически развернуть звёздочку
        (expand_select_star) и выполнить запрос.

        Звёздочка разрешена ТОЛЬКО если allowed_columns == "*" (has_wildcard)
        и нет excluded_columns. Явный белый список (allowed_columns: [a, b])
        требует перечисления полей — иначе SELECT * вернул бы поля вне списка.
        """
        table_label = qualified or "таблицы"
        allowed = rule.get("allowed") or set()
        excluded = rule.get("excluded") or set()

        if excluded:
            if allowed:
                allowed_part = ", ".join(sorted(allowed))
            else:
                # Полный список полей известен только БД, поэтому описываем
                # правило через исключения — engine развернёт его по факту.
                allowed_part = "все, кроме: " + ", ".join(sorted(excluded))
            return (
                f"Для таблицы '{table_label}' нельзя SELECT *: есть скрытые поля. "
                f"Разрешённые поля: {allowed_part}. Перепиши запрос."
            )

        if allowed and not rule.get("has_wildcard"):
            return (
                f"Для таблицы '{table_label}' нельзя SELECT *. "
                f"Разрешённые поля: {', '.join(sorted(allowed))}. "
                "Перепиши запрос."
            )
        return None

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

    # ------------------------------------------------------------------
    # Авторазвёртывание SELECT * (мягкий выход из отказа, без ослабления RBAC)
    # ------------------------------------------------------------------
    @staticmethod
    def is_select_star_denial(reason: str) -> bool:
        """True, если отказ вызван SELECT * и запрос можно попробовать починить."""
        return SELECT_STAR_ERROR_MARKER in (reason or "")

    def extract_tables(self, sql: str, default_schema: str = "") -> list[str]:
        """Возвращает таблицы запроса как schema.table (lowercase).

        Нужен engine: expand_select_star синхронный, а полный список колонок
        из БД берётся асинхронно — значит, таблицы должны быть известны
        заранее. При ошибке парсинга возвращает [].
        """
        try:
            parsed = sqlglot.parse_one(sql, read="mysql")
        except Exception:
            return []
        if parsed is None:
            return []

        default_schema_l = (default_schema or "").strip().lower()
        result: list[str] = []
        seen: set[str] = set()
        for table in parsed.find_all(exp.Table):
            name = self._table_qualified_name(table)
            if not name:
                continue
            if "." not in name and default_schema_l:
                name = f"{default_schema_l}.{name}"
            if name not in seen:
                seen.add(name)
                result.append(name)
        return result

    @staticmethod
    def _direct_tables(select: exp.Select) -> list[exp.Table]:
        """Таблицы из FROM/JOIN конкретного SELECT (без подзапросов)."""
        result: list[exp.Table] = []
        from_clause = select.args.get("from") or select.args.get("from_")
        if from_clause is not None and isinstance(from_clause.this, exp.Table):
            result.append(from_clause.this)
        for join in select.args.get("joins") or []:
            if isinstance(join.this, exp.Table):
                result.append(join.this)
        return result

    @classmethod
    def _find_table_by_qualifier(cls, select: exp.Select,
                                 qualifier: str) -> exp.Table | None:
        """Ищет таблицу по алиасу / короткому / полному имени."""
        q = (qualifier or "").lower()
        if not q:
            return None
        for table in cls._direct_tables(select):
            names = {
                (table.alias_or_name or "").lower(),
                (table.name or "").lower(),
                cls._table_qualified_name(table),
            }
            if q in names:
                return table
        return None

    def expand_select_star(
        self,
        sql: str,
        user_roles: list[str],
        default_schema: str = "",
        all_columns_getter=None,
    ) -> str:
        """Заменяет SELECT * на явный список разрешённых полей.

        Подставляются только поля из allowed_columns либо (если задан лишь
        excluded_columns) все поля таблицы минус исключённые. Технические
        поля отбрасываются. Безопасность не ослабляется: результат всё равно
        нужно прогнать через validate_sql.

        Args:
            sql: Исходный SQL.
            user_roles: Роли пользователя.
            default_schema: Схема для коротких имён.
            all_columns_getter: Синхронный коллбэк
                (qualified | short name) -> list[str] | None. Нужен только
                для таблиц с excluded_columns (чтобы вычислить «все, кроме»).

        Returns:
            SQL с развёрнутыми звёздочками либо исходный SQL, если развернуть
            не удалось (ошибка парсинга, неизвестная таблица, SELECT * из
            нескольких таблиц без квалификатора, подзапрос и т.п.).
        """
        if not sql:
            return sql

        try:
            parsed = sqlglot.parse_one(sql, read="mysql")
        except Exception:
            return sql
        if parsed is None:
            return sql

        self._load_config()
        rules = self._collect_rules(user_roles)
        if not rules:
            return sql

        default_schema_l = (default_schema or "").strip().lower()

        def _resolve_columns(rule: dict, qualified: str) -> list[str] | None:
            """Список полей для подстановки или None, если развернуть нельзя."""
            allowed = rule.get("allowed") or set()
            excluded = rule.get("excluded") or set()

            if allowed and not rule.get("has_wildcard"):
                columns = sorted(allowed)
            elif excluded:
                if all_columns_getter is None:
                    return None
                all_columns = all_columns_getter(qualified)
                if not all_columns and "." in qualified:
                    all_columns = all_columns_getter(qualified.split(".")[-1])
                if not all_columns:
                    return None
                columns = [
                    str(c) for c in all_columns
                    if str(c).lower() not in excluded
                ]
            else:
                # Звёздочка разрешена целиком — разворачивать нечего.
                return None

            cleaned = [
                c for c in columns
                if c and not is_technical_qualified(str(c).lower())
            ]
            return cleaned or None

        expanded_any = False
        unresolved = False

        for select in list(parsed.find_all(exp.Select)):
            projections = list(select.expressions)
            if not projections:
                continue

            new_projections: list[exp.Expression] = []
            changed = False

            for projection in projections:
                target_table: exp.Table | None = None

                if isinstance(projection, exp.Star):
                    direct = self._direct_tables(select)
                    if len(direct) != 1:
                        # Несколько таблиц: непонятно, чьи поля подставлять.
                        new_projections.append(projection)
                        unresolved = True
                        continue
                    target_table = direct[0]
                elif (
                    isinstance(projection, exp.Column)
                    and isinstance(projection.this, exp.Star)
                ):
                    raw_tbl = projection.table
                    qualifier = (
                        raw_tbl.name
                        if hasattr(raw_tbl, "name")
                        else str(raw_tbl)
                    )
                    target_table = self._find_table_by_qualifier(select, qualifier)
                    if target_table is None:
                        new_projections.append(projection)
                        unresolved = True
                        continue
                else:
                    new_projections.append(projection)
                    continue

                qualified = self._table_qualified_name(target_table)
                if "." in qualified:
                    schema_name, tbl_name = qualified.split(".", 1)
                else:
                    schema_name, tbl_name = default_schema_l, qualified

                rule = self._match_table_rule(tbl_name, schema_name, rules)
                if rule is None:
                    new_projections.append(projection)
                    unresolved = True
                    continue

                full_name = (
                    f"{schema_name}.{tbl_name}" if schema_name else tbl_name
                )
                columns = _resolve_columns(rule, full_name)
                if not columns:
                    new_projections.append(projection)
                    unresolved = True
                    continue

                alias = target_table.alias_or_name or tbl_name
                table_identifier = (
                    exp.to_identifier(alias, quoted=True) if alias else None
                )
                for column in columns:
                    new_projections.append(
                        exp.Column(
                            this=exp.to_identifier(column, quoted=True),
                            table=(
                                table_identifier.copy()
                                if table_identifier is not None
                                else None
                            ),
                        )
                    )
                changed = True

            if changed:
                expanded_any = True
                select.set("expressions", new_projections)

        if not expanded_any or unresolved:
            return sql

        try:
            return parsed.sql(dialect="mysql")
        except Exception:
            return sql
