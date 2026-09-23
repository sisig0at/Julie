import { useEffect, useRef, type ReactElement } from "react";

/** Состояние ассистента: бездействие, слушание, размышление, речь. */
export type OrbState = "idle" | "listening" | "thinking" | "speaking";

/** Пропсы орба. */
type AssistantOrbProps = {
    state: OrbState;
    /** Аудиоэлемент для реакции эквалайзера в состоянии speaking (если играет). */
    audioElement?: HTMLAudioElement | null;
    /** Клик по орбу (toggle чат-панели). */
    onClick?: () => void;
};

// Геометрия SVG: центр, радиус кольца, размеры бара.
const SIZE: number = 140;
const CENTER: number = SIZE / 2;
const RING_RADIUS: number = 48;
const BAR_WIDTH: number = 3;
const BAR_MAX_HEIGHT: number = 30;
const BAR_COUNT: number = 32;

// Дизайн-токены для градиента speaking (violet → cyan по кольцу).
const VIOLET: string = "#8B7CF6";
const CYAN: string = "#4FD1C5";

/**
 * Линейно смешивает два hex-цвета.
 *
 * Вход: два цвета вида "#RRGGBB" и параметр t от 0 до 1.
 * Выход: строка цвета "#RRGGBB".
 */
function mixHex(from: string, to: string, t: number): string {
    const parse = (hex: string): number[] => [
        parseInt(hex.slice(1, 3), 16),
        parseInt(hex.slice(3, 5), 16),
        parseInt(hex.slice(5, 7), 16),
    ];
    const a: number[] = parse(from);
    const b: number[] = parse(to);
    const clamped: number = Math.min(1, Math.max(0, t));
    const channel = (i: number): string =>
        Math.round(a[i] + (b[i] - a[i]) * clamped)
            .toString(16)
            .padStart(2, "0");
    return `#${channel(0)}${channel(1)}${channel(2)}`;
}

/**
 * Статическая "волна" для listening: плавное распределение высот по кольцу.
 *
 * Вход: индекс бара.
 * Выход: множитель высоты от 0.25 до 0.55.
 */
function listeningScale(index: number): number {
    return 0.4 + 0.15 * Math.sin((index / BAR_COUNT) * Math.PI * 4);
}

/**
 * Круглый орб-ассистент с эквалайзером на 32 бара.
 *
 * Вход: пропсы AssistantOrbProps.
 * Выход: JSX кнопки-орба диаметром 140px.
 */
function AssistantOrb({ state, audioElement, onClick }: AssistantOrbProps): ReactElement {
    // Ссылки на <rect> для прямого управления высотой в speaking (без ререндеров).
    const barRefs = useRef<Array<SVGRectElement | null>>([]);

    // speaking + живое аудио → Web Audio API: AnalyserNode → высоты баров по кадрам.
    useEffect(() => {
        if (state !== "speaking" || !audioElement) {
            return;
        }

        const AudioContextCtor =
            window.AudioContext ??
            (window as unknown as { webkitAudioContext?: typeof AudioContext })
                .webkitAudioContext;
        if (AudioContextCtor === undefined) {
            console.error("Web Audio API не поддерживается в этом окружении.");
            return;
        }

        const context: AudioContext = new AudioContextCtor();
        let source: MediaElementAudioSourceNode;
        try {
            // Один элемент нельзя подключать дважды — исключение ловим явно.
            source = context.createMediaElementSource(audioElement);
        } catch (error) {
            console.error("Не удалось подключить аудиоэлемент к AudioContext:", error);
            void context.close();
            return;
        }

        const analyser: AnalyserNode = context.createAnalyser();
        analyser.fftSize = 64; // frequencyBinCount = 32 → по значению на бар.
        source.connect(analyser);
        analyser.connect(context.destination);

        context.resume().catch((error: unknown) => {
            console.error("AudioContext не возобновился:", error);
        });

        const bins = new Uint8Array(analyser.frequencyBinCount);
        let frameId: number = 0;

        const tick = (): void => {
            analyser.getByteFrequencyData(bins);
            for (let i = 0; i < BAR_COUNT; i += 1) {
                const rect = barRefs.current[i];
                if (rect) {
                    // Без сглаживания — чтобы реакция была заметна.
                    const scale = Math.max(0.05, bins[i] / 255);
                    rect.style.transform = `scaleY(${scale})`;
                }
            }
            frameId = requestAnimationFrame(tick);
        };
        frameId = requestAnimationFrame(tick);

        return () => {
            cancelAnimationFrame(frameId);
            source.disconnect();
            analyser.disconnect();
            context.close().catch((error: unknown) => {
                console.error("Не удалось закрыть AudioContext:", error);
            });
            // Возвращаем бары под управление CSS-анимаций.
            barRefs.current.forEach((rect) => {
                if (rect) {
                    rect.style.transform = "";
                }
            });
        };
    }, [state, audioElement]);

    // Цвет баров по состоянию; в speaking — градиент по кольцу.
    const colorFor = (index: number): string => {
        switch (state) {
            case "listening":
                return "var(--accent-cyan)";
            case "thinking":
                return "var(--accent-violet)";
            case "speaking":
                return mixHex(VIOLET, CYAN, index / (BAR_COUNT - 1));
            case "idle":
            default:
                return "var(--text-muted)";
        }
    };

    // CSS-класс анимации: idle/thinking — пульсация, listening — статика,
    // speaking с аудио управляется rAF (без класса), без аудио — пульсация thinking.
    const barAnimationClass = (): string => {
        if (state === "idle") {
            return "orb-bar--idle";
        }
        if (state === "thinking") {
            return "orb-bar--thinking";
        }
        if (state === "speaking" && !audioElement) {
            return "orb-bar--thinking";
        }
        if (state === "listening") {
            return "";
        }
        return "";
    };

    return (
        <button type="button" className="orb" onClick={onClick} aria-label="Джули">
            <svg
                className="orb-equalizer"
                viewBox={`0 0 ${SIZE} ${SIZE}`}
                width={SIZE}
                height={SIZE}
                aria-hidden="true"
            >
                {Array.from({ length: BAR_COUNT }, (_, index: number) => (
                    <g
                        key={index}
                        transform={`rotate(${(index * 360) / BAR_COUNT} ${CENTER} ${CENTER})`}
                    >
                        <rect
                            ref={(element: SVGRectElement | null) => {
                                barRefs.current[index] = element;
                            }}
                            className={`orb-bar ${barAnimationClass()}`}
                            x={CENTER - BAR_WIDTH / 2}
                            y={CENTER - RING_RADIUS}
                            width={BAR_WIDTH}
                            height={BAR_MAX_HEIGHT}
                            rx={BAR_WIDTH / 2}
                            fill={colorFor(index)}
                            style={
                                state === "listening"
                                    ? { transform: `scaleY(${listeningScale(index)})` }
                                    : undefined
                            }
                        />
                    </g>
                ))}
            </svg>
        </button>
    );
}

export default AssistantOrb;
