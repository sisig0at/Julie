import argparse
import asyncio
import hmac
import os
import sys
import time
import threading
import urllib.error
import urllib.request
from pathlib import Path

# Windows consoles default to a legacy codepage (e.g. cp1251) which crashes
# every emoji/status print; force UTF-8 before importing the logging-heavy
# modules below. Electron already sets PYTHONUTF8 for its own spawn, this
# covers running the backend directly (run.sh / manual start).
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import speech_recognition as sr

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import jarvis_core as core
import config as app_config

# Single source of truth: core owns the config; server must share that same
# instance or saves from the UI would never reach the chat/tools layer.
CONFIG = core.CONFIG

app = FastAPI(title="JARVIS Backend")

# --- API auth: bearer token on every /api/* route except the liveness probe ---
# electron/main.js creates the token once (config.json), passes it to this
# process via JARVIS_API_TOKEN and to the renderer over IPC - it never travels
# over HTTP. A manually started backend generates its own via config.ensure_api_token().
_env_token = os.environ.get("JARVIS_API_TOKEN", "").strip()
API_TOKEN = CONFIG.set_api_token(_env_token) if _env_token else CONFIG.ensure_api_token()
print("🔑 API auth: /api/* requires 'Authorization: Bearer <token>' "
      "(except /api/health)", flush=True)

# /api/health only answers {"ok", "configured"} and is used as a plain liveness
# probe by electron/main.js (checkExistingBackend) before the token exists.
_AUTH_EXEMPT = {"/api/health"}


# Registered BEFORE the CORS middleware on purpose: Starlette inserts each new
# middleware at the front of the stack, so CORS ends up outermost and even 401
# answers carry Access-Control-Allow-Origin for allowed origins.
@app.middleware("http")
async def require_api_token(request, call_next):
    path = request.url.path
    if (request.method != "OPTIONS"
            and path.startswith("/api/")
            and path not in _AUTH_EXEMPT):
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied, f"Bearer {API_TOKEN}"):
            return JSONResponse({"detail": "unauthorized: missing or invalid bearer token"},
                                status_code=401)
    return await call_next(request)


# The UI reaches this backend from exactly two places:
#   1. Electron's renderer loaded from file://  -> Origin: "null"
#   2. this same backend serving electron/renderer on http://127.0.0.1:<port>
# Anything else (other websites, other hosts) is refused - on top of the token.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["null"],
    allow_origin_regex=r"^http://(127\.0\.0\.1|localhost)(:\d+)?$",
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["authorization", "content-type"],
)

AUDIO_DIR = Path(core.AUDIO_DIR)
app.mount("/audio", StaticFiles(directory=core.AUDIO_DIR), name="audio")

# --- Speech-to-text ---
_recognizer = sr.Recognizer()
_listen_lock = threading.Lock()
_listening = {"active": False}
_stop_event = threading.Event()


def _chunk_energy(data: bytes) -> float:
    import array
    samples = array.array("h", data)
    if not samples:
        return 0.0
    return sum(s * s for s in samples) / len(samples)


def _resolve_stt_lang(lang: str) -> str:
    """Language for this recognition request.

    ?lang= (from the UI toggle) wins over the saved preference; the shortcuts
    "en"/"ru" are expanded via config.STT_LANGS, a full locale ("de-DE") passes
    through, anything unrecognised falls back to the saved config value."""
    raw = str(lang or "").strip().lower().replace("_", "-")
    if raw in app_config.STT_LANGS:
        return app_config.STT_LANGS[raw]
    if raw:
        parts = raw.split("-")
        if len(parts) <= 2 and all(p.isalpha() for p in parts):
            return "-".join([parts[0], parts[1].upper()] if len(parts) == 2 else parts)
    return CONFIG.stt_language()


@app.post("/api/listen/stop")
async def stop_listen():
    """Interrupt an active recording early."""
    _stop_event.set()
    return {"ok": True}


