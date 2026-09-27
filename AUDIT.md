# AUDIT — что реализовано в проекте J.A.R.V.I.S. и как это работает

Аудит проведён чтением кода на текущем коммите `94ab016` (рабочее дерево чистое).
Всё, что написано ниже, подтверждено кодом; где функциональности нет — так и указано
(«не реализовано» / «не нашёл»). Номера строк даны для текущего состояния файлов.

---

## 1. Общая архитектура

### 1.1. Две части: Electron UI + Python-бэкенд

| Компонент | Файл | Роль |
|---|---|---|
| Main-процесс Electron | `electron/main.js` | окно, спавн бэкенда, горячие клавиши, авто-обновление |
| Preload-мост | `electron/preload.js` | `contextBridge` → `window.jarvis` (минимум IPC-методов) |
| UI (renderer) | `electron/renderer/index.html`, `renderer.js`, `styles.css` | HUD, чат, настройки |
| Backend | `src/server.py` (FastAPI + uvicorn) | REST API, STT, раздача аудио и UI |
| Ядро | `src/jarvis_core.py` | LLM-клиенты, роутинг мозгов, tool-loop, TTS, статусы |
| Правила роутинга | `src/brain_router.py` | классификатор dual-режима |
| Системные операции | `src/platform_ops.py` | приложения, громкость, файлы, поиск, время |
| Конфиг | `src/config.py` | `%APPDATA%\Jarvis\config.json` (Windows) |

### 1.2. Как стартует бэкенд и как связаны процессы

- `main.js:38-50` (`resolveBackendCommand`):
  - **packaged**: `resources/backend/jarvis-backend(.exe) --port 0`;
  - **dev**: `venv/Scripts/python.exe -u src/server.py --port 0` (venv в корне репо).
- `main.js:74-109` (`startBackend`): spawn с `PYTHONUTF8=1`, чтение stdout/stderr на регулярку
  `JARVIS_PORT=(\d+)` → формируется `backendUrl = http://127.0.0.1:<порт>` и рассылается в
  renderer через IPC `backend:url`.
- `src/server.py:334-349`: аргументы `--port` (по умолчанию 8765) и `--host` (по умолчанию
  **127.0.0.1**); при `--port 0` порт выбирается через `_pick_free_port()` (`server.py:327-331`)
  и печатается строка `JARVIS_PORT=<n>`; далее `uvicorn.run(app, host, port)`.
- **Dev-режим**: `main.js:52-72` (`checkExistingBackend`) — если на 8765 уже отвечает
  `GET /api/health`, второй бэкенд не спавнится (так работает `run.sh`: поднимает бэкенд на
  8765, лог в `/tmp/jarvis_backend.log`, затем `npx electron . --no-sandbox`).
- **Остановка**: `main.js:165-177` — на Windows `taskkill /pid <pid> /T /F`, иначе SIGKILL;
  вызывается из `will-quit` вместе с `globalShortcut.unregisterAll()`.
- UI **раздаётся самим бэкендом**: `server.py:321-324` монтирует `electron/renderer` на `/`
  (работает и в обычном браузере), аудио — `/audio` (`server.py:45` и ручной роут
  `server.py:313-318`).

### 1.3. Протокол

Только **HTTP + JSON** (REST), WebSocket нет. Renderer ходит через обёртку
`backend._req` (`renderer.js:15-30`): кэширует URL, при сетевой ошибке сбрасывает кэш и
делает **один повтор** с новым URL (страховка от «бегущего» порта).

Безопасность на текущий момент:
- окно: `contextIsolation: true`, `nodeIntegration: false`, есть CSP
  (`index.html:5`, `default-src 'self'`, `connect-src http://127.0.0.1:*`);
- сервер слушает только `127.0.0.1`;
- CORS: `allow_origins=["*"]`, `allow_methods=["*"]` (`server.py:37-42`) — **авторизации/токена
  на API нет** (см. раздел 7).

### 1.4. Поток данных (схема)

**Текстовый ввод:**
```
input → renderer.sendMessage() (renderer.js:225-242)
  → POST /api/chat {text, tts:true}
  → server.chat() (server.py:279-293)
  → core.fetch_ai_response() (jarvis_core.py:529)
       ├─ resolve_brain(prompt) → (тег, клиент, модель, причина маршрута)  (jarvis_core.py:158-174)
       ├─ LLM-вызов с tools=ACTION_TOOLS, до 6 раундов tool-loop            (jarvis_core.py:557-641)
       └─ при ошибке: понятное сообщение об Ollama / "Connection error."    (jarvis_core.py:651-695)
  → core.generate_speech(reply) — edge-tts, mp3 в src/audio/                (jarvis_core.py:698-704)
  → ответ {reply, audio, brain}
  → renderer: сообщение с префиксом "[local]"/"[cloud]", воспроизведение /audio/<file>
```

**Голосовой ввод:**
```
клик 🎤 (renderer.js:283-316)
  → POST /api/listen?lang=en-US|ru-RU
  → server.listen_once() (server.py:85-155)
       ├─ asyncio.to_thread: запись с микрофона (свой цикл чтения чанков 100 мс)
       ├─ стоп: кнопка / тишина 1 с после речи / phrase_limit 10 с / timeout 5 с без речи
       └─ recognize_google(audio, language=stt_lang)   ← (server.py:142)
  → {text} → тот же путь /api/chat (см. выше)
```
Кнопка «стоп» → `POST /api/listen/stop` → `_stop_event.set()` (`server.py:78-82`).

