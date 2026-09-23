import { useCallback, useEffect, useRef, useState, type ReactElement } from "react";
import "./App.css";
import AssistantOrb, { type OrbState } from "./components/AssistantOrb";
import ChatPanel, { type ChatMessage } from "./components/ChatPanel";

// Адреса backend.
const API_BASE: string = "http://localhost:8000";
const VOICE_WS_URL: string = "ws://localhost:8000/ws/voice";

// Русские подписи статусов под орбом (совпадают с state-пропсами).
const STATE_LABELS: Record<OrbState, string> = {
    idle: "",
    listening: "слушаю",
    thinking: "думаю",
    speaking: "говорю",
};

/** Ответ backend на POST /chat. */
type ChatResponse = {
    reply: string;
    brain: "fast" | "smart";
    fallback_reason: string | null;
};

/** Ответ backend с полем detail (FastAPI HTTPException). */
type ErrorDetail = {
    detail: string;
};

/** Успешный ответ голосового контура по WebSocket. */
type VoiceReply = {
    type: "reply";
    text: string;
    audio_url: string;
    transcript: string;
    brain: "fast" | "smart";
    fallback_reason: string | null;
};

/** Ошибка голосового контура по WebSocket (text — ответ без озвучки). */
type VoiceError = {
    type: "error";
    detail: string;
    text?: string;
};

/**
 * Корневой UI: орб + выезжающий чат + голосовой контур по Ctrl+Space.
 *
 * Вход: без параметров.
 * Выход: JSX оверлея.
 */
