dv_file_version = '261030.01'
"""Тесты санитизации HTML в ответах LLM.

Проверяем, что:
- html.escape() превращает <script> в &lt;script&gt;;
- sanitize_telegram_html() сохраняет безопасные теги Telegram и
  экранирует всё остальное (script/img/атрибуты/javascript:-ссылки).
"""

import html

from app.interfaces.telegram_adapter import sanitize_telegram_html


def test_html_escape_escapes_script_tag():
    """Базовое свойство html.escape, на котором построен санитайзер."""
    assert html.escape("<script>alert(1)</script>") == (
        "&lt;script&gt;alert(1)&lt;/script&gt;"
    )


def test_script_tag_is_escaped():
    result = sanitize_telegram_html("<script>alert('x')</script>")
    assert "<script" not in result
    assert "</script>" not in result
    assert "&lt;script&gt;" in result


def test_plain_text_untouched():
    assert sanitize_telegram_html("Просто текст") == "Просто текст"


def test_empty_text():
    assert sanitize_telegram_html("") == ""


def test_safe_tags_are_preserved():
    src = "<b>Жирный</b>, <i>курсив</i>, <code>select 1</code>"
    assert sanitize_telegram_html(src) == src


def test_pre_block_is_preserved():
    src = "<pre>SELECT * FROM t;</pre>"
    assert sanitize_telegram_html(src) == src


def test_attributes_are_dropped():
    result = sanitize_telegram_html('<b onclick="evil()">x</b>')
    assert "onclick" not in result
    assert result == "<b>x</b>"


def test_img_is_escaped():
    result = sanitize_telegram_html('<img src="http://evil/x.png">')
    assert "<img" not in result
    assert "&lt;img" in result


def test_a_href_http_allowed():
    src = '<a href="https://example.com">ссылка</a>'
    assert sanitize_telegram_html(src) == src


def test_a_href_javascript_rejected():
    result = sanitize_telegram_html('<a href="javascript:alert(1)">клик</a>')
    assert '<a href="javascript:' not in result
    assert "&lt;a href=" in result