**Статус/HUD:**
```
renderer.pollStatus() каждые 800 мс (renderer.js:205)
  → GET /api/status → core.get_status() (словарь STATUS, jarvis_core.py:110-120)
  → handleStatus() (renderer.js:131-165): баннер EXECUTING/COMPLETE/ERROR,
    статус THINKING/EXECUTING/STANDBY с brain-тегом, орб busy/error
```

---

## 2. LLM-провайдеры и dual-brain

### 2.1. Провайдеры (полный список)

| Провайдер | base_url | API-ключ | Где объявлено |
|---|---|---|---|
| `groq` | `https://api.groq.com/openai/v1` | обязателен | `jarvis_core.py:56-57` |
| `openrouter` | `https://openrouter.ai/api/v1` | обязателен | `jarvis_core.py:54-55` |
| `ollama` | `http://localhost:11434/v1` | фиктивный `"ollama"` | `jarvis_core.py:49-53`, `config.py:55` |

Все три — OpenAI-совместимый `chat.completions`, клиент собирается одним кодом в
`reload_client()` (`jarvis_core.py:40-74`), вызывается при старте и при каждом сохранении
настроек (`server.py:200`).

Клиенты:
- `client_groq` + `ACTIVE_PROVIDER`/`ACTIVE_MODEL`/`ACTIVE_VISION_MODEL` — **ручной режим** (легаси);
- `LOCAL_CLIENT`/`LOCAL_MODEL` и `CLOUD_CLIENT`/`CLOUD_PROVIDER`/`CLOUD_MODEL` — строятся
  всегда, используются только в dual-режиме (`jarvis_core.py:61-69`).

Списки моделей для дропдаунов: `GROQ_MODELS`, `OPENROUTER_FALLBACK_MODELS`,
`OLLAMA_FALLBACK_MODELS` (`config.py:58-91`). Живые списки:
- Ollama: `GET http://localhost:11434/api/tags`, кэш 10 с (`server.py:208-239`), при недоступности
  возвращается `reachable:false` и текст-инструкция «Start it with 'ollama serve'…»;
- OpenRouter: `https://openrouter.ai/api/v1/models`, кэш 300 с (`server.py:246-268`);
- Groq: статический список (`server.py:269`).

Конфиг: `src/config.py` → `DEFAULT_CONFIG` (`config.py:15-31`), файл
`%APPDATA%\Jarvis\config.json` (Windows) / `~/.config/jarvis/config.json` (Linux)
(`config.py:94-99`), запись с `chmod 600` (`config.py:124-127`).

### 2.2. Классификация запроса в `brain_router.py`

Эвристика без ML и сетевых вызовов. `classify(prompt)` (`brain_router.py:130-137`)
перебирает **упорядоченный** список `ROUTING_RULES` (`brain_router.py:42-105`): выигрывает
**первое** правило, у которого выполнились **все** условия (AND, `_rule_matches`,
`brain_router.py:108-127`). Если ни одно не подошло → `DEFAULT_BRAIN = "cloud"`
(`brain_router.py:40`).

Условия правила: `min_chars`/`max_chars` (длина сырой строки), `min_words`/`max_words`
(число слов), `keywords` — подстрока в lowercased-тексте, `patterns` — regex c
`re.IGNORECASE`.

Правила **как они есть в коде сейчас** (пороги и полные списки ключевых слов):

1. **Длинный запрос → cloud**
   - `{"brain":"cloud", "name":"long prompt (>400 chars)", "min_chars": 400}`
   - `{"brain":"cloud", "name":"long prompt (>60 words)", "min_words": 60}`

2. **Рассуждение/код/генерация → cloud** (`brain_router.py:50-64`, keywords)
   - RU-стемы: `объясн, почему, сравн, напиши код, напиши скрипт, напиши программ,
     сгенерируй, допиши, рефактор, оптимизируй, пошаг, план, разбер, проанализ, докаж,
     придумай, посчитай, реши задач, переведи, напиши эссе, напиши текст, расскажи про,
     как работает, в чем разница, посовет, порекоменд, прикинь`
   - EN: `explain, compare, write code, write a script, refactor, optimize,
     step by step, "design ", implement, debug, analyze, prove, summar, translate,
     "draft ", pros and cons, suggest, recommend, give me ideas`
   - `{"brain":"cloud", "name":"reasoning regex patterns"}` (`brain_router.py:65-71`):
     `\bwhy\b`, `\bhow (does|do|to)\b`, `\bwrite\b.*\b(code|script|function|app)\b`,
     `\bgenerat`, `\bregex\b`, `\bsql\b`, `\bapi\b`, `\balgorithm\b`,
     `\bthe difference\b`, `\bdiff between\b`, `\bconvert\b`, `\bconvert the\b`,
     `\bоцен`, `\bсравн`

3. **Явная встроенная команда-тул → local** (`brain_router.py:74-100`)
   - keywords (время): `который час, сколько времени, текущее время, какое сегодня,
     what time, what's the date, what is the date`
   - keywords (окна/программы): `открой, закрой, запусти, выключи программ, список програм,
     какие програм, установленн, закрой вкладк, закрой окн, open app, close app, list apps,
     installed apps, close window, close tab`
   - keywords (сайты): `открой сайт, открой ютуб, зайди на, open site, open youtube`
   - keywords (звук/экран): `громкост, звук, заглуш, без звука, заблокируй, блокировка,
     скриншот, снимок экрана, что на экране, что у меня на экране, volume, mute, unmute,
     lock screen, screenshot, what's on my screen, what is on my screen`
   - keywords (файлы/поиск/память): `найди файл, найди в системе, поиск в интернет,
     найди в интернет, погод, новост, what's the weather, search the web, find file,
     очисти память, забудь, clear memory, forget`
   - patterns: `\bwhat time\b`, `\bopen\b`, `\bclose\b`, `\bvolume\b`, `\block\b`,
     `\bscreenshot\b`, `\blist (my )?apps\b`, `\bset .{0,12}volume\b`

