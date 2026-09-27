# J.A.R.V.I.S. Assistant

A futuristic HUD voice assistant. Electron UI + Python (FastAPI) backend.

- Voice input (mic, EN/RU recognition language), speech output (edge-tts), text chat
- AI tool use (Groq / OpenRouter / local Ollama, function calling): open/close apps,
  open websites, volume control, lock screen, screenshots, screen vision,
  close windows/tabs, web search, find files, current time
- Cross-platform: Linux + Windows 11
- Works as an app (Electron) or in a browser (UI served by the backend)

## Structure

```
├── src/                  Python backend (FastAPI)
│   ├── server.py         API server (port 8765 / dynamic in packaged mode)
│   ├── jarvis_core.py    AI logic, tools, TTS, status
│   ├── brain_router.py   Dual-brain classifier: local (Ollama) vs cloud prompt
│   ├── platform_ops.py   Cross-platform system operations
│   ├── config.py         User config (API key/provider/model) - NOT bundled
│   └── audio/            Generated TTS files (gitignored)
├── electron/             Electron UI (HUD)
│   ├── main.js           Window, global shortcuts, spawns backend, settings IPC
│   ├── preload.js
│   └── renderer/         HUD UI (index.html / styles.css / renderer.js)
├── scripts/              Build helpers
├── legacy/               Old standalone script (reference only)
├── requirements.txt
└── run.sh                Dev: start backend + Electron in one command
```

## Setup (development)

```bash
python -m venv venv
venv/bin/pip install -r requirements.txt   # Windows: venv\Scripts\pip install ...
cd electron && npm install
cd ..
./run.sh                                   # Windows: run the steps manually
```

On first launch the app shows a configuration dialog (Groq, OpenRouter or Ollama,
API key, model choice). **The key is stored in the user config dir, never in the repo or exe:**

- Linux:   `~/.config/jarvis/config.json`
- Windows: `%APPDATA%\Jarvis\config.json`

Click the gear icon (bottom right) any time to change the provider/key/model.

### Voice input language (EN / RU)

The button left of the mic toggles the recognition language: `EN` (default,
`en-US`) or `RU` (`ru-RU`). The choice is saved to `config.json`
(`stt_language`, so it survives a restart) and passed as `?lang=` to
`POST /api/listen`.

Transcription goes through Google Web Speech (the `SpeechRecognition` library),
which needs **an internet connection** and recognises exactly one locale per
request - there is no automatic RU/EN detection, so flip the button before
speaking. TTS (edge-tts) is untouched.

### Local model (Ollama)

Pick provider **Ollama (local)** in the settings dialog - no API key is needed and the
model list comes from your local `ollama list`. Requirements:

```bash
ollama serve            # service must run on http://localhost:11434
ollama pull llama3.2    # any chat model; use a vision model for screen analysis
```

If the service is not running or the selected model is not pulled, the settings dialog
and the chat show an actionable error instead of failing silently. Tool-calling uses the
same OpenAI-compatible format, so the tools work as with Groq/OpenRouter (pick a model
that supports tools, e.g. `qwen2.5`, `llama3.1`, `llama3.2`, `mistral`).

### Dual-brain routing (BRAIN: Dual)

Next to **PROVIDER** the settings dialog has a **BRAIN** selector:

- `Manual - one provider` (default) - exactly the old behaviour: the chosen provider
  handles every request.
- `Dual - local + cloud routing` - a heuristic router (`src/brain_router.py`) looks at the
  prompt *before* the LLM call: short factual/household commands and built-in tool
  requests (`get_current_time`, `list_apps`, volume, lock screen, ...) go to the local
  Ollama brain, prompts that need reasoning, code, comparison or long context go to the
  cloud brain (Groq/OpenRouter - picked by PROVIDER in this mode).

In dual mode the **Ollama model row and the cloud model row are both active**, the API
key belongs to the cloud brain, and the manual provider in `config.json` is left alone
(`brain_mode: "dual"` + `cloud_provider` are stored alongside it). If the local brain is
down, the request is retried once on the cloud brain instead of failing.

Where to see the route: backend log (`[local] route: ...`, `[local] 🧰 Executing: ...`,
`[local] 🧠 reply: ...`), the status banner (`[local] GET CURRENT TIME`), and the answer
bubbles are prefixed with `[local]` / `[cloud]`.

Tuning the classifier - edit `ROUTING_RULES` / `DEFAULT_BRAIN` in `src/brain_router.py`
(ordered rules, first match wins: length thresholds, keyword stems, regex patterns).
Quick check without starting the app:

```bash
venv/bin/python src/brain_router.py "объясни как работает рекурсия"
```

## Packaging (installer)

The Python backend is compiled to a single binary with PyInstaller, then bundled
with the Electron app via electron-builder into a lightweight NSIS wizard installer.

```bash
# 1. Build the backend binary (Linux or Windows - run on the target OS)
venv/bin/pip install pyinstaller
venv/bin/python scripts/build_backend.py

# 2. Build the installer (Windows: NSIS wizard; Linux: AppImage)
cd electron && npm install && npm run dist
```

Output: `electron/dist/`.

### Release / publish (one command)

```bash
cd electron
npm run publish
```

This compiles the backend, builds the native installer (AppImage on Linux,
NSIS .exe on Windows), tags the release (`v<version>`), pushes the tag, and
drafts a GitHub release with the installer attached.

On Linux, the Windows installer is built automatically by GitHub Actions
(`.github/workflows/build-windows.yml`) on a real Windows runner and attached to
the same draft release. Requires the `gh` CLI to be authenticated.

## API key note

Never commit a real API key. `src/config.py` reads everything from the user config
file; the key never leaves the user's machine and is never part of the installer.
