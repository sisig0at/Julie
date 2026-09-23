// Главный процесс Electron: поднимает Vite dev-сервер и грузит React-приложение.

import { app, BrowserWindow, globalShortcut, screen, session } from "electron";
import { spawn, type ChildProcess } from "node:child_process";
import { fileURLToPath } from "node:url";

// Адрес dev-сервера Vite (порт по умолчанию).
const DEV_SERVER_URL: string = "http://localhost:5173";

// Глобальный хоткей переключения записи голоса (Ctrl+Space).
const VOICE_SHORTCUT: string = "CommandOrControl+Space";

// Сборка preload: .cts компилируется в CommonJS (.cjs) — sandboxed-преload
// Electron не умеет грузить ESM, поэтому расширение именно такое.
const PRELOAD_PATH: string = fileURLToPath(new URL("./preload.cjs", import.meta.url));

// Фиксированный размер окна: чат (360) + орб (140) + отступы (20*2) + зазор (16),
// высота — чат (480) + отступы (20*2). Меняем только через CSS-видимость чата.
const WINDOW_WIDTH: number = 556;
const WINDOW_HEIGHT: number = 520;

// Сколько ждать ответа от dev-сервера, мс.
const STARTUP_TIMEOUT_MS: number = 30_000;

// Интервал опроса dev-сервера, мс.
const POLL_INTERVAL_MS: number = 500;

// Каталог frontend (файл лежит в frontend/dist-electron/).
const FRONTEND_DIR: string = fileURLToPath(new URL("..", import.meta.url));

// Дочерний процесс Vite, чтобы убить его при выходе из приложения.
let viteProcess: ChildProcess | null = null;

/**
 * Запускает `npm run dev` (Vite) в каталоге frontend.
 *
 * Вход: путь к каталогу frontend.
 * Выход: ChildProcess дочернего процесса; при ошибке запуска — исключение.
 */
function startVite(frontendDir: string): ChildProcess {
    const command: string = process.platform === "win32" ? "npm.cmd" : "npm";
    // shell: true обязателен для .cmd на Windows.
    const child: ChildProcess = spawn(command, ["run", "dev"], {
        cwd: frontendDir,
        shell: true,
        stdio: "inherit",
    });
    child.on("error", (error: Error) => {
        console.error("Не удалось запустить Vite:", error.message);
    });
    return child;
}

/**
 * Ждёт, пока dev-сервер начнёт отвечать.
 *
 * Вход: URL dev-сервера, общий таймаут в мс.
 * Выход: true, если сервер ответил; false по истечении таймаута.
 */
async function waitForDevServer(url: string, timeoutMs: number): Promise<boolean> {
    const deadline: number = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        try {
            const response: Response = await fetch(url, { method: "GET" });
            if (response.ok) {
                return true;
            }
        } catch (error) {
            // Сервер ещё не поднялся — это ожидаемо, повторяем опрос.
            const message: string = error instanceof Error ? error.message : String(error);
            console.log(`Dev-сервер ещё не готов (${message}), повтор...`);
        }
        await new Promise<void>((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
    }
    return false;
}

/**
 * Останавливает дочерний процесс Vite вместе с его детьми.
 *
 * Вход: pid процесса Vite или null.
 * Выход: undefined; при ошибке остановки — запись в stderr.
 */
function stopVite(pid: number | undefined): void {
    if (pid === undefined) {
        return;
    }
    try {
        if (process.platform === "win32") {
            // /T — убить дерево процессов, иначе Vite переживёт cmd-обёртку.
            spawn("taskkill", ["/pid", String(pid), "/T", "/F"], { stdio: "ignore" });
        } else {
            process.kill(pid);
        }
    } catch (error) {
        if (error instanceof Error) {
            console.error("Не удалось остановить Vite:", error.message);
        }
    }
}

/**
 * Создаёт главное окно и грузит React-приложение с dev-сервера.
 *
 * Вход: без параметров.
 * Выход: undefined; при недоступности dev-сервера — исключение с инструкцией.
 */
async function createWindow(): Promise<void> {
    viteProcess = startVite(FRONTEND_DIR);

    const ready: boolean = await waitForDevServer(DEV_SERVER_URL, STARTUP_TIMEOUT_MS);
    if (!ready) {
        throw new Error(
            `Dev-сервер Vite не ответил за ${STARTUP_TIMEOUT_MS} мс на ${DEV_SERVER_URL}. ` +
                "Проверь, что 'npm install' выполнен во frontend/ и порт 5173 свободен.",
        );
    }

    const window: BrowserWindow = new BrowserWindow({
        width: WINDOW_WIDTH,
        height: WINDOW_HEIGHT,
        transparent: true,
        frame: false,
        alwaysOnTop: true,
        resizable: false,
        hasShadow: false,
        title: "Julie",
        webPreferences: {
            preload: PRELOAD_PATH,
        },
    });

    // Глобальный хоткей: переключает запись голоса (уведомляет renderer).
    const shortcutRegistered: boolean = globalShortcut.register(VOICE_SHORTCUT, () => {
        if (!window.isDestroyed()) {
            window.webContents.send("voice:toggle");
        }
    });
    if (!shortcutRegistered) {
        console.error(
            `Хоткей ${VOICE_SHORTCUT} занят другим приложением — запись голоса будет недоступна.`,
        );
    }

    // Позиция — верхний правый угол рабочей области основного монитора.
    const workArea = screen.getPrimaryDisplay().workArea;
    window.setBounds({
        x: workArea.x + workArea.width - WINDOW_WIDTH,
        y: workArea.y,
        width: WINDOW_WIDTH,
        height: WINDOW_HEIGHT,
    });

    await window.loadURL(DEV_SERVER_URL);
}

// Приложение готово — создаём окно; при сбое выходим с ошибкой в консоль.
app.whenReady().then(
    () => {
        // Разрешаем renderer доступ к микрофону (запись голоса); прочие
        // разрешения (камера, уведомления...) по умолчанию отклоняем.
        session.defaultSession.setPermissionRequestHandler(
            (_webContents, permission, callback) => {
                callback(permission === "media");
            },
        );

        createWindow().catch((error: unknown) => {
            const message: string = error instanceof Error ? error.message : String(error);
            console.error(message);
            app.quit();
        });
    },
    (error: unknown) => {
        const message: string = error instanceof Error ? error.message : String(error);
        console.error("Не удалось инициализировать Electron:", message);
        app.quit();
    },
);

// Закрыли все окна — выходим из приложения.
app.on("window-all-closed", () => {
    app.quit();
});

// Перед завершением останавливаем дочерний Vite и снимаем глобальные хоткеи.
app.on("before-quit", () => {
    globalShortcut.unregisterAll();
    if (viteProcess !== null) {
        stopVite(viteProcess.pid);
        viteProcess = null;
    }
});
