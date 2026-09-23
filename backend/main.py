"""Точка входа backend: FastAPI, CORS, /health, /chat и WebSocket /ws/voice."""

import json
import uuid
import wave
from pathlib import Path
from typing import Literal

import numpy as np
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.brains.fast_local import OllamaUnavailableError, ask_fast
from app.brains.router import classify
from app.brains.smart_cloud import SmartApiNotConfiguredError, ask_smart
from app.voice.stt import transcribe
from app.voice.tts import synthesize

# Подхватываем backend/.env (OLLAMA_HOST, PIPER_VOICE_MODEL, SMART_*).
load_dotenv()

# Создаём приложение — базовый объект FastAPI для регистрации роутов.
app: FastAPI = FastAPI(title="Julie backend")

# Тип выбора мозга: быстрый локальный или умный облачный.
BrainName = Literal["fast", "smart"]

# Каталог временных wav (входные записи и ответы TTS), раздаётся как /audio.
AUDIO_DIR: Path = Path(__file__).resolve().parent / "tmp_audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/audio", StaticFiles(directory=str(AUDIO_DIR)), name="audio")

# Порог тишины: пиковая амплитуда float32-записи ниже него — на микрофон
# не попала речь. Без проверки whisper выдаёт галлюцинации на тишине
# (например, корейские фразы), и мозг отвечает на них всерьёз.
SILENCE_PEAK: float = 0.01


class ChatRequest(BaseModel):
    """Входной payload POST /chat: {"message": str, "force_brain"?: ...}."""

    message: str = Field(..., min_length=1, description="Сообщение пользователя")
    force_brain: BrainName | None = Field(
        None, description="Принудительно выбрать мозг вместо классификатора"
    )


class ChatResponse(BaseModel):
    """Выходной payload POST /chat: {"reply", "brain", "fallback_reason"}."""

    reply: str = Field(..., description="Ответ ассистента")
    brain: BrainName = Field(..., description="Мозг, который реально ответил")
    fallback_reason: str | None = Field(
        None, description="Причина отката на fast, если smart не сработал"
    )


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


async def run_brain_pipeline(message: str, force_brain: BrainName | None = None) -> ChatResponse:
    """Общий пайплайн текстового запроса: роутер → мозг → ответ.

    Общая для POST /chat и WebSocket /ws/voice.

    Вход: текст запроса и опциональный принудительный выбор мозга.
    Выход: ChatResponse (reply, brain, fallback_reason).
    Исключения:
        OllamaUnavailableError — локальная модель недоступна;
        SmartApiNotConfiguredError — smart недоступен (не должен выйти наружу:
        перехватывается здесь и превращается в fallback на fast).
    """
    brain: BrainName = force_brain if force_brain else classify(message)
    fallback_reason: str | None = None

    if brain == "smart":
        try:
            reply: str = await ask_smart(message)
        except SmartApiNotConfiguredError as error:
            # Заглушка smart-мозга: откатываемся на локальную модель.
            fallback_reason = str(error)
            brain = "fast"
            reply = await ask_fast(message)
    else:
        reply = await ask_fast(message)

    return ChatResponse(reply=reply, brain=brain, fallback_reason=fallback_reason)


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """Маршрутизирует сообщение по мозгам: fast (Ollama) или smart (заглушка).

    Вход: JSON {"message": "<текст>", "force_brain": "fast"|"smart"|null}.
    Выход: JSON {"reply": str, "brain": "fast"|"smart",
    "fallback_reason": str|null} со статусом 200;
    при недоступности Ollama — HTTP 503 {"detail": "<текст ошибки>"}.
    """
    try:
        return await run_brain_pipeline(request.message, request.force_brain)
    except OllamaUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except TimeoutError as error:
        # Ollama не ответила за отведённое время — шлём 504, а не 500.
        raise HTTPException(status_code=504, detail=str(error)) from error
    except RuntimeError as error:
        # Прочие ошибки мозга (HTTP-сбой Ollama) — шлём 502, а не 500.
        raise HTTPException(status_code=502, detail=str(error)) from error


