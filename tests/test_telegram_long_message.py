"""Тесты нарезки длинных сообщений Telegram (_split_long_text).

Функция вынесена из TelegramAdapter._send_long_message и не зависит от
aiogram-объектов, поэтому тесты не поднимают Bot и не требуют
TELEGRAM_TOKEN в окружении.
"""

from app.interfaces.telegram_adapter import _split_long_text

MAX_LENGTH = 4000


def _assert_no_broken_tag(parts: list[str]) -> None:
    """Проверяет, что ни одна часть не обрывается внутри HTML-тега."""
    for part in parts:
        assert part.count("<b>") == part.count("</b>"), (
            f"Непарный <b> в части: {part[:100]!r}…"
        )
        if "<" in part:
            assert part.rfind("<") < part.rfind(">"), (
                f"Часть обрывается внутри тега: {part[-100:]!r}"
            )


def test_short_text_single_part() -> None:
    assert _split_long_text("hello", MAX_LENGTH) == ["hello"]


def test_split_by_newline() -> None:
    text = "a" * 3000 + "\n" + "b" * 3000

    parts = _split_long_text(text, MAX_LENGTH)

    assert len(parts) == 2
    assert parts[0] == "a" * 3000      # перевод строки в часть не попал
    assert parts[1] == "b" * 3000


def test_split_by_space_no_newline() -> None:
    text = "a" * 3000 + " " + "b" * 3000

    parts = _split_long_text(text, MAX_LENGTH)

    assert len(parts) == 2
    assert parts[0] == "a" * 3000      # разрез по пробелу, пробел отброшен
    assert parts[1] == "b" * 3000


def test_no_split_inside_html_tag() -> None:
    text = "a" * 3999 + "<b>x</b>"

    parts = _split_long_text(text, MAX_LENGTH)

    assert all(len(p) <= MAX_LENGTH for p in parts)
    _assert_no_broken_tag(parts)
    assert "".join(parts) == text      # ничего не потеряно


def test_hard_split_when_no_spaces() -> None:
    text = "a" * 5000                  # ни переводов строки, ни пробелов

    parts = _split_long_text(text, MAX_LENGTH)

    assert len(parts) == 2
    assert len(parts[0]) == MAX_LENGTH
    assert len(parts[1]) == 1000
    assert "".join(parts) == text