@app.post("/api/listen")
async def listen_once(timeout: float = 5.0, phrase_limit: float = 10.0, lang: str = ""):
    """Record from the mic and return transcribed text.
    Stops early when: the user clicks stop, or silence follows speech (1s).
    lang: recognition language ("en" / "ru" / "ru-RU"); empty = saved pref."""
    if _listening["active"]:
        return {"text": "", "error": "already_listening"}

    stt_lang = _resolve_stt_lang(lang)
    _listening["active"] = True
    _stop_event.clear()
    try:
        def record_and_recognize():
            try:
                with sr.Microphone() as source:
                    _recognizer.adjust_for_ambient_noise(source, duration=0.4)
                    threshold = _recognizer.energy_threshold
                    sample_rate = source.SAMPLE_RATE
                    sample_width = source.SAMPLE_WIDTH
                    chunk_frames = sample_rate // 10  # 100ms
                    frames = []
                    has_speech = False
                    silent_since = None
                    start = time.time()

                    while True:
                        if _stop_event.is_set():
                            break
                        if time.time() - start > phrase_limit:
                            break
                        try:
                            data = source.stream.read(chunk_frames)
                        except OSError:
                            break
                        frames.append(data)

                        energy = _chunk_energy(data)
                        if energy > threshold:
                            if not has_speech:
                                has_speech = True
                            silent_since = None
                        else:
                            if has_speech:
                                if silent_since is None:
                                    silent_since = time.time()
                                elif time.time() - silent_since > 1.0:
                                    break

                        if not has_speech and time.time() - start > timeout:
                            break

                    if not frames or not has_speech:
                        return ""

                    audio = sr.AudioData(b"".join(frames), sample_rate, sample_width)
                    # language= is what makes RU recognition work at all:
                    # without it recognize_google always transcribes as en-US.
                    return _recognizer.recognize_google(audio, language=stt_lang)
            except sr.WaitTimeoutError:
                return ""
            except sr.UnknownValueError:
                return ""
            except sr.RequestError as e:
                return f"__ERROR__ {e}"

        result = await asyncio.to_thread(record_and_recognize)
        if result.startswith("__ERROR__"):
            return {"text": "", "error": result.split(" ", 1)[1]}
        return {"text": result}
    finally:
        _listening["active"] = False


class ChatRequest(BaseModel):
    text: str
    tts: bool = True


class TTSRequest(BaseModel):
    text: str


class ConfigRequest(BaseModel):
    provider: str | None = None
    api_key: str | None = None
    model: str | None = None
    openrouter_model: str | None = None
    ollama_model: str | None = None
    brain_mode: str | None = None       # "manual" | "dual"
    cloud_provider: str | None = None   # dual mode: "groq" | "openrouter"
    stt_language: str | None = None     # voice input: "en" | "ru"


class TestCloudRequest(BaseModel):
    provider: str = ""                  # "openrouter" | "groq" | "" = from config
    api_key: str | None = None          # key typed in the field (may be unsaved)


@app.get("/api/health")
async def health():
    return {"ok": True, "configured": CONFIG.is_configured()}


@app.get("/api/config")
async def get_config():
    return CONFIG.public()


@app.post("/api/config")
async def save_config(req: ConfigRequest):
    cfg = CONFIG.update(
        provider=req.provider,
        api_key=req.api_key,
        model=req.model,
        openrouter_model=req.openrouter_model,
        ollama_model=req.ollama_model,
        brain_mode=req.brain_mode,
        cloud_provider=req.cloud_provider,
        stt_language=req.stt_language,
    )
    core.reload_client()
    return cfg


