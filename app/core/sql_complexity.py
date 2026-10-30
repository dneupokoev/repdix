dv_file_version = '261030.03'
"""Проверка сложности SQL-запроса ДО выполнения.

Модуль закрывает класс запросов, которые формально безопасны (проходят
validator.validate_sql и rbac_guard.validate_sql), но потенциально
«вешают» БД или взрывают контекст:

- слишком много JOIN;
- JOIN без условия (декартово произведение);
- глубокая вложенность подзапросов;
- слишком много UNION.

check_complexity() вызывается ПОСЛЕ проверок безопасности (validator, RBAC),
но ДО db_manager.execute_query(). При превышении лимита бросает
SQLTooComplex — вызывающий код превращает это в понятный отказ (текст
исключения уже сформулирован для пользователя/LLM).

Fail-closed: если запрос не удалось разобрать sqlglot — это тоже
SQLTooComplex. Невалидный/обфусцированный SQL не должен доходить до БД.

Диалект sqlglot: "mysql" (обслуживает и MySQL, и MariaDB — отдельного
диалекта MariaDB в sqlglot нет). Неизвестный диалект отвергается
ValueError в _normalize_dialect.
"""

import logging

import sqlglot
import sqlglot.errors
from sqlglot import exp

logger = logging.getLogger(__name__)


# DBManager отдаёт типы "mysql"/"mariadb"; sqlglot обслуживает оба
# диалектом mysql (отдельного диалекта MariaDB нет).
_DIALECT_ALIASES: dict[str, str] = {
    "mysql": "mysql",
    "mariadb": "mysql",
}


class SQLTooComplex(Exception):
    """SQL отклонён проверкой сложности.

    Attributes:
        reason: Человекочитаемая причина отказа (её можно показать
            пользователю и передать LLM как результат инструмента).
    """

    reason: str

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _normalize_dialect(dialect: str) -> str:
    """Приводит имя диалекта к тому, что понимает sqlglot.

    Args:
        dialect: Имя диалекта, полученное от DBManager ("mysql"/"mariadb").

    Returns:
        Диалект sqlglot ("mysql").

    Raises:
        ValueError: если диалект не поддерживается.
    """
    key = (dialect or "mysql").strip().lower()
    if key not in _DIALECT_ALIASES:
        raise ValueError(f"Unsupported dialect: {dialect}")
    return _DIALECT_ALIASES[key]


def _max_subquery_depth(node: exp.Expression, depth: int = 0) -> int:
    """Максимальная глубина вложенности exp.Subquery в поддереве.

    Args:
        node: Корень поддерева.
        depth: Текущая глубина (сколько Subquery уже на пути от корня).

    Returns:
        Максимальная глубина Subquery, встреченная в поддереве.
    """
    if isinstance(node, exp.Subquery):
        depth += 1

    best = depth
    for child in node.iter_expressions():
        child_depth = _max_subquery_depth(child, depth)
        if child_depth > best:
            best = child_depth
    return best


def check_complexity(
    sql: str,
    dialect: str = "mysql",
    max_joins: int = 5,
    max_depth: int = 3,
    max_unions: int = 3,
) -> None:
    """Отклоняет слишком сложные SQL-запросы.

    Вызывать ПОСЛЕ validator.validate_sql() и rbac_guard.validate_sql(),
    но ДО db_manager.execute_query().

    Args:
        sql: SQL-запрос (только SELECT/WITH — гарантируется validator'ом).
        dialect: Диалект: "mysql" (MySQL/MariaDB). Другие диалекты
            не поддерживаются.
        max_joins: Максимально допустимое количество JOIN.
        max_depth: Максимальная глубина вложенности подзапросов.
        max_unions: Максимально допустимое количество UNION.

    Returns:
        None, если запрос в пределах лимитов.

    Raises:
        SQLTooComplex: если запрос не удалось разобрать (fail-closed) или
            превышен любой из лимитов. Текст исключения пригоден для
            показа пользователю.
        ValueError: если передан неподдерживаемый диалект.
    """
    dialect_name = _normalize_dialect(dialect)

    try:
        tree = sqlglot.parse_one(sql, read=dialect_name)
    except sqlglot.errors.ParseError as e:
        logger.warning(
            "SQL не разобран sqlglot (диалект %s): %s", dialect_name, e,
        )
        raise SQLTooComplex(f"Не удалось разобрать SQL: {e}") from e
    except ValueError as e:
        # Неизвестный диалект: конфигурационная ошибка, но fail-closed.
        logger.error("Неизвестный диалект SQL %r: %s", dialect_name, e)
        raise SQLTooComplex(f"Неизвестный диалект SQL: {dialect_name}") from e

    if tree is None:
        logger.warning("sqlglot вернул пустое дерево для SQL")
        raise SQLTooComplex(
            "Не удалось разобрать SQL: пустой результат парсинга."
        )

    # 1. Количество JOIN
    joins = list(tree.find_all(exp.Join))
    if len(joins) > max_joins:
        raise SQLTooComplex(
            f"Слишком много JOIN ({len(joins)} > {max_joins}). "
            "Уточните вопрос."
        )

    # 2. JOIN без ON/USING = декартово произведение
    for join in joins:
        if join.args.get("on") is None and join.args.get("using") is None:
            raise SQLTooComplex(
                "Обнаружено декартово произведение (JOIN без условия). "
                "Уточните вопрос."
            )

    # 3. Глубина вложенности подзапросов
    # RecursionError на экстремально глубоком дереве — не SQLTooComplex,
    # без этой обработки engine вернул бы «Ошибка LLM» (fail-open по UX).
    try:
        depth = _max_subquery_depth(tree)
    except RecursionError:
        raise SQLTooComplex(
            "Слишком глубокая вложенность подзапросов. Уточните вопрос."
        ) from None
    if depth > max_depth:
        raise SQLTooComplex(
            f"Слишком глубокая вложенность подзапросов "
            f"({depth} > {max_depth}). Уточните вопрос."
        )

    # 4. Количество UNION
    unions = list(tree.find_all(exp.Union))
    if len(unions) > max_unions:
        raise SQLTooComplex(
            f"Слишком много UNION ({len(unions)} > {max_unions}). "
            "Уточните вопрос."
        )

    logger.debug(
        "SQL complexity ok: joins=%d, depth=%d, unions=%d, dialect=%s",
        len(joins), depth, len(unions), dialect_name,
    )
