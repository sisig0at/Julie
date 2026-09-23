"""Роутер: выбирает быстрый (локальный) или умный (облачный) мозг по тексту."""

from typing import Literal

# Слова-триггеры, которые переводят запрос в «умный» мозг.
SMART_KEYWORDS: tuple[str, ...] = (
    "объясни",
    "почему",
    "спланируй",
    "план",
    "напиши код",
    "код",
    "сравни",
    "проанализируй",
)

# Порог длины сообщения, начиная с которого нужен «умный» мозг.
LONG_MESSAGE_THRESHOLD: int = 200


def classify(message: str) -> Literal["fast", "smart"]:
    """Определяет, каким мозгом обрабатывать сообщение.

    Вход: текст сообщения пользователя.
    Выход: "smart" — если длиннее 200 символов или содержит триггер-слово
    (без учёта регистра), иначе "fast".
    """
    if len(message) > LONG_MESSAGE_THRESHOLD:
        return "smart"

    lowered: str = message.lower()
    if any(keyword in lowered for keyword in SMART_KEYWORDS):
        return "smart"

    return "fast"