def _test_cloud_key(provider: str, key: str) -> dict:
    """Light auth probe against the selected cloud provider.

    OpenRouter: GET /api/v1/auth/key (cheap, returns key metadata).
    Groq:       GET /openai/v1/models   (only HTTP status is consulted).
    The key travels only in the Authorization header over TLS: it is never
    printed to the log and never included in the returned JSON.
    """
    if provider == "ollama":
        return {"ok": True, "message": "Ollama is local - no API key needed."}
    if provider == "openrouter":
        url, host = "https://openrouter.ai/api/v1/auth/key", "openrouter.ai"
    else:
        url, host = "https://api.groq.com/openai/v1/models", "api.groq.com"

    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read(8192)
    except urllib.error.HTTPError as e:
        # Only the status code is used - response bodies are not logged.
        if e.code == 401:
            return {"ok": False, "message": "Invalid API key."}
        if e.code == 402:
            return {"ok": False, "message": "Insufficient OpenRouter credits."}
        return {"ok": False, "message": f"{host} answered HTTP {e.code}."}
    except Exception as e:
        reason = str(getattr(e, "reason", "") or e).strip().splitlines()
        return {"ok": False,
                "message": f"Could not reach {host}: {reason[0][:120] if reason else 'network error'}"}
    return {"ok": True, "message": f"Key is valid - {host} accepted it."}


@app.post("/api/test_cloud")
def test_cloud(req: TestCloudRequest):
    """Settings TEST button: check an API key WITHOUT saving it. The key is
    sent only to the provider; neither a log line nor the JSON response ever
    contains it."""
    provider = (req.provider or "").strip().lower()
    if provider not in ("groq", "openrouter", "ollama"):
        provider = (CONFIG.cloud_provider() if CONFIG.brain_mode() == "dual"
                    else CONFIG.provider())
    if provider == "ollama":
        return {"ok": True, "message": "Ollama is local - no API key needed."}
    key = (req.api_key or "").strip() or str(CONFIG.data.get("api_key", "")).strip()
    if not key:
        return {"ok": False, "message": "No API key to test - paste a key first."}

    result = _test_cloud_key(provider, key)
    # Log only the verdict - never the key itself.
    print(f"🔑 test_cloud: {provider} → {'ok' if result['ok'] else 'failed'}", flush=True)
    return result


_models_cache = {"ts": 0.0, "models": []}
_ollama_cache = {"ts": 0.0, "models": [], "reachable": False, "error": ""}


def _fetch_ollama_models() -> dict:
    """List the models pulled into the local Ollama service (short cache).
    Returns reachable=False with a hint when localhost:11434 is down."""
    if time.time() - _ollama_cache["ts"] < 10 and _ollama_cache["models"]:
        return {
            "models": _ollama_cache["models"],
            "reachable": _ollama_cache["reachable"],
            "error": _ollama_cache["error"],
        }

    models, reachable, error = [], False, ""
    try:
        import urllib.request, json as _json
        with urllib.request.urlopen(app_config.OLLAMA_API_TAGS, timeout=3) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        for m in data.get("models", []):
            name = m.get("name") or m.get("model") or ""
            if name:
                models.append({"id": name, "name": name})
        models.sort(key=lambda m: m["id"])
        reachable = True
    except Exception as e:
        print(f"⚠️ Ollama model fetch failed: {e}")
        error = (f"Ollama is not reachable at {app_config.OLLAMA_BASE_URL.rsplit('/v1', 1)[0]}. "
                 "Start it with 'ollama serve' and pull a model (e.g. 'ollama pull llama3.2').")

    _ollama_cache.update(ts=time.time(), models=models, reachable=reachable, error=error)
    return {
        "models": models or [{"id": m, "name": m} for m in app_config.OLLAMA_FALLBACK_MODELS],
        "reachable": reachable,
        "error": error,
    }


