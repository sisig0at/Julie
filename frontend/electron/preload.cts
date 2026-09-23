import { contextBridge, ipcRenderer } from "electron";

/**
 * Единственный активный обработчик хоткея: повторный вызов onVoiceToggle
 * заменяет его, чтобы двойные подписки (StrictMode в dev) не плодили дубли —
 * иначе одно нажатие тоглило бы запись дважды.
 */
let voiceHandler: (() => void) | null = null;

ipcRenderer.on("voice:toggle", () => {
    voiceHandler?.();
});

/**
 * Мост main → renderer для глобальных хоткеев.
 *
 * Вход: колбэк, вызываемый при нажатии Ctrl+Space (глобальный хоткей).
 * Выход: функция отписки; колбэк заменяет предыдущий (подписка ровно одна).
 */
contextBridge.exposeInMainWorld("julie", {
    onVoiceToggle: (callback: () => void): (() => void) => {
        voiceHandler = callback;
        return () => {
            // Снимаем только свой обработчик (не чужой, если уже заменён).
            if (voiceHandler === callback) {
                voiceHandler = null;
            }
        };
    },
});