4. **Короткий запрос → local**: `{"max_chars": 100, "max_words": 14}` (`brain_router.py:103-104`).

Проверка правил из консоли: `python src/brain_router.py "текст"` (CLI-самотест,
`brain_router.py:140-155`).

### 2.3. Manual vs Dual

`resolve_brain()` (`jarvis_core.py:158-174`):
- **manual** (по умолчанию): возвращается ровно легаси-клиент/модель (`client_groq`,
  `ACTIVE_MODEL`) с тегом `[_provider_brain_tag]` = `[local]` для ollama, иначе `[cloud]`,
  причина в логе `manual/<провайдер>`. Поведение байт-в-байт как до появления dual.
- **dual**: `brain_router.classify()` → `[local]` (LOCAL_CLIENT/LOCAL_MODEL) либо
  `[cloud]` (CLOUD_CLIENT/CLOUD_MODEL).

Ключ dual-режима: проверка «нужен ли ключ» смещается на облачную часть
(`jarvis_core.py:532-549`): в dual локальный мозг работает без ключа; если классификатор
отправил в cloud, а ключа нет — явное сообщение «The cloud brain has no API key
configured. Add one in settings…».

### 2.4. Fallback-логика

Что происходит при недоступности:

| Ситуация | Поведение | Где |
|---|---|---|
| manual, нет ключа | «No API key configured yet. Open settings…», статус `error/config required` | `jarvis_core.py:534-537` |
| dual → local, Ollama упал/нет модели, **и есть** облачный ключ | один повтор тем же запросом в cloud, лог `unavailable → falling back to [cloud]` | `jarvis_core.py:566-582` |
| dual → local, Ollama упал, облачного ключа нет | запрос падает в обработчик → читаемое сообщение об Ollama (`ollama_error_message`) | `jarvis_core.py:651-660` |
| dual → cloud, ошибка | **автоматического fallback на local НЕТ** → при `tool_use_failed`/`failed_generation` пробует recovery (см. 3.3), иначе `Connection error.` | `jarvis_core.py:661-695` |
| Ollama недоступен (UI, дропдаун моделей) | `reachable:false` + «Ollama is not reachable at http://localhost:11434. Start it with 'ollama serve'…» | `server.py:229-232`, `renderer.js:421-429` |
| Ollama вернул 404 (нет модели) | «Ollama model '<имя>' is not available locally. Pull it with 'ollama pull …'» | `jarvis_core.py:92-96` |
| Ошибки вида connection refused / 11434 / max retries | «Ollama is not reachable at http://localhost:11434. Start it with 'ollama serve'…» | `jarvis_core.py:97-101` |

`ollama_error_message(err)` (`jarvis_core.py:84-101`) возвращает `""` для «не-Ollama»
ошибок, чтобы обычная обработка осталась прежней.

---

## 3. Tool-calling / функции

### 3.1. Полный список зарегистрированных тулов

`ACTION_TOOLS` — `jarvis_core.py:233-377` (14 тулов, стандартный OpenAI-формат
`type:"function"`; описания ниже — это то, что реально пролетает в промпт):

