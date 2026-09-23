"""Быстрый локальный «мозг»: обращение к Ollama с моделью qwen2.5:7b-instruct."""

import os

import httpx

# Дефолтный адрес Ollama, если переменная окружения OLLAMA_HOST не задана.
DEFAULT_OLLAMA_HOST: str = "http://localhost:11434"

# Имя локальной модели в Ollama.
MODEL_NAME: str = "qwen2.5:7b-instruct"

# Таймаут HTTP-запроса к Ollama, секунды.
TIMEOUT_SECONDS: float = 30.0

# Системный промпт: без него qwen на непонятных/обрезанных входах уезжает
# с русского (китайский, корейский). Ещё ограничиваем длину — это голосовой
# ассистент, длинные ответы в TTS слушать мука.
SYSTEM_PROMPT: str = (
    "Ты — Джули, русскоязычный голосовой ассистент. "
    "Всегда отвечай ТОЛЬКО на русском языке, ни на каком другом. "
    "Отвечай кратко: 1-3 предложения, без списков. "
    "Если вопрос непонятен или запись распознана неточно — вежливо попроси повторить."
)


class OllamaUnavailableError(Exception):
    """Ollama недоступна: сервер не отвечает по адресу host.

    Аргументы/атрибуты: текст ошибки с инструкцией по запуску.
    """


def get_ollama_host() -> str:
    """Возвращает адрес Ollama.

    Вход: без параметров (читает переменную окружения OLLAMA_HOST).
    Выход: строка host без завершающего слэша; если переменная не задана
    или пустая (например "OLLAMA_HOST=" в .env) — DEFAULT_OLLAMA_HOST.
    """
    host: str = os.getenv("OLLAMA_HOST", "").strip()
    if not host:
        return DEFAULT_OLLAMA_HOST
    return host.rstrip("/")


async def ask_fast(message: str, history: list[dict[str, str]] | None = None) -> str:
    """Отправляет сообщение в Ollama и возвращает ответ модели.

    Вход:
        message — текущее сообщение пользователя;
        history — предыдущие сообщения в формате [{"role": ..., "content": ...}],
        может быть None (system-промпт SYSTEM_PROMPT добавляется автоматически).
    Выход: строка — поле message.content из ответа Ollama.
    Исключения:
        OllamaUnavailableError — Ollama не отвечает по адресу host;
        TimeoutError — Ollama не ответила за TIMEOUT_SECONDS;
        RuntimeError — Ollama вернула ошибку HTTP или неожиданный формат JSON.
    """
    host: str = get_ollama_host()
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history is not None:
        # Не дублируем system, если он уже есть в переданной истории.
        messages.extend(entry for entry in history if entry.get("role") != "system")
    messages.append({"role": "user", "content": message})

    payload: dict[str, object] = {
        "model": MODEL_NAME,
        "messages": messages,
        "stream": False,
    }

    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        try:
            response: httpx.Response = await client.post(f"{host}/api/chat", json=payload)
        except httpx.ConnectError as error:
            raise OllamaUnavailableError(
                f"Ollama не запущена на {host}. "
                "Запусти `ollama serve` и убедись, что модель "
                f"{MODEL_NAME} скачана (`ollama pull {MODEL_NAME}`)."
            ) from error
        except httpx.TimeoutException as error:
            raise TimeoutError(
                f"Ollama не ответила за {TIMEOUT_SECONDS} с на {host}. "
                "Проверь, что модель загружена (`ollama list`), и повтори запрос."
            ) from error
        except httpx.TransportError as error:
            # Остальные сетевые ошибки (DNS, сброс соединения и т.п.).
            raise RuntimeError(f"Сетевая ошибка при обращении к Ollama на {host}: {error}") from error

    if response.is_error:
        raise RuntimeError(
            f"Ollama вернула HTTP {response.status_code} на {host}/api/chat: {response.text}"
        )

    try:
        data: dict[str, object] = response.json()
        message_obj: dict[str, str] = data["message"]  # type: ignore[assignment]
        content: str = message_obj["content"]
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError(
            f"Неожиданный формат ответа Ollama на {host}/api/chat: {response.text}"
        ) from error

    return content
