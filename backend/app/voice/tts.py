"""Текст в речь (TTS) через Piper; голос задаётся PIPER_VOICE_MODEL в .env."""

import os
import wave
from pathlib import Path

from dotenv import load_dotenv

# Подхватываем backend/.env при импорте модуля.
load_dotenv()

try:
    from piper import PiperVoice
except ImportError as error:
    raise RuntimeError(
        "piper-tts не установлен. Выполни: pip install piper-tts"
    ) from error

# Кэш загруженных голосов по пути к .onnx (onnxruntime-сессии дорогие).
_voices: dict[str, PiperVoice] = {}


def _get_voice() -> PiperVoice:
    """Загружает голос Piper по пути из PIPER_VOICE_MODEL.

    Вход: без параметров (читает PIPER_VOICE_MODEL из окружения/.env).
    Выход: экземпляр PiperVoice (кэшируется по пути).
    Исключения:
        RuntimeError — PIPER_VOICE_MODEL не задан или файл не загрузился;
        FileNotFoundError — файла .onnx нет по указанному пути.
    """
    model_path = os.getenv("PIPER_VOICE_MODEL", "").strip()
    if not model_path:
        raise RuntimeError(
            "PIPER_VOICE_MODEL не задан в .env. Скачай ru-голос командой: "
            "python -m piper.download_voices ru_RU-irina-medium "
            "--download-dir backend/data/piper — и пропиши в .env путь вида "
            "PIPER_VOICE_MODEL=backend/data/piper/ru_RU-irina-medium.onnx"
        )

    # Корень проекта (родитель backend/) — чтобы относительный путь из .env
    # находился из любого каталога запуска.
    project_root: Path = Path(__file__).resolve().parents[3]

    configured = Path(model_path)
    if configured.is_absolute():
        path = configured
        searched: list[Path] = [configured]
    else:
        searched = [Path.cwd() / configured, project_root / configured]
        path = next((option for option in searched if option.is_file()), configured)

    if not path.is_file():
        raise FileNotFoundError(
            f"Файл голоса Piper не найден: {model_path} "
            f"(искали в: {', '.join(str(option) for option in searched)}). "
            "Скачай голос: python -m piper.download_voices ru_RU-irina-medium "
            "--download-dir backend/data/piper — и обнови PIPER_VOICE_MODEL в .env"
        )

    cached = _voices.get(str(path))
    if cached is not None:
        return cached

    config_path = Path(f"{path}.json")
    try:
        voice = PiperVoice.load(
            str(path),
            config_path=str(config_path) if config_path.is_file() else None,
        )
    except (OSError, RuntimeError, ValueError) as error:
        raise RuntimeError(
            f"Не удалось загрузить голос Piper {model_path}: {error}. "
            "Проверь, что это валидный .onnx из piper.download_voices."
        ) from error

    _voices[str(path)] = voice
    return voice


def synthesize(text: str, output_path: str) -> str:
    """Генерирует wav-файл с озвучкой текста.

    Вход:
        text — текст для озвучки (непустой);
        output_path — путь будущего wav-файла (каталоги создаются).
    Выход: строка — тот же путь к созданному файлу.
    Исключения:
        ValueError — пустой текст;
        RuntimeError — PIPER_VOICE_MODEL не настроен или сбой синтеза;
        FileNotFoundError — файла голоса нет;
        OSError — не удалось записать выходной файл.
    """
    if not text.strip():
        raise ValueError("Пустой текст для синтеза речи")

    voice = _get_voice()
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        # wave.open в режиме записи сам выставляет заголовок wav.
        with wave.open(str(destination), "wb") as wav_file:
            voice.synthesize_wav(text, wav_file)
    except (wave.Error, OSError, RuntimeError, ValueError) as error:
        raise RuntimeError(
            f"Сбой синтеза речи для файла {output_path}: {error}"
        ) from error

    return output_path
