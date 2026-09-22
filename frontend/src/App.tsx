import { useEffect, useState, type ReactElement } from "react";
import "./App.css";

/**
 * Проверяет связь с backend при монтировании.
 *
 * Вход: без параметров.
 * Выход: JSX со статусом "backend: ok" / "backend: недоступен" / "backend: проверяется...".
 */
function App(): ReactElement {
    const [status, setStatus] = useState<string>("backend: проверяется...");

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

    return (
        <main className="app">
            <h1>Julie</h1>
            <p data-testid="backend-status">{status}</p>
        </main>
    );
}

export default App;
