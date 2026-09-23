"""Облачный «умный» мозг — заглушка: реальный API пока не подключён."""

from typing import Final


class SmartApiNotConfiguredError(Exception):
    """Облачный API не настроен/не подключён на этом этапе."""


# Текст ошибки-заглушки, который уйдёт в fallback_reason.
STUB_MESSAGE: Final[str] = (
    "Облачный API пока не подключён — сейчас работаем только с локальным мозгом. "
    "Реализация будет добавлена отдельным шагом позже."
)


# TODO: реализовать вызов облачного API (Gemini/OpenRouter), когда будет решено, какой провайдер использовать
async def ask_smart(message: str, history: list[dict[str, str]] | None = None) -> str:
    """Заглушка «умного» мозга: всегда бросает исключение.

    Вход:
        message — текущее сообщение пользователя;
        history — предыдущие сообщения ([{"role": ..., "content": ...}]), may be None.
    Выход: никогда не возвращает значение.
    Исключения: SmartApiNotConfiguredError — всегда, пока реализация не добавлена.
    """
    raise SmartApiNotConfiguredError(STUB_MESSAGE)
