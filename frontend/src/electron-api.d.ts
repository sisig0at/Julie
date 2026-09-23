/** Мост preload: глобальные хоткеи Electron, доступен как window.julie. */
type JulieBridge = {
    /**
     * Регистрирует обработчик глобального хоткея переключения записи.
     *
     * Вход: колбэк на нажатие Ctrl+Space (заменяет предыдущий).
     * Выход: функция отписки для cleanup в useEffect.
     */
    onVoiceToggle: (callback: () => void) => () => void;
};

interface Window {
    julie?: JulieBridge;
}