function App(): ReactElement {
    const [chatOpen, setChatOpen] = useState<boolean>(false);
    const [orbState, setOrbState] = useState<OrbState>("idle");
    const [ttsAudio, setTtsAudio] = useState<HTMLAudioElement | null>(null);
    // Состояние чата живёт здесь, чтобы голосовые сообщения падали в тот же список.
    const [messages, setMessages] = useState<ChatMessage[]>([]);
    const [pending, setPending] = useState<boolean>(false);

    // Ресурсы текущей записи (нужны в колбэках без зависимостей).
    const recordingRef = useRef<boolean>(false);
    // Старт записи ещё в полёте (async getUserMedia) — защищает от двойного старта.
    const startingRef = useRef<boolean>(false);
    // Нажатие хоткея во время старта — остановиться сразу после готовности.
    const pendingStopRef = useRef<boolean>(false);
    const audioContextRef = useRef<AudioContext | null>(null);
    const streamRef = useRef<MediaStream | null>(null);
    const wsRef = useRef<WebSocket | null>(null);

    /**
     * Добавляет сообщение в чат.
     *
     * Вход: сообщение без текстовых полей-опций.
     * Выход: None (обновление состояния).
     */
    const pushMessage = useCallback((message: ChatMessage): void => {
        setMessages((prev: ChatMessage[]) => [...prev, message]);
    }, []);

    /**
     * Отправляет текстовое сообщение в POST /chat (логика этапа 1/2).
     *
     * Вход: текст пользователя.
     * Выход: None; при ошибке — системное сообщение в чате.
     */
    const sendChat = useCallback(
        async (text: string): Promise<void> => {
            const trimmed: string = text.trim();
            if (trimmed === "" || pending) {
                return;
            }

            setPending(true);
            pushMessage({ role: "user", text: trimmed });

            try {
                const response: Response = await fetch(`${API_BASE}/chat`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ message: trimmed }),
                });

                if (!response.ok) {
                    const detail: string = await response
                        .json()
                        .then((data: ErrorDetail) => data.detail)
                        .catch(() => `Ошибка HTTP ${response.status}`);
                    pushMessage({ role: "system", text: detail });
                    return;
                }

                const data: ChatResponse = await response.json();
                pushMessage({
                    role: "assistant",
                    text: data.reply,
                    brain: data.brain,
                    fallbackReason: data.fallback_reason ?? undefined,
                });
            } catch (error: unknown) {
                const message: string = error instanceof Error ? error.message : String(error);
                pushMessage({ role: "system", text: `Не удалось связаться с backend: ${message}` });
            } finally {
                setPending(false);
            }
        },
        [pending, pushMessage],
    );

    /**
     * Освобождает ресурсы записи (микрофон, AudioContext, сокет).
     *
     * Вход: без параметров (работает по ref'ам).
     * Выход: None.
     */
    const releaseRecordingResources = useCallback((): void => {
        streamRef.current?.getTracks().forEach((track: MediaStreamTrack) => track.stop());
        streamRef.current = null;
        void audioContextRef.current?.close().catch((error: unknown) => {
            console.error("Не удалось закрыть AudioContext записи:", error);
        });
        audioContextRef.current = null;
        recordingRef.current = false;
        startingRef.current = false;
        pendingStopRef.current = false;
    }, []);

    /**
     * Обрабатывает сообщения от бэкенда по WebSocket голосового контура.
     *
     * Вход: текстовое JSON-сообщение от backend.
     * Выход: None; reply → реплики в чат + озвучка, error → системное сообщение.
     */
    const handleVoiceMessage = useCallback(
        (raw: string): void => {
            let payload: VoiceReply | VoiceError;
            try {
                payload = JSON.parse(raw) as VoiceReply | VoiceError;
            } catch (error) {
                const message: string = error instanceof Error ? error.message : String(error);
                pushMessage({ role: "system", text: `Плохой ответ голосового контура: ${message}` });
                setOrbState("idle");
                return;
            }

            if (payload.type === "reply") {
                pushMessage({ role: "user", text: `🎙 ${payload.transcript}` });
                pushMessage({
                    role: "assistant",
                    text: payload.text,
                    brain: payload.brain,
                    fallbackReason: payload.fallback_reason ?? undefined,
                });

                // crossOrigin ДО назначения src: страница (localhost:5173) и
                // аудио (localhost:8000) — разные origin'ы. Без CORS-режима
                // MediaElementSource в орбе обнуляет звук (приватность браузера)
                // — «ГОВОРЮ» горит, а колонки молчат.
                const audio = new Audio();
                audio.crossOrigin = "anonymous";
                audio.src = `${API_BASE}${payload.audio_url}`;
                setTtsAudio(audio);
                setOrbState("speaking");
                audio.addEventListener(
                    "ended",
                    () => {
                        setTtsAudio(null);
                        setOrbState("idle");
                    },
                    { once: true },
                );
                audio.play().catch((error: unknown) => {
                    const message: string = error instanceof Error ? error.message : String(error);
                    pushMessage({ role: "system", text: `Не удалось проиграть ответ: ${message}` });
                    setTtsAudio(null);
                    setOrbState("idle");
                });
                return;
            }

            // type === "error"
            pushMessage({ role: "system", text: payload.detail });
            if (payload.text) {
                // TTS упал, но ответ мозга есть — покажем его текстом.
                pushMessage({ role: "assistant", text: payload.text });
            }
            setOrbState("idle");
        },
        [pushMessage],
    );

    /**
     * Останавливает запись: шлёт "stop", освобождает микрофон, ждёт ответ.
     *
     * Вход: без параметров.
     * Выход: None; состояние орба → thinking (если stop реально ушёл),
     * иначе → idle.
     */
    const stopRecording = useCallback((): void => {
        const socket = wsRef.current;
        let awaitingReply: boolean = false;

        if (socket !== null) {
            if (socket.readyState === WebSocket.CONNECTING) {
                // Сокет ещё открывается: onopen отправит "start", а мы добавим
                // "stop" следом (обработчики вызываются в порядке регистрации).
                socket.addEventListener(
                    "open",
                    () => {
                        socket.send(JSON.stringify({ type: "stop" }));
                    },
                    { once: true },
                );
                awaitingReply = true;
            } else if (socket.readyState === WebSocket.OPEN) {
                socket.send(JSON.stringify({ type: "stop" }));
                awaitingReply = true;
            }
            // CLOSING/CLOSED — отправлять некуда: ответ уже пришёл или был сбой.
        }

        releaseRecordingResources();
        setOrbState(awaitingReply ? "thinking" : "idle");
    }, [releaseRecordingResources]);

    /**
     * Начинает запись микрофона и стриминг PCM по WebSocket.
     *
     * Вход: без параметров.
     * Выход: None (async); при отказе доступа к микрофону — системное сообщение.
     */
    const startRecording = useCallback(async (): Promise<void> => {
        // Защита от двойного старта: пока старт в полёте, второй Ctrl+Space
        // не создаёт параллельный сокет/микрофон, а планирует остановку.
        startingRef.current = true;
        pendingStopRef.current = false;
        // Статус сразу, не дожидаясь async-готовности — хоткей видно сразу.
        setOrbState("listening");

        let stream: MediaStream;
        try {
            // Встроенные обработчики Chromium против тихого/шумного входа ПК:
            // подавление эха, шумодав, авто-громкость. Без ручного буста —
            // он бы давал клиппинг вместо чистой речи.
            stream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true,
                },
            });
        } catch (error: unknown) {
            const message: string = error instanceof Error ? error.message : String(error);
            pushMessage({ role: "system", text: `Нет доступа к микрофону: ${message}` });
            setOrbState("idle");
            startingRef.current = false;
            return;
        }

        const context = new AudioContext();
        if (context.state === "suspended") {
            // Без gesture от глобального хоткея контекст может стартовать
            // suspended — тогда onaudioprocess не срабатывает и чанков нет.
            await context.resume().catch((error: unknown) => {
                const message: string = error instanceof Error ? error.message : String(error);
                pushMessage({ role: "system", text: `AudioContext не запустился: ${message}` });
            });
        }
        const socket = new WebSocket(VOICE_WS_URL);
        socket.binaryType = "arraybuffer";

        socket.onopen = (): void => {
            socket.send(JSON.stringify({ type: "start", sampleRate: context.sampleRate }));
        };
        socket.onmessage = (event: MessageEvent<string>): void => {
            handleVoiceMessage(event.data);
            socket.close();
        };
        socket.onerror = (): void => {
            pushMessage({ role: "system", text: `WebSocket ${VOICE_WS_URL} недоступен — запусти backend` });
            releaseRecordingResources();
            setOrbState("idle");
        };

        const source: MediaStreamAudioSourceNode = context.createMediaStreamSource(stream);
        // ScriptProcessor: deprecated, но достаточен для этапа 3 без аудио-модулей.
        const processor: ScriptProcessorNode = context.createScriptProcessor(4096, 1, 1);
        // Нулевой gain нужен, чтобы processor работал, но микрофон не шёл в колонки.
        const mute: GainNode = context.createGain();
        mute.gain.value = 0;

        processor.onaudioprocess = (event: AudioProcessingEvent): void => {
            if (socket.readyState !== WebSocket.OPEN) {
                return;
            }
            const samples: Float32Array = event.inputBuffer.getChannelData(0);
            // Копия: буфер event переиспользуется браузером.
            socket.send(samples.slice().buffer as ArrayBuffer);
        };

        source.connect(processor);
        processor.connect(mute);
        mute.connect(context.destination);

        audioContextRef.current = context;
        streamRef.current = stream;
        wsRef.current = socket;
        recordingRef.current = true;
        startingRef.current = false;
        setOrbState("listening");

        // Хоткей нажали, пока старт шёл, — останавливаемся сразу.
        if (pendingStopRef.current) {
            pendingStopRef.current = false;
            stopRecording();
        }
    }, [handleVoiceMessage, pushMessage, releaseRecordingResources, stopRecording]);

    /**
     * Переключает запись голоса (вызывается глобальным хоткеем Ctrl+Space).
     *
     * Вход: без параметров.
     * Выход: None.
     */
    const toggleRecording = useCallback((): void => {
        if (startingRef.current) {
            // Старт ещё в полёте — запоминаем остановку вместо второго старта.
            pendingStopRef.current = true;
            return;
        }
        if (recordingRef.current) {
            stopRecording();
            return;
        }
        // Обрываем играющий ответ: иначе микрофон запишет голос ассистента
        // из колонок (она распознавала собственные реплики). Заодно это
        // естественное «перебить» — можно говорить сразу.
        if (ttsAudio !== null) {
            ttsAudio.pause();
            setTtsAudio(null);
            setOrbState("idle");
        }
        void startRecording();
    }, [startRecording, stopRecording, ttsAudio]);

    // Регистрируем глобальный хоткей из Electron preload; cleanup отписывается,
    // а replace-семантика моста гарантирует ровно один активный обработчик.
    useEffect(() => {
        if (!window.julie) {
            // Мост не поднялся — хоткей молча не сработает; пишем в консоль.
            console.warn("Мост preload (window.julie) недоступен — Ctrl+Space не будет работать.");
            return;
        }
        const unsubscribe = window.julie.onVoiceToggle(() => toggleRecording());
        return unsubscribe;
    }, [toggleRecording]);

    // Освобождаем ресурсы при размонтировании.
    useEffect(() => {
        return () => {
            releaseRecordingResources();
        };
    }, [releaseRecordingResources]);

    return (
        <div className="overlay-root">
            <ChatPanel open={chatOpen} messages={messages} pending={pending} onSend={(text: string) => void sendChat(text)} />
            <div className="orb-wrap">
                <AssistantOrb
                    state={orbState}
                    audioElement={ttsAudio}
                    onClick={() => setChatOpen((prev: boolean) => !prev)}
                />
                <span className={`orb-status ${orbState === "idle" ? "orb-status--hidden" : ""}`}>
                    {STATE_LABELS[orbState]}
                </span>
            </div>
        </div>
    );
}

export default App;
