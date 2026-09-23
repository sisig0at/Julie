import { useEffect, useRef, useState, type ReactElement } from "react";

/** Одно сообщение чата: пользователь, ассистент или системное уведомление. */
export type ChatMessage = {
    role: "user" | "assistant" | "system";
    text: string;
    /** Какой мозг ответил (только у ассистента). */
    brain?: "fast" | "smart";
    /** Причина отката на fast, если smart не сработал. */
    fallbackReason?: string;
};

/** Пропсы панели чата (состояние чата живёт в App, чтобы голос писал туда же). */
type ChatPanelProps = {
    /** Панель раскрыта (видима рядом с орбом). */
    open: boolean;
    /** Сообщения чата (владелец состояния — App). */
    messages: ChatMessage[];
    /** Идёт текстовый запрос (блокируем ввод). */
    pending: boolean;
    /** Отправить текстовое сообщение в /chat. */
    onSend: (text: string) => void;
};

/**
 * Стеклянная панель чата слева от орба: список сообщений и поле ввода.
 *
 * Вход: пропсы ChatPanelProps.
 * Выход: JSX панели со слайдом+fade при open.
 */
function ChatPanel({ open, messages, pending, onSend }: ChatPanelProps): ReactElement {
    const [input, setInput] = useState<string>("");
    const [status, setStatus] = useState<string>("backend: проверяется...");
    // Автопрокрутка к последнему сообщению.
    const listRef = useRef<HTMLDivElement | null>(null);

    // Проверяем живость backend при монтировании.
    useEffect(() => {
        let cancelled: boolean = false;

        fetch("http://localhost:8000/health")
            .then((response: Response) => {
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                return response.json() as Promise<{ status: string }>;
            })
            .then((data: { status: string }) => {
                if (!cancelled) {
                    setStatus(data.status === "ok" ? "backend: ok" : "backend: недоступен");
                }
            })
            .catch((error: unknown) => {
                if (!cancelled) {
                    console.error("Запрос к backend не удался:", error);
                    setStatus("backend: недоступен");
                }
            });

        return () => {
            cancelled = true;
        };
    }, []);

    // Прокручиваем список вниз при каждом новом сообщении.
    useEffect(() => {
        listRef.current?.scrollTo({ top: listRef.current.scrollHeight });
    }, [messages]);

    return (
        <section
            className={`chat-panel ${open ? "chat-panel--open" : "chat-panel--closed"}`}
            aria-hidden={!open}
        >
            <header className="chat-panel__header">
                <span className="chat-panel__title">Джули</span>
                <span className="chat-panel__status" data-testid="backend-status">
                    {status}
                </span>
            </header>

            <div className="chat-panel__messages" ref={listRef}>
                {messages.map((message: ChatMessage, index: number) => (
                    <div key={index} className={`bubble ${message.role}`}>
                        {message.text}
                        {message.role === "assistant" && (
                            <div className="meta">
                                {message.brain && <span className="brain-tag">{message.brain}</span>}
                                {message.fallbackReason && (
                                    <span className="fallback-reason">{message.fallbackReason}</span>
                                )}
                            </div>
                        )}
                    </div>
                ))}
            </div>

            <form
                className="chat-panel__composer"
                onSubmit={(event: React.FormEvent<HTMLFormElement>) => {
                    event.preventDefault();
                    onSend(input);
                    setInput("");
                }}
            >
                <input
                    type="text"
                    value={input}
                    placeholder="Спроси Джули..."
                    disabled={pending}
                    onChange={(event: React.ChangeEvent<HTMLInputElement>) =>
                        setInput(event.target.value)
                    }
                />
                <button type="submit" disabled={pending || input.trim() === ""}>
                    {pending ? "..." : "➤"}
                </button>
            </form>
        </section>
    );
}

export default ChatPanel;