@app.get("/api/models")
async def get_models(provider: str = "groq", only_tools: bool = True):
    """Settings dropdown model lists.

    OpenRouter: the chat always uses tool-calling, so by default the list is
    filtered to models whose `supported_parameters` include "tools" (audit
    task 5). Per model: field present -> keep only "tools" models; field
    absent -> the model stays (we can't know better). If the API sends no
    supported_parameters at all, the list is returned as is and
    "tools_filter" says "field_missing".
    """
    if provider == "ollama":
        return _fetch_ollama_models()
    if provider == "openrouter":
        # Live fetch with a short cache so the UI dropdown is fresh but snappy
        # (the cache holds the RAW list incl. supported_parameters; filtering
        # happens per request so only_tools=false sees everything).
        if time.time() - _models_cache["ts"] > 300 or not _models_cache["models"]:
            models = []
            try:
                import json as _json
                with urllib.request.urlopen(
                    "https://openrouter.ai/api/v1/models", timeout=10
                ) as resp:
                    data = _json.loads(resp.read().decode("utf-8"))
                for m in data.get("data", []):
                    entry = {
                        "id": m.get("id", ""),
                        "name": m.get("name", m.get("id", "")),
                    }
                    params = m.get("supported_parameters")
                    if isinstance(params, list):
                        entry["supported_parameters"] = params
                    models.append(entry)
                models.sort(key=lambda m: (not m["id"].startswith("openai/"), m["id"]))
            except Exception as e:
                print(f"⚠️ OpenRouter model fetch failed: {e}")
            _models_cache["ts"] = time.time()
            _models_cache["models"] = models or []
        if not _models_cache["models"]:
            # Fetch failed (or empty catalogue): static fallback list - it has
            # no supported_parameters, so nothing can be filtered or claimed.
            return {"models": [
                {"id": m, "name": m} for m in app_config.OPENROUTER_FALLBACK_MODELS
            ], "tools_filter": "fallback"}
        raw = _models_cache["models"]

        if not only_tools:
            return {"models": raw}
        if not any("supported_parameters" in m for m in raw):
            # Field not provided by this API response: keep the list and say so.
            print("ℹ️ OpenRouter /api/v1/models has no supported_parameters - "
                  "model list shown unfiltered")
            return {"models": raw, "tools_filter": "field_missing"}
        kept = [m for m in raw
                if "supported_parameters" not in m
                or "tools" in m["supported_parameters"]]
        dropped = len(raw) - len(kept)
        if dropped:
            print(f"ℹ️ OpenRouter: hid {dropped} model(s) without tools support "
                  "from the dropdown")
        return {"models": [
            {"id": m["id"], "name": m["name"]} for m in kept
        ], "tools_filter": "applied"}
    return {"models": [{"id": m, "name": m} for m in app_config.GROQ_MODELS]}


@app.get("/api/status")
async def get_status():
    data = core.get_status()
    data["server_time"] = time.time()
    return data


@app.post("/api/chat")
async def chat(req: ChatRequest):
    if not req.text.strip():
        return {"reply": "", "audio": None}

    reply = await core.fetch_ai_response(req.text.strip())

    audio = None
    if req.tts and reply:
        try:
            audio = await core.generate_speech(reply)
        except Exception as e:
            print(f"⚠️ TTS Error: {e}")

    return {"reply": reply, "audio": audio, "brain": core.LAST_BRAIN}


@app.post("/api/tts")
async def tts(req: TTSRequest):
    audio = await core.generate_speech(req.text)
    return {"audio": audio}


@app.get("/api/history")
async def get_history():
    return {"messages": core.get_history()}


@app.post("/api/clear")
async def clear_history():
    core.clear_history()
    return {"ok": True}


@app.get("/audio/{filename}")
async def get_audio(filename: str):
    file_path = AUDIO_DIR / filename
    if file_path.exists():
        return FileResponse(file_path, media_type="audio/mpeg")
    return {"error": "not found"}


# Serve the UI at the root so it works in a browser too
RENDERER_DIR = Path(__file__).parent.parent / "electron" / "renderer"
if RENDERER_DIR.exists():
    app.mount("/", StaticFiles(directory=RENDERER_DIR, html=True), name="ui")


def _pick_free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="JARVIS backend")
    parser.add_argument("--port", type=int, default=8765,
                        help="Port to listen on. 0 = pick a free port (used by the Electron app).")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    port = args.port
    if port == 0:
        port = _pick_free_port()
        # Electron spawns us and reads this line to learn the URL
        print(f"JARVIS_PORT={port}", flush=True)

    uvicorn.run(app, host=args.host, port=port)