def _write_pcm_wav(pcm_bytes: bytes, output_path: Path, sample_rate: int) -> None:
    """Собирает сырые mono float32 PCM-чанки в wav-файл (int16, little-endian).

    Вход: байты float32 LE (хвост < 4 байт отбрасывается), путь выхода,
    частота дискретизации 8000..192000 Гц.
    Выход: None; файл wav создан по output_path.
    Исключения:
        ValueError — невалидная частота дискретизации;
        OSError / wave.Error — не удалось записать файл.
    """
    if not 8000 <= sample_rate <= 192000:
        raise ValueError(f"Недопустимая частота дискретизации: {sample_rate}")

    usable = pcm_bytes[: len(pcm_bytes) // 4 * 4]
    samples = np.frombuffer(usable, dtype="<f4")
    pcm16 = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with wave.open(str(output_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm16.tobytes())
    except (wave.Error, OSError) as error:
        raise OSError(f"Не удалось записать wav {output_path}: {error}") from error


async def _send_ws(websocket: WebSocket, payload: dict[str, object]) -> None:
    """Отправляет JSON фронту по WebSocket, глуша ошибку закрытого сокета.

    Вход: сокет и словарь payload (кодируется в JSON, кириллица как есть).
    Выход: None; если соединение уже закрыто — сообщение не доставляется.
    """
    try:
        await websocket.send_text(json.dumps(payload, ensure_ascii=False))
    except RuntimeError:
        # Клиент уже отключился — отправлять некуда.
        pass


async def _send_ws_error(websocket: WebSocket, detail: str, text: str | None = None) -> None:
    """Отправляет фронту {"type": "error", "detail": ..., "text"?: ...}.

    Вход: сокет, текст ошибки и опциональный текст ответа (если TTS упал,
    но ответ мозга уже есть — покажем его текстом).
    Выход: None.
    """
    payload: dict[str, object] = {"type": "error", "detail": detail}
    if text is not None:
        payload["text"] = text
    await _send_ws(websocket, payload)


async def _handle_voice_stop(websocket: WebSocket, buffer: bytearray, sample_rate: int) -> None:
    """Прогоняет собранную запись: STT → мозг → TTS → ответ фронту.

    Вход: сокет, буфер сырых float32-чанков, частота дискретизации.
    Выход: None; фронту уходит {"type": "reply", "text", "audio_url",
    "transcript", "brain", "fallback_reason"} или {"type": "error", ...}.
    """
    if not buffer:
        await _send_ws_error(websocket, "Пустая запись: не получено ни одного аудиочанка")
        return

    # Проверка на тишину ДО распознавания: если пик ниже порога, речи не было
    # (отключённый микрофон, пустая запись) — не даём whisper галлюцинировать.
    samples: np.ndarray = np.frombuffer(bytes(buffer[: len(buffer) // 4 * 4]), dtype=np.float32)
    if samples.size == 0 or float(np.abs(samples).max()) < SILENCE_PEAK:
        await _send_ws_error(
            websocket,
            "Не расслышал речь: запись тихая — подойди ближе к микрофону "
            "и проверь, что микрофон не отключён в системе",
        )
        return

    token: str = uuid.uuid4().hex
    input_path: Path = AUDIO_DIR / f"in_{token}.wav"
    output_path: Path = AUDIO_DIR / f"out_{token}.wav"

    try:
        _write_pcm_wav(bytes(buffer), input_path, sample_rate)
    except (ValueError, OSError, wave.Error) as error:
        await _send_ws_error(websocket, f"Не удалось сохранить запись: {error}")
        return

    try:
        transcript: str = transcribe(str(input_path))
    except (FileNotFoundError, RuntimeError) as error:
        await _send_ws_error(websocket, f"STT: {error}")
        return

    if not transcript:
        await _send_ws_error(websocket, "Не разобрал речь — попробуй говорить громче и ближе к микрофону")
        return

    try:
        chat_response: ChatResponse = await run_brain_pipeline(transcript)
    except OllamaUnavailableError as error:
        await _send_ws_error(websocket, str(error))
        return
    except TimeoutError as error:
        await _send_ws_error(websocket, str(error))
        return
    except RuntimeError as error:
        # Прочие ошибки мозга (HTTP-сбой Ollama и т.п.) — фронту текстом.
        await _send_ws_error(websocket, str(error))
        return

    try:
        synthesize(chat_response.reply, str(output_path))
    except (FileNotFoundError, RuntimeError, ValueError, OSError) as error:
        # Ответ мозга есть — отдадим его текстом, без аудио.
        await _send_ws_error(websocket, f"TTS: {error}", text=chat_response.reply)
        return

    await _send_ws(
        websocket,
        {
            "type": "reply",
            "text": chat_response.reply,
            "audio_url": f"/audio/{output_path.name}",
            "transcript": transcript,
            "brain": chat_response.brain,
            "fallback_reason": chat_response.fallback_reason,
        },
    )


@app.websocket("/ws/voice")
async def voice_websocket(websocket: WebSocket) -> None:
    """Голосовой контур: чанки речи → STT → роутер/мозг → TTS → ответ.

    Вход (от фронта): {"type": "start", "sampleRate": int}, затем бинарные
    чанки mono float32 LE, затем {"type": "stop"}.
    Выход (фронту): {"type": "reply", "text", "audio_url", "transcript",
    "brain", "fallback_reason"} или {"type": "error", "detail", "text"?};
    после reply/error соединение закрывается.
    TODO: вместо ручного старта по хоткею в будущем нужен wake-word
    детектор (openwakeword, кастомная модель под "Джули"/"Джуди").
    """
    await websocket.accept()
    buffer = bytearray()
    sample_rate: int = 48000

    while True:
        message: dict[str, object] = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return

        chunk: bytes | None = message.get("bytes")  # type: ignore[assignment]
        if chunk:
            buffer.extend(chunk)
            continue

        text: str | None = message.get("text")  # type: ignore[assignment]
        if text is None:
            continue

        try:
            payload: dict[str, object] = json.loads(text)
        except json.JSONDecodeError as error:
            await _send_ws_error(websocket, f"Некорректный JSON от фронта: {error}")
            return

        payload_type = payload.get("type")
        if payload_type == "start":
            try:
                sample_rate = int(payload.get("sampleRate", 48000))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                await _send_ws_error(websocket, f"Некорректный sampleRate: {payload.get('sampleRate')}")
                return
        elif payload_type == "stop":
            await _handle_voice_stop(websocket, buffer, sample_rate)
            return
        else:
            await _send_ws_error(websocket, f"Неизвестный тип сообщения: {payload_type!r}")
            return
