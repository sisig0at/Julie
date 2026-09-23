"""Речь в текст (STT) через faster-whisper на CPU."""

from pathlib import Path

from faster_whisper import WhisperModel

# Конфигурация модели: medium, CPU, 8-бит. Перешли с small по решению
# пользователя — medium заметно точнее на тихой/шумной записи микрофона ПК,
# скорость достаточно для коротких реплик голосового ассистента.
MODEL_SIZE: str = "medium"
DEVICE: str = "cpu"
COMPUTE_TYPE: str = "int8"

# Язык распознавания фиксируем: ассистент русскоязычный. Без фиксации
# whisper на шуме/тишине «уходит» в корейский или китайский, и мозг
# отвечает на галлюцинацию всерьёз.
WHISPER_LANGUAGE: str = "ru"

# NB: initial_prompt с фразами Джули сознательно НЕ используем: промпт-байас
# заставляет whisper подставлять его слова вместо сказанного пользователем
# (распознавал чужие фразы как «Чем могу помочь?»). Язык и так зафиксирован
# WHISPER_LANGUAGE, так что чистый декод важнее красивой пунктуации.

# Параметры VAD: порог мягче дефолта (0.5), поля вокруг речи шире — тихие
# начала/окончания фраз не должны обрезаться.
VAD_PARAMETERS: dict[str, int | float] = {
    "threshold": 0.4,
    "min_speech_duration_ms": 200,
    "speech_pad_ms": 300,
}

# Лениво загруженная модель (первый вызов может качать веса с HuggingFace).
_model: WhisperModel | None = None


def _get_model() -> WhisperModel:
    """Лениво загружает и возвращает единую модель faster-whisper.

    Вход: без параметров (модель и device заданы константами модуля).
    Выход: экземпляр WhisperModel.
    Исключения: RuntimeError — не удалось загрузить модель (нет сети,
    забит диск и т.п.) с инструкцией, что сделать.
    """
    global _model
    if _model is None:
        try:
            _model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)
        except (OSError, RuntimeError, ValueError) as error:
            raise RuntimeError(
                f"Не удалось загрузить модель faster-whisper '{MODEL_SIZE}' "
                f"({DEVICE}/{COMPUTE_TYPE}): {error}. Проверь интернет (первый запуск "
                "скачивает веса) и наличие места на диске."
            ) from error
    return _model


def transcribe(audio_path: str) -> str:
    """Распознаёт речь из wav-файла.

    Вход: путь к существующему wav-файлу.
    Выход: распознанный текст (строка, может быть пустой, если речи не было).
    Описание: сначала проход с VAD (отсекает шум), при пустом результате —
    повторный проход без VAD: Silero на записи с браузерной обработкой
    микрофона (NS/AGC) иногда выбрасывает короткую речь целиком.
    Исключения:
        FileNotFoundError — файла нет по указанному пути;
        RuntimeError — сбой загрузки модели или распознавания.
    """
    path = Path(audio_path)
    if not path.is_file():
        raise FileNotFoundError(f"Аудиофайл не найден: {audio_path}")

    model = _get_model()
    try:
        text: str = _recognize(model, str(path), use_vad=True)
        if not text:
            # VAD мог отбросить запись целиком — пробуем без него. Чистую
            # тишину сюда уже не пустит порог SILENCE_PEAK в main.py.
            text = _recognize(model, str(path), use_vad=False)
    except (OSError, RuntimeError, ValueError) as error:
        raise RuntimeError(f"Ошибка распознавания речи для {audio_path}: {error}") from error

    return text


def _recognize(model: WhisperModel, path: str, *, use_vad: bool) -> str:
    """Один проход распознавания: с VAD или без (фолбэк для записей без речи VAD).

    Вход: загруженная модель, путь к wav, флаг use_vad.
    Выход: распознанный текст (возможно пустой).
    Исключения: OSError, RuntimeError, ValueError — сбой распознавания.
    """
    segments, _info = model.transcribe(
        path,
        language=WHISPER_LANGUAGE,
        # VAD (Silero, встроен в faster-whisper) отсекает не-речь — меньше
        # галлюцинаций на шуме; но см. фолбэк в transcribe.
        vad_filter=use_vad,
        vad_parameters=VAD_PARAMETERS if use_vad else None,
        # Не переносим контекст между сегментами — классический источник
        # повторяющихся «хвостов» на русской записи.
        condition_on_previous_text=False,
    )
    return " ".join(segment.text.strip() for segment in segments).strip()
