# Julie

Десктопный AI-ассистент: FastAPI backend + Electron/React frontend + Ollama, STT/TTS.

## Установка

```bash
# терминал 1 — backend
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt   # Linux/macOS: .venv/bin/pip
.venv\Scripts\uvicorn main:app --reload --port 8000

# терминал 2 — frontend
cd frontend
npm install
npm run electron:dev
```

Backend отвечает на `http://localhost:8000/health`, frontend — на `http://localhost:5173`.
В окне Electron должно появиться «backend: ok». Переменные окружения — в `.env` по образцу `.env.example`.