| # | Имя | Описание из кода | Реализация |
|---|---|---|---|
| 1 | `open_application` | Открыть приложение (в описании сказано «on the user's Linux system») | `platform_ops.open_app` (`platform_ops.py:100-123`): Windows — словарь `exe_map` + `start "" <exe>`; Linux — `.desktop` файл → `nohup … &`, иначе `shutil.which` |
| 2 | `close_application` | Закрыть приложение | `platform_ops.close_app` (`:126-146`): Windows `taskkill /f /im <exe>`, Linux `pkill -f <proc>` |
| 3 | `open_website` | Открыть сайт в браузере | `build_website_url` (`jarvis_core.py:214-231`, словарь `KNOWN_DOMAINS` `:196-211`, иначе `<слово>.com`) + `webbrowser.open` (`:445-453`) |
| 4 | `set_volume` | `action` ∈ set/mute/unmute/up/down, `level` 0-100 | `platform_ops.set_volume/mute_volume/volume_step` (`:151-201`): Windows — **pycaw/comtypes**, Linux — `wpctl` |
| 5 | `lock_screen` | Заблокировать ПК | `platform_ops.lock_screen` (`:206-211`): `rundll32.exe user32.dll,LockWorkStation` / `loginctl lock-session` |
| 6 | `take_screenshot` | Скриншот в папку Pictures | `pyautogui.screenshot()` → `~/Pictures/screenshot_jarvis.png` (`jarvis_core.py:480-486`) |
| 7 | `analyze_screen` | Снять и визуально оценить экран (`question`) | `capture_and_analyze_screen` (`jarvis_core.py:380-431`): PNG → base64 → vision-модель, `max_tokens=300` |
| 8 | `get_current_time` | Текущие дата/время системы | `platform_ops.current_time` (`:317-320`, формат 12h на английском) |
| 9 | `list_apps` | Список установленных приложений (для правила «open X = app или сайт?») | `platform_ops.list_apps` (`:35-63`): обход Start Menu за `*.lnk` (Windows) / `*.desktop` (Linux); в ответ отдаётся первые 60 (`jarvis_core.py:503-507`) |
| 10 | `search_web` | Поиск фактов/новостей | `platform_ops.web_search` (`:300-312`) = Wikipedia API + DuckDuckGo Instant Answers, **без ключей** |
| 11 | `find_files` | Найти файл по имени | `platform_ops.find_files` (`:244-258`): рекурсивный обход `~`, пропуск `node_modules/.git/__pycache__/venv/AppData`, максимум 8 совпадений |
| 12 | `close_tab` | Закрыть вкладку браузера | `platform_ops.close_tab` (`:226-239`): `keyboard.send("ctrl+w")` |
| 13 | `close_window` | Закрыть активное окно | `platform_ops.close_window` (`:214-223`): `keyboard.send("alt+f4")` / `wmctrl -c :ACTIVE:` |
| 14 | `clear_memory` | Забыть историю разговора | обнуляет `conversation_history` до system-промпта (`jarvis_core.py:516-520`) |

Дополнительно в системный промпт (`jarvis_core.py:176-194`) зашиты правила: при «открой X»
сначала `list_apps`, `analyze_screen` — только по явной просьбе, время → `get_current_time`,
факты → `search_web`, отвечать кратко.

Диспетчер — `execute_action(tool_name, tool_args)` (`jarvis_core.py:434-526`): ставит
статус `executing`, вызывает нужную функцию, `finish_status(...)`, при исключении логирует
и возвращает строку `Failed to execute <name>.`

### 3.2. Формат тулов для всех провайдеров

Один и тот же `ACTION_TOOLS` передаётся во все вызовы (`jarvis_core.py:559-564`, `605-611`,
`629-635`) — никакой адаптации под провайдера нет: и Ollama, и Groq/OpenRouter получают
одинаковый OpenAI-совместимый набор.

### 3.3. Цикл вызова тула

`fetch_ai_response()` (`jarvis_core.py:529-695`):

1. Проверка ключа → `resolve_brain()` → лог `route: <причина>`.
2. Промпт добавляется в `conversation_history`; **история обрезается**: если сообщений > 9,
   остаются `[system] + последние 8` (`jarvis_core.py:554-555`).
3. Первый вызов LLM: `messages=history, tools=ACTION_TOOLS, temperature=0.6, max_tokens=400`
   (`:559-565`). При исключении в dual-локальном мозге — fallback в cloud (раздел 2.4).
4. **Tool-loop, максимум 6 раундов** (`:587-641`):
   - если `message.tool_calls` — каждое `tool_calls` парсится (`json.loads(arguments)`,
     при `JSONDecodeError` → `{}`), лог `🧰 Executing: <name>(<args>)`, выполняется
     `execute_action`, результат кладётся в историю как `{"role":"tool", "tool_call_id": …}`,
     затем **следующий** вызов LLM с уже накопленными tool-результатами → `continue`;
   - если tool_calls нет — чистится `content` (вырезаются `…` блоки),
     и ответ берётся как есть.
5. **Recovery-механизмы** (модель не всегда отвечает структурно):
   - модель *напечатала* вызов текстом: регексп `<function=имя{args}>` из `content`
     (`:617-636`) — тул исполняется, в историю добавляется `role:"assistant"` (текст) +
     `role:"tool"`, вызов LLM повторяется;
   - провайдер вернул ошибку `tool_use_failed`/`failed_generation`: то же извлечение уже
     из текста исключения + повторный запрос без tools (`:661-692`).
6. Пустой ответ → `"Done."` (`:643-644`); финальная реплика кладётся в историю, статус
   `idle/complete`, лог `🧠 reply: …` (`:646-650`).

**Важно из кода:** все вызовы LLM/тулов — **синхронные** внутри `async def`, поэтому на время
чата event loop uvicorn занят (см. раздел 7).

---

## 4. Голос

### 4.1. STT (речь → текст)

- Библиотека: `speech_recognition` (`SpeechRecognition==3.17.0`), микрофон — `PyAudio`.
  Импорт на вершине `server.py:20`.
- Запись: свой цикл чтения (`server.py:97-148`), чанк 100 мс, порог энергии после
  `adjust_for_ambient_noise(duration=0.4)`; остановка по: кнопке стоп, тишине > 1 с после
  речи, `phrase_limit` (10 с), отсутствию речи дольше `timeout` (5 с).
- Распознавание: **`_recognizer.recognize_google(audio, language=stt_lang)`**
  (`server.py:142`) — это Google Web Speech, т.е. **онлайн**; комментарий в коде прямо
  фиксирует: без `language=` распознавалось всегда как en-US.
- **Язык задаётся** трёхступенчато — `_resolve_stt_lang(lang)` (`server.py:62-75`):
  1. `?lang=` из UI; короткие коды мапятся через `config.STT_LANGS`
     (`config.py:38-44`): `en→en-US`, `en-us→en-US`, `en-gb→en-GB`, `ru→ru-RU`, `ru-ru→ru-RU`;
  2. произвольная локаль из двух альфа-частей проходит насквозь (напр. `de-DE`) — т.е.
     API умеет больше, чем предлагает UI;
  3. иначе — сохранённая в конфиге `stt_language` (по умолчанию `en-US`;
     мусор в конфиге нормализуется в `en-US`, `config.py:47-51`).
- Сохранение выбора: кнопка `#btn-lang` (`index.html:54`) → `renderer.js:255-281`
  (тумблер EN↔RU, `saveConfig({stt_language})`), чтение при первом успешном опросе
  (`loadSttLang()`, `renderer.js:189,262-268`).
- Результат: `{"text": …}`; «речи нет» → `{"text": ""}` → в UI «NO SPEECH DETECTED»;
  `sr.RequestError` → `{"error": …}` → «MIC ERROR» (`server.py:143-152`).
- Параллелизм: флаг `_listening` → повторный вызов вернёт `error:"already_listening"`
  (`server.py:90-91`); `_listen_lock` (`server.py:49`) объявлен, но **нигде не используется**.
- Работа в отдельном потоке: `asyncio.to_thread(record_and_recognize)` (`server.py:150`) —
  запись не блокирует event loop.

### 4.2. TTS (текст → речь)

- Движок: **edge-tts** (облачный Microsoft), `generate_speech()` (`jarvis_core.py:698-704`).
- Голос: жёстко зашит `TTS_VOICE = "en-US-ChristopherNeural"` (`jarvis_core.py:107`).
  Выбора голоса нет; **русского голоса нет** — ответ всегда озвучивается английским голосом.
- Каждый ответ чата автоматически озвучивается: renderer шлёт `tts: true`
  (`renderer.js:35`), сервер генерирует mp3 и возвращает имя файла
  (`server.py:286-293`), плеер проигрывает `/audio/<file>` (`renderer.js:218-223`).
  Отдельного endpoint `POST /api/tts` существует (`server.py:296-299`), но **UI его не вызывает**.
- Ошибка TTS не валит ответ: лог `⚠️ TTS Error`, `audio: None` (`server.py:287-291`).
- Файлы складываются в `src/audio/` (`.gitignore:10-11`), **не удаляются никогда** (чистки
  нет — проверено: `os.remove` встречается только для временного PNG в vision-туле).

### 4.3. Чего не хватает (голос)

- **faster-whisper не внудрён**: его нет в `requirements.txt` (файл целиком — 8 зависимостей,
  раздел 6), нет флага `stt_engine`, нет модуля `src/stt_whisper.py`. Вся текущая STT-логика —
  только `recognize_google`. План миграции (оценка ~2–3 ч, модель base/small, скачивание
  ~140/460 МБ при первом запуске) обсуждался, но **в коде не реализован**.
- **Офлайн-режим голоса недоступен**: и STT (Google), и TTS (edge-tts) требуют интернет.
- Авто-определение языка не реализовано (эндпоинт Google принимает одну локаль на запрос) —
  язык только переключателем EN/RU.
- Wake word / «Джарвис» на слух — не реализован; наоборот, `openwakeword` явно исключён из
  сборки (`scripts/build_backend.py:95`), как и `pystray`, `pygame` и др.
- Ошибки микрофона, не попавшие в три перехваченных исключения (например `OSError` при
  отсутствии устройства), **не обрабатываются** → FastAPI вернёт 500 → в UI «MIC ERROR:
  Backend error 500» вместо человеческого сообщения (`server.py:96-155`).
- Нет настройки TTS (голос/выкл.) и нет настройки STT-движка в UI.

---

## 5. UI (HUD)

### 5.1. Из чего состоит (`electron/renderer/index.html`)

- **Titlebar** (`:15-27`): пульсирующая точка `#core-dot`, заголовок, подзаголовок
  «Mark VII · Uplink Secure», часы `#clock`, кнопка `CLEAR` (`#btn-clear`), «−» и «✕»
  (через IPC `window:minimize/close`).
- **HUD-ядро** (`:29-40`): три кольца, glow, `#core-orb` (индикатор busy/error),
  `#status-label` (STANDBY / THINKING / EXECUTING / LISTENING / OFFLINE / ERROR).
- **Правая панель** (`:42-59`): баннер задачи `#task-banner` (иконка, заголовок, текст),
  лента чата `#chat-log`, строка ввода: **`#btn-lang` (EN/RU)**, `#btn-mic` (🎤),
  `#text-input`, `#btn-send`.
- **Футер** (`:62-69`): `#sysinfo`, `BACKEND: OFFLINE/ONLINE`, блок `UPDATE: …`
  (появляется от авто-обновления), шестерёнка `#btn-settings`.
- **Модалка настроек** (`:73-125`):
  - `PROVIDER` — `groq | openrouter | ollama (local)` (`:85-89`);
  - `BRAIN` — `manual - one provider | dual - local + cloud routing` (`:93-96`);
  - `API KEY` + кнопка показать/скрыть (`:98-104`); строка скрывается для Ollama;
  - три строки модели с `<datalist>`: Groq / OpenRouter / Ollama (`:105-119`);
  - статус `#cfg-status` и `SAVE`.

### 5.2. Что реально настраивается через UI

- провайдер (только в manual), режим BRAIN (manual/dual), API-ключ, модель Groq,
  модель OpenRouter, модель Ollama, **язык распознавания речи EN/RU** (кнопкой у микрофона).
- Логика показа строк — `refreshForm()` (`renderer.js:434-477`): в dual провайдер
  «Ollama» отключён (локальный мозг всегда Ollama, у него своя строка модели), ключ скрыт
  для чистого Ollama, в dual активны **обе** строки моделей (local + cloud), подсказка
  переписывается. Сохранение — `renderer.js:501-547` с валидацией (нет ключа → «ENTER AN
  API KEY»; нет Ollama-модели → «ENTER AN OLLAMA MODEL»); в dual сохраняется
  `cloud_provider`, а `provider` не трогается, чтобы ручной выбор остался прежним.
- Первый запуск без ключа → модалка открывается сама (`checkFirstRun`, `renderer.js:549-561`).
- Логика чата: `sendMessage` (`:225-242`) — ответ показывается с префиксом мозга
  (`${brain} ${reply}`); голосовой путь идентичен (`:283-316`).
- Баннеры: `showBanner` (`:103-110`, живут 5 с), `handleStatus` (`:131-165`):
  `EXECUTING <таск> · <детали>`, `THINKING [brain]`, `ERROR`, по завершении — зелёный
  `COMPLETE` на 5 с; backtick/Esc сбрасывают баннер (`:338-352`; глобальные шорткаты
  регистрирует `main.js:204-218`).
- `CLEAR` → `POST /api/clear` + «MEMORY PURGED» (`:323-331`).
- Статус бэкенда и первый «UPLINK ESTABLISHED» — из `pollStatus` (`:181-203`).
- Футер авто-обновления — `renderer.js:563-586`.

### 5.3. Чего в UI нет

- Настроек vision-моделей: в конфиге есть `vision_model`, `openrouter_vision`,
  `ollama_vision`, но строк в модалке для них **нет** (правятся только руками в config.json).
- Настройки TTS (голос, вкл/выкл), STT-движка, температуры/длины ответа, порогов роутинга.
- Просмотра истории (`GET /api/history` есть, но renderer его не вызывает).
- Индикации «говорит/ожидает бэкенд» в реальном времени помимо статусной строки и баннера.

---

## 6. Конфигурация

### 6.1. `%APPDATA%\Jarvis\config.json` — все поля (полный список)

Источник истины — `DEFAULT_CONFIG` (`src/config.py:15-31`):

| Поле | По умолчанию | Что делает | Где читается |
|---|---|---|---|
| `provider` | `"groq"` | ручной провайдер: `groq \| openrouter \| ollama` | `config.provider()`, `reload_client`, валидация `config.py:184` |
| `api_key` | `""` | ключ Groq/OpenRouter (для Ollama не нужен) | `jarvis_core.py:48,62`, `cloud_key_present()` |
| `model` | `"llama-3.3-70b-versatile"` | модель Groq | `active_model()`, dual `cloud_model()` |
| `openrouter_model` | `"openai/gpt-4o-mini"` | модель OpenRouter | `active_model()` |
| `ollama_model` | `"llama3.2"` | модель Ollama (ручной режим и local-мозг dual) | `local_model()`, `active_model()` |
| `vision_model` | `"qwen/qwen3.6-27b"` | vision-модель Groq | `active_vision_model()` |
| `openrouter_vision` | `"qwen/qwen2.5-vl-72b-instruct"` | vision OpenRouter | там же |
| `ollama_vision` | `"llava"` | vision Ollama | там же |
| `brain_mode` | `"manual"` | `manual` \| `dual` | `config.brain_mode()` (`config.py:139-142`) |
| `cloud_provider` | `""` | облачный мозг dual: `groq \| openrouter`; `""` = следовать `provider`, если он облачный, иначе groq | `config.cloud_provider()` (`:144-152`) |
| `stt_language` | `"en-US"` | язык STT: `en-US \| ru-RU` | `config.stt_language()` (`:163-165`), нормализация `normalize_stt_lang()` |

Поведение хранилища: чтение с мержем поверх дефолтов (`:108-117`), запись c `chmod 600`
(`:119-129`), файл лежит **вне репозитория** (`.gitignore:27` комментирует это явно).

**Отдаётся в UI** через `public()` (`config.py:204-215`): `configured`, `provider`, `model`,
`openrouter_model`, `ollama_model`, `brain_mode`, `cloud_provider`, `stt_language`.
**Не отдаётся**: `api_key`, все vision-поля — это «No secrets here».

**Записывается из UI** через `ConfigRequest` (`server.py:167-175`) → `AppConfig.update()`
(`config.py:181-202`) → `core.reload_client()` (`server.py:200`). Пустые значения частично
игнорируются («empty string = field was hidden»).

### 6.2. Константы, которые выглядят как конфиг, но в файле не лежат

- `TTS_VOICE` — `jarvis_core.py:107`;
- `OLLAMA_BASE_URL` / `OLLAMA_API_TAGS` — `config.py:55-56` (хардкод `localhost:11434`,
  в UI не меняется);
- пороги/списки роутинга — `brain_router.py:40-105` (правятся только в этом файле);
- `GROQ_MODELS` / `OPENROUTER_FALLBACK_MODELS` / `OLLAMA_FALLBACK_MODELS` — `config.py:58-91`;
- параметры LLM-вызова `temperature=0.6`, `max_tokens=400` — `jarvis_core.py:563-564` (и ещё 4 вызова);
- `DEV_BACKEND_PORT = 8765` — `main.js:20` (и дефолт `--port` в `server.py:338`);
- `ACTIVE_VISION_MODEL` дефолт `"qwen/qwen3.6-27b"` дублирует `vision_model` (`jarvis_core.py:26`).

### 6.3. Зависимости (`requirements.txt`, 8 строк)

```
fastapi>=0.141.0  uvicorn>=0.52.0  openai>=2.53.0  edge-tts>=7.2.0
SpeechRecognition>=3.17.0  PyAudio>=0.2.14  PyAutoGUI>=0.9.54
```
**Нет**: `pycaw`, `comtypes`, `keyboard`, `faster-whisper`, `requests` (сеть — на `urllib`).

---

## 7. Известные ограничения / TODO

Явных маркеров `TODO`/`FIXME`/`HACK` в исходниках **нет** (grep: только два
`NOTE:`-комментария — `jarvis_core.py:17`, `legacy/test_jarvis.py:126`). Ниже — то, что
видно при чтении кода и проверено в рантайме.

### 7.1. Сломанные или вводящие в заблуждение функции

1. **Громкость на Windows нерабочая**: `set_volume/mute_volume/volume_step` требуют
   `pycaw`+`comtypes` (`platform_ops.py:155-156`), их **нет в requirements.txt и не
   установлено** (проверено: `find_spec('pycaw') == False`) → всегда
   «Volume control failed.»
2. **`close_window` / `close_tab` на Windows — тихий no-op с «успехом»**: используют
   `import keyboard` (`platform_ops.py:217,229,235`), пакета нет, исключение глушится
   `except Exception: pass`, а функция всё равно возвращает «Closed the window/tab.».
   В релизной сборке это гарантированно: `build_backend.py:97` содержит
   `--exclude-module keyboard`.
3. **`close_app` всегда отвечает «Closed …»**: `os.system("taskkill … >nul 2>&1")`
   — результат не проверяется (`platform_ops.py:137-138`).
4. **Дублирование в описании тулов**: «on the user's Linux system» в `open_application`/
   `close_application` (`jarvis_core.py:238,250`), хотя код исполняется на Windows.

### 7.2. Архитектурные

5. **Блокировка event loop во время чата**: `fetch_ai_response` — `async`, но все
   `client.chat.completions.create(...)` и тулы — синхронные (`jarvis_core.py:559,605,629`;
   `execute_action` → `os.system`, сетевые вызовы). Пока идёт ответ LLM, uvicorn не
   обслуживает другие запросы → опрос `/api/status` каждые 800 мс и STT-запросы
   подвисают (сама запись микрона в `asyncio.to_thread` — не блокирует).
6. **История разговора**: общий глобальный `conversation_history` без блокировки — два
   параллельных `/api/chat` перемешают сообщения; объём жёстко ограничен 9 сообщениями
   (system + 8), между рестартами не сохраняется.
7. **Нет авторизации на API + CORS `*`** (`server.py:37-42`): любая вкладка браузера может
   достучаться до `http://127.0.0.1:<port>/api/chat` и **исполнить тулы** (закрыть окно,
   заблокировать экран, открыть сайт…). Смягчающие факторы: слушает только loopback,
   CSP у самого приложения.
8. **Командная инъекция через аргументы модели**: `os.system(f'start "" {exe}')`
   (`platform_ops.py:111`), `os.system(f"taskkill /f /im {exe}")` (`:137`),
   `pkill -f '{proc}'` (`:145`) подставляют строку из ответа LLM без экранирования.
9. **Fallback только в одну сторону**: cloud → local не предусмотрен (раздел 2.4).
10. **Vision-тул вне dual-роутинга**: `capture_and_analyze_screen` использует
    `client_groq` + `ACTIVE_VISION_MODEL` (`jarvis_core.py:392-393`), т.е. **ручной**
    провайдер/модель, а не тот мозг, куда ушёл запрос.
11. В сообщении 404 от Ollama подставляется `ACTIVE_MODEL` (`jarvis_core.py:94-95`), а не
    `LOCAL_MODEL` — в dual-режиме подсказка может назвать чужую модель.

### 7.3. Прочее, замеченное в коде

12. `_listen_lock` (`server.py:49`) не используется; состояние `speaking` из докстринга
    `STATUS` (`jarvis_core.py:111`) никогда не выставляется.
13. `POST /api/tts` и `GET /api/history` реализованы (`server.py:296-304`), UI их не вызывает.
14. mp3 в `src/audio/` копятся без очистки; `/audio/{filename}` отдаёт любой существующий
    файл по имени (`server.py:313-318`).
15. `find_files` синхронно обходит весь домашний каталог (`platform_ops.py:251`),
    `list_apps` каждый раз заново гонит `os.walk` по Start Menu (`:42`) — без кэша.
16. `web_search` ограничен Wikipedia + DuckDuckGo Instant Answers, параметр `max_results`
    не используется (`platform_ops.py:300-312`) — «новостей» по факту нет.
17. `current_time` всегда 12-часовой английский формат (`:320`).
18. Глобальная клавиша `Escape` перехватывается **системно** (`main.js:212`) — Esc в любой
    программе пошлёт `task:clear` в приложение.
19. Микрофон: `OSError` не перехватывается → HTTP 500 вместо внятной ошибки (раздел 4.3).
20. Тестов в репозитории нет — только легаси-скрипт `legacy/test_jarvis.py` (сам помечен
    «use src/jarvis_core.py instead»).
21. Пороги роутинга чувствительны: любое правило с `keywords` матчится подстрокой, поэтому
    «открой» уводит в local даже длинные запросы с этим словом (правило 3 идёт раньше
    правила 4, но после правил cloud 1-2 — длинный запрос >400 символов всё равно уйдёт в cloud).
22. Уровень репозитория GitHub: ветка `main` содержит этот форк, но дефолтной веткой
    на GitHub остаётся `master` со старым скелетоном — правится в веб-интерфейсе
    (Settings → Default branch).

---

## 8. Точки расширения

| Что добавить | Где именно | Почему именно там |
|---|---|---|
| **Новый тул** | ① схема в `ACTION_TOOLS` (`jarvis_core.py:233-377`); ② ветка в `execute_action` (`:434-526`); ③ реализация в `platform_ops.py`; ④ (желательно) ключевые слова в `ROUTING_RULES` (`brain_router.py:74-100`), чтобы dual отправлял такие команды в local; ⑤ упоминание в системном промпте (`jarvis_core.py:176-194`) | единый диспетчер, формат OpenAI tools один для всех провайдеров — больше нигде править не надо |
| **Новый LLM-провайдер** | ① base_url-ветка в `reload_client` (`jarvis_core.py:40-58`); ② валидация в `AppConfig.update` (`config.py:184`) + `active_model`/`active_vision_model` (`:167-179`); ③ `/api/models` в `server.py:242-269`; ④ `<option>` в `index.html:85-89` + `refreshForm`/`save` в `renderer.js:434-547` | клиент один общий (OpenAI-совместимый), правки — только в точках выбора провайдера |
| **Новое правило роутинга** | `ROUTING_RULES` в `brain_router.py:42-105` (+ `DEFAULT_BRAIN:40`) | файл создан специально: формат правил документирован в докстринге (`:1-34`), есть CLI-самотест (`:140-155`), в `jarvis_core.py` ничего не трогаем |
| **Новый элемент/панель UI** | `index.html` (разметка) + `styles.css` + `renderer.js` (`backend.*` для новых API `:1-76`, обработчики). Для индикации процессов — новое поле в `STATUS` (`jarvis_core.py:110-120`) + ветка в `handleStatus` (`renderer.js:131-165`) | HUD питается только от `/api/status` и REST-вызовов, новых каналов не требуется |
| **Новый REST-endpoint** | `@app.<verb>("…")` в `server.py` (+ при необходимости поле в `ConfigRequest`/`ChatRequest`) + метод в объекте `backend` (`renderer.js:1-76`) | FastAPI-роутер плоский, CORS уже открыт |
| **Новое поле конфигурации** | ① `DEFAULT_CONFIG` (`config.py:15-31`); ② геттер в `AppConfig`; ③ `update()` (`:181-202`); ④ `ConfigRequest` (`server.py:167-175`); ⑤ `public()` (`config.py:204-215`); ⑥ строка в модалке (`index.html:73-125`) + payload в `renderer.js:516-531` | иначе поле не переживёт рестарт или не доедет до UI |
| **Другой STT-движок** | тело `record_and_recognize` в `server.listen_once` (`server.py:97-148`) + флаг в конфиге + `requirements.txt` | запись уже изолирована в `asyncio.to_thread`, `language=` подставляется туда же |
| **Другой голос/язык TTS** | `TTS_VOICE` (`jarvis_core.py:107`) → вынести в конфиг; в паре с `STT_LANGS` (`config.py:38-44`) и тумблером (`renderer.js:255-281`) | точки ввода языка уже есть (EN/RU), осталась только озвучка |
| **Вторая модель vision / маршрутизация vision** | `capture_and_analyze_screen` (`jarvis_core.py:380-431`) — сейчас жёстко берёт `client_groq`/`ACTIVE_VISION_MODEL` | единственное место, где vision отделён от основного tool-loop |

---

## TL;DR

1. **Архитектура**: Electron (frameless HUD) спавнит FastAPI-бэкенд на `127.0.0.1` со
   свободного порта, договариваясь через строку `JARVIS_PORT=`; связь — только REST/JSON,
   UI раздаётся самим бэкендом; опрос `/api/status` каждые 800 мс рисует HUD.
2. **Провайдеры**: groq / openrouter / ollama — все через один OpenAI-совместимый клиент
   (`reload_client`), конфиг в `%APPDATA%\Jarvis\config.json`; Ollama работает вообще без
   ключа, ошибки «не отвечает localhost:11434 / нет модели» отдаются человекочитаемо.
3. **Dual-brain**: чистая эвристика в отдельном файле `brain_router.py` (8 правил:
   >400 символов/>60 слов и «объясн/напиши код/…» → cloud; системные команды и короткие
   фразы ≤100 символов/14 слов → local; дефолт cloud), поверх легаси-ручного режима, без
   его замены; fallback только local→cloud (одна повторная попытка).
4. **Инструменты**: 14 тулов (окна/приложения, громкость, экран, время, поиск, файлы,
   вкладки, память) с единым OpenAI-форматом, цикл до 6 раундов + два recovery-механизма
   для моделей, которые печатают вызов текстом.
5. **Голос**: STT — `recognize_google` с явным `language=` (en-US/RU-RU, тумблер EN/RU,
   требует интернет); TTS — edge-tts, единственный голос `en-US-ChristopherNeural`;
   **faster-whisper и офлайн-режим не внедрены**, авто-определение языка не реализовано.
6. **Главные баги из аудита**: на Windows не работают громкость (нет `pycaw`) и
   `close_window`/`close_tab` (нет `keyboard`, в сборке он исключён) — и это маскируется
   сообщениями об успехе; чат синхронно блокирует event loop; у API нет авторизации при
   CORS `*`; история — всего 8 сообщений, mp3 копятся без очистки.
7. **Расширять легко**: новый тул — `ACTION_TOOLS` + `execute_action` + `platform_ops`;
   новое правило маршрута — только `brain_router.py`; новое поле конфига — 6 точек
   (DEFAULT_CONFIG → геттер → update → ConfigRequest → public → модалка UI). Тестов нет.
