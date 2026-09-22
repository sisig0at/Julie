"""Точка входа backend: FastAPI-приложение с CORS и эндпоинтом /health."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Создаём приложение — базовый объект FastAPI для регистрации роутов.
app: FastAPI = FastAPI(title="Julie backend")

# Разрешаем запросы только с адреса Vite dev-сервера (порт 5173).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    """Проверка живости backend.

    Вход: без параметров.
    Выход: JSON {"status": "ok"} со статусом 200.
    """
    return {"status": "ok"}
