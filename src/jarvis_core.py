import os
import re
import json
import time
import asyncio
import datetime
import webbrowser
import base64

import edge_tts
from openai import OpenAI, APIConnectionError, APIStatusError

import platform_ops
import brain_router
import config as app_config

# NOTE: pyautogui is imported lazily inside the functions that need it.
# Importing it at module level after edge_tts (aiohttp's C extensions) crashes
# in the PyInstaller build (import-order dependent segfault).

CONFIG = app_config.AppConfig()

client_groq = None
ACTIVE_PROVIDER = "groq"
ACTIVE_MODEL = "llama-3.3-70b-versatile"
ACTIVE_VISION_MODEL = "qwen/qwen3.6-27b"

# --- Dual-brain clients (only used when brain_mode == "dual") ---
# The manual client above stays the single source of truth in manual mode;
# these are extra clients built from the same config for routed requests.
LOCAL_CLIENT = None          # Ollama (local brain)
LOCAL_MODEL = ""
CLOUD_CLIENT = None          # Groq / OpenRouter (cloud brain)
CLOUD_PROVIDER = "groq"
CLOUD_MODEL = ""
BRAIN_TAG = ""               # "[local]" | "[cloud]" for the request in flight
LAST_BRAIN = ""              # brain of the last completed reply (sent to the UI)


def reload_client():
    """(Re)build the OpenAI client from saved config. Called at startup and
    whenever the user saves new settings from the UI."""
    global client_groq, ACTIVE_PROVIDER, ACTIVE_MODEL, ACTIVE_VISION_MODEL
    global LOCAL_CLIENT, LOCAL_MODEL, CLOUD_CLIENT, CLOUD_PROVIDER, CLOUD_MODEL
    ACTIVE_PROVIDER = CONFIG.provider()
    ACTIVE_MODEL = CONFIG.active_model()
    ACTIVE_VISION_MODEL = CONFIG.active_vision_model()
    api_key = CONFIG.data.get("api_key", "").strip()
    if ACTIVE_PROVIDER == "ollama":
        # Ollama serves an OpenAI-compatible API locally and ignores auth,
        # but the client still requires some non-empty key value.
        base_url = app_config.OLLAMA_BASE_URL
        api_key = "ollama"
    elif ACTIVE_PROVIDER == "openrouter":
        base_url = "https://openrouter.ai/api/v1"
    else:
        base_url = "https://api.groq.com/openai/v1"
    client_groq = OpenAI(base_url=base_url, api_key=api_key or "missing")
    print(f"🤖 Provider: {ACTIVE_PROVIDER} | Model: {ACTIVE_MODEL}")

    # --- Dual-brain clients (built always, used only in brain_mode == "dual") ---
    stored_key = CONFIG.data.get("api_key", "").strip() or "missing"
    LOCAL_MODEL = CONFIG.local_model()
    LOCAL_CLIENT = OpenAI(base_url=app_config.OLLAMA_BASE_URL, api_key="ollama")
    CLOUD_PROVIDER = CONFIG.cloud_provider()
    CLOUD_MODEL = CONFIG.cloud_model()
    cloud_base = ("https://openrouter.ai/api/v1" if CLOUD_PROVIDER == "openrouter"
                  else "https://api.groq.com/openai/v1")
    CLOUD_CLIENT = OpenAI(base_url=cloud_base, api_key=stored_key)
    if CONFIG.brain_mode() == "dual":
        print(f"🧠 Brain mode: dual | local: {LOCAL_MODEL} | "
              f"cloud: {CLOUD_PROVIDER}/{CLOUD_MODEL}")
    else:
        print("🧠 Brain mode: manual")


reload_client()


# --- Ollama-specific failures: explain them instead of failing silently ---
OLLAMA_BASE_HINT = "Ollama is not reachable at http://localhost:11434."


def ollama_error_message(err: Exception) -> str:
    """Human-readable message for local Ollama problems, or "" for errors that
    are not Ollama-availability issues (so normal error handling stays intact)."""
    if isinstance(err, APIConnectionError):
        return (
            f"{OLLAMA_BASE_HINT} Start it with 'ollama serve' and pull a model "
            f"(e.g. 'ollama pull llama3.2'), then try again."
        )
    if isinstance(err, APIStatusError) and err.status_code == 404:
        return (
            f"Ollama model '{ACTIVE_MODEL}' is not available locally. "
            f"Pull it with 'ollama pull {ACTIVE_MODEL}' and try again."
        )
    low = str(err).lower()
    if any(t in low for t in ("connection refused", "econnrefused", "11434",
                              "failed to establish a new connection", "max retries exceeded")):
        return f"{OLLAMA_BASE_HINT} Start it with 'ollama serve' and try again."
    return ""


AUDIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

TTS_VOICE = "en-US-ChristopherNeural"

# --- Status / task tracking (polled by the UI) ---
STATUS = {
    "state": "idle",          # idle | thinking | executing | speaking | error
    "task": "",               # current task label
    "detail": "",             # detail for current task
    "ts": 0.0,                # timestamp current task started
    "last_task": "",          # last completed task label
    "last_detail": "",        # last completed detail
    "last_ts": 0.0,           # timestamp last task finished
    "finished": False,        # True when the last task completed (UI shows it 5s)
    "brain": ""               # "[local]" | "[cloud]" - which brain is handling it
}


def set_status(state: str, task: str = "", detail: str = ""):
    now = time.time()
    STATUS["state"] = state
    STATUS["brain"] = BRAIN_TAG
    if task:
        STATUS["task"] = task
        STATUS["detail"] = detail
        STATUS["ts"] = now
        STATUS["finished"] = False


def finish_status(detail: str = ""):
    now = time.time()
    if STATUS["task"]:
        STATUS["last_task"] = STATUS["task"]
        STATUS["last_detail"] = detail or STATUS["detail"]
        STATUS["last_ts"] = now
    STATUS["state"] = "idle"
    STATUS["task"] = ""
    STATUS["detail"] = ""
    STATUS["finished"] = True


def get_status():
    return dict(STATUS)


def _provider_brain_tag(provider: str) -> str:
    return "[local]" if provider == "ollama" else "[cloud]"


def cloud_key_present() -> bool:
    return bool(CONFIG.data.get("api_key", "").strip())


def resolve_brain(prompt: str):
    """Decide which brain handles this request.

    manual mode -> the provider from config decides everything (legacy
                   behaviour, byte-for-byte the same client/model as before)
    dual mode   -> brain_router.classify() sends simple prompts to the local
                   Ollama brain and hard ones to the configured cloud brain

    Returns (brain_tag, client, model, route_reason_for_the_logs).
    """
    if CONFIG.brain_mode() != "dual":
        return (_provider_brain_tag(ACTIVE_PROVIDER), client_groq, ACTIVE_MODEL,
                f"manual/{ACTIVE_PROVIDER}")
    brain, reason = brain_router.classify(prompt)
    if brain == "local":
        return "[local]", LOCAL_CLIENT, LOCAL_MODEL, reason
    return "[cloud]", CLOUD_CLIENT, CLOUD_MODEL, reason

conversation_history = [
    {
        "role": "system",
        "content": (
            "You are JARVIS, a highly capable AI assistant running on the user's computer. "
            "You have tools to control the computer: open apps, close apps, open websites, "
            "control volume, lock the screen, take screenshots, analyze the screen, close windows, "
            "close tabs, search the web, and find files. "
            "IMPORTANT RULES: "
            "1. If the user says 'open X' and X could be either an app or a website, FIRST call "
            "list_apps to check if it's installed. If found, open_application. If not, open the "
            "website. If it's genuinely ambiguous, briefly ask whether they want the app or website. "
            "2. Only use analyze_screen if the user explicitly asks about what's on their screen. "
            "3. For time questions, use get_current_time. "
            "4. For questions about facts/current events, use search_web. "
            "5. Reply concisely, like a voice assistant."
        )
    }
]

KNOWN_DOMAINS = {
    "youtube": "youtube.com", "google": "google.com", "github": "github.com",
    "reddit": "reddit.com", "twitter": "x.com", "x": "x.com",
    "facebook": "facebook.com", "instagram": "instagram.com", "linkedin": "linkedin.com",
    "wikipedia": "wikipedia.org", "stackoverflow": "stackoverflow.com",
    "netflix": "netflix.com", "spotify": "open.spotify.com", "amazon": "amazon.com",
    "ebay": "ebay.com", "gmail": "mail.google.com", "maps": "maps.google.com",
    "translate": "translate.google.com", "news": "news.google.com", "drive": "drive.google.com",
    "whatsapp": "web.whatsapp.com", "telegram": "web.telegram.org", "twitch": "twitch.tv",
    "discord": "discord.com", "steam": "store.steampowered.com", "roblox": "roblox.com",
    "apple": "apple.com", "microsoft": "microsoft.com", "stackexchange": "stackexchange.com",
    "medium": "medium.com", "quora": "quora.com", "pinterest": "pinterest.com",
    "tiktok": "tiktok.com", "snapchat": "snapchat.com", "duckduckgo": "duckduckgo.com",
    "bing": "bing.com", "yahoo": "yahoo.com", "hackernews": "news.ycombinator.com",
    "ycombinator": "news.ycombinator.com", "archive": "archive.org", "imdb": "imdb.com",
}


def build_website_url(site: str) -> str:
    site = site.strip().strip('"').strip("'")
    if not site:
        return ""
    if site.startswith(("http://", "https://")):
        return site
    if "." in site.split("/")[0] and not site.split("/")[0].startswith("www"):
        return f"https://{site}"
    clean = site.lower().replace(" ", "")
    if clean in KNOWN_DOMAINS:
        return f"https://{KNOWN_DOMAINS[clean]}"
    # try to extract first word as domain
    first = clean.split("/")[0].split(".")[0]
    if first in KNOWN_DOMAINS:
        return f"https://{KNOWN_DOMAINS[first]}"
    if "." in first:
        return f"https://{first}"
    return f"https://{first}.com"

ACTION_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "open_application",
            "description": "Open an application on the user's Linux system (e.g. discord, steam, spotify, terminal, files, code).",
            "parameters": {
                "type": "object",
                "properties": {"app": {"type": "string", "description": "Name of the application to open"}},
                "required": ["app"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "close_application",
            "description": "Close a running application on the user's Linux system.",
            "parameters": {
                "type": "object",
                "properties": {"app": {"type": "string", "description": "Name of the application to close"}},
                "required": ["app"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "open_website",
            "description": "Open a website in the default browser (e.g. youtube, google, github).",
            "parameters": {
                "type": "object",
                "properties": {"site": {"type": "string", "description": "Website name or URL to open"}},
                "required": ["site"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_volume",
            "description": "Set the master volume to a percentage, or mute/unmute, or change volume by steps.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["set", "mute", "unmute", "up", "down"], "description": "What to do with the volume"},
                    "level": {"type": "integer", "description": "Target volume percentage (0-100), only for 'set'"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "lock_screen",
            "description": "Lock the user's PC screen.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "take_screenshot",
            "description": "Take a screenshot and save it to the Pictures folder.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_screen",
            "description": "Capture and visually analyze the user's screen to answer a question about what is on it.",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string", "description": "The question about the screen contents"}},
                "required": ["question"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Get the current date and time of the user's system.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_apps",
            "description": "List the applications installed on the user's system. Use this to check whether something the user asked to 'open' is an installed app.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the web for information, facts, news, or anything the user asks about.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "The search query"}},
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "Find files on the user's computer by name.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "File name or part of it to search for"}},
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "close_tab",
            "description": "Close the current tab in the active browser window.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "close_window",
            "description": "Close the currently active window.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "clear_memory",
            "description": "Clear the conversation memory / forget the conversation history.",
            "parameters": {"type": "object", "properties": {}}
        }
    }
]


def capture_and_analyze_screen(prompt: str) -> str:
    screenshot_path = os.path.join(AUDIO_DIR, "jarvis_vision_temp.png")
    try:
        import pyautogui  # lazy: avoids frozen-build crash (see module header)
        print("📸 Capturing screen...")
        screenshot = pyautogui.screenshot()
        screenshot.save(screenshot_path)

        print("👁️ Analyzing visual context...")
        with open(screenshot_path, "rb") as img_file:
            base64_image = base64.b64encode(img_file.read()).decode('utf-8')

        response = client_groq.chat.completions.create(
            model=ACTIVE_VISION_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "You are JARVIS inspecting the user's screen. "
                                f"Answer this query based on what you see: {prompt}"
                            )
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{base64_image}"
                            }
                        }
                    ]
                }
            ],
            max_tokens=300
        )

        if os.path.exists(screenshot_path):
            os.remove(screenshot_path)

        content = response.choices[0].message.content.strip()
        cleaned_content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
        return cleaned_content if cleaned_content else content

    except Exception as e:
        print(f"⚠️ Screen Vision Error: {e}")
        if os.path.exists(screenshot_path):
            try:
                os.remove(screenshot_path)
            except Exception:
                pass
        return "Vision error, boss."


def execute_action(tool_name: str, tool_args: dict) -> str:
    try:
        set_status("executing", tool_name, str(tool_args))
        if tool_name == "open_application":
            result = platform_ops.open_app(tool_args.get("app", ""))
            finish_status(result)
            return result
        elif tool_name == "close_application":
            result = platform_ops.close_app(tool_args.get("app", ""))
            finish_status(result)
            return result
        elif tool_name == "open_website":
            site = tool_args.get("site", "").strip()
            url = build_website_url(site)
            if url:
                webbrowser.open(url)
                finish_status(f"Opened {site} in the browser")
                return f"Opened {site} in the browser ({url})."
            finish_status("No website given")
            return "No website given."
        elif tool_name == "set_volume":
            action = tool_args.get("action")
            if action == "set":
                result = platform_ops.set_volume(int(tool_args.get("level", 50)))
                finish_status(result)
                return result
            elif action == "mute":
                result = platform_ops.mute_volume(True)
                finish_status(result)
                return result
            elif action == "unmute":
                result = platform_ops.mute_volume(False)
                finish_status(result)
                return result
            elif action == "up":
                result = platform_ops.volume_step(5)
                finish_status(result)
                return result
            elif action == "down":
                result = platform_ops.volume_step(-5)
                finish_status(result)
                return result
        elif tool_name == "lock_screen":
            result = platform_ops.lock_screen()
            finish_status(result)
            return result
        elif tool_name == "take_screenshot":
            import pyautogui  # lazy: avoids frozen-build crash (see module header)
            shot_path = os.path.expanduser("~/Pictures/screenshot_jarvis.png")
            os.makedirs(os.path.dirname(shot_path), exist_ok=True)
            pyautogui.screenshot().save(shot_path)
            finish_status(f"Screenshot saved")
            return f"Screenshot saved to {shot_path}."
        elif tool_name == "analyze_screen":
            result = capture_and_analyze_screen(tool_args.get("question", "What is on my screen?"))
            finish_status("Screen analyzed")
            return result
        elif tool_name == "close_window":
            result = platform_ops.close_window()
            finish_status(result)
            return result
        elif tool_name == "close_tab":
            result = platform_ops.close_tab()
            finish_status(result)
            return result
        elif tool_name == "get_current_time":
            result = platform_ops.current_time()
            finish_status("Time retrieved")
            return result
        elif tool_name == "list_apps":
            apps = platform_ops.list_apps()
            result = "Installed apps: " + ", ".join(apps[:60]) if apps else "No apps found."
            finish_status("Apps listed")
            return result
        elif tool_name == "search_web":
            result = platform_ops.web_search(tool_args.get("query", ""))
            finish_status("Web search done")
            return result
        elif tool_name == "find_files":
            result = platform_ops.find_files(tool_args.get("query", ""))
            finish_status("Files found")
            return result
        elif tool_name == "clear_memory":
            global conversation_history
            conversation_history = [conversation_history[0]]
            finish_status("Memory cleared")
            return "Conversation memory cleared."
        finish_status()
        return "Action executed."
    except Exception as e:
        print(f"⚠️ Action Error ({tool_name}): {e}")
        finish_status(f"Failed: {e}")
        return f"Failed to execute {tool_name}."


async def fetch_ai_response(user_prompt: str) -> str:
    global conversation_history, BRAIN_TAG, LAST_BRAIN

    # Manual mode keeps the exact legacy check; in dual mode the local brain
    # works without any API key, so the key requirement moves to the cloud arm.
    if CONFIG.brain_mode() != "dual" and not CONFIG.is_configured():
        print("⚠️ No API key configured")
        set_status("error", "config required")
        return "No API key configured yet. Open settings to add your key."

    # --- Brain routing: decide BEFORE calling the LLM ---
    brain_tag, client, model, route_reason = resolve_brain(user_prompt)
    BRAIN_TAG = brain_tag
    LAST_BRAIN = brain_tag
    print(f"{brain_tag} route: {route_reason}")

    if brain_tag == "[cloud]" and CONFIG.brain_mode() == "dual" and not cloud_key_present():
        print("⚠️ Cloud brain has no API key")
        set_status("error", "cloud key required")
        return ("The cloud brain has no API key configured. Add one in settings "
                "(gear icon), or switch BRAIN to manual with the Ollama provider.")

    set_status("thinking", "processing", user_prompt[:60])
    conversation_history.append({"role": "user", "content": user_prompt})

    if len(conversation_history) > 9:
        conversation_history = [conversation_history[0]] + conversation_history[-8:]

    try:
        try:
            response = client.chat.completions.create(
                model=model,
                messages=conversation_history,
                tools=ACTION_TOOLS,
                temperature=0.6,
                max_tokens=400
            )
        except Exception as first_err:
            # Dual mode: if the local brain is down / has no model, retry once
            # with the cloud brain instead of failing the whole request.
            if (brain_tag == "[local]" and CONFIG.brain_mode() == "dual"
                    and ollama_error_message(first_err) and cloud_key_present()):
                print(f"{brain_tag} unavailable → falling back to [cloud]: {first_err}")
                brain_tag, client, model = "[cloud]", CLOUD_CLIENT, CLOUD_MODEL
                BRAIN_TAG = LAST_BRAIN = brain_tag
                response = client.chat.completions.create(
                    model=model,
                    messages=conversation_history,
                    tools=ACTION_TOOLS,
                    temperature=0.6,
                    max_tokens=400
                )
            else:
                raise
        message = response.choices[0].message
        cleaned_reply = ""

        # Tool execution loop: keep calling until model stops asking for tools (max 6 rounds)
        for _ in range(6):
            if message.tool_calls:
                conversation_history.append(message)
                for tool_call in message.tool_calls:
                    tool_name = tool_call.function.name
                    try:
                        tool_args = json.loads(tool_call.function.arguments or "{}")
                    except json.JSONDecodeError:
                        tool_args = {}
                    print(f"{BRAIN_TAG} 🧰 Executing: {tool_name}({tool_args})")
                    result = execute_action(tool_name, tool_args)
                    conversation_history.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result
                    })

                set_status("thinking", "formulating reply")
                message = client.chat.completions.create(
                    model=model,
                    messages=conversation_history,
                    tools=ACTION_TOOLS,
                    temperature=0.6,
                    max_tokens=400
                ).choices[0].message
                continue

            content = (message.content or "").strip()

            # Recover leaked <function=...> calls that the model printed as text
            leak_match = re.search(r"<function=([\w]+)(\{.*?\})?(?:</function>|>)", content)
            if leak_match and not cleaned_reply:
                tool_name = leak_match.group(1)
                try:
                    tool_args = json.loads(leak_match.group(2) or "{}")
                except json.JSONDecodeError:
                    tool_args = {}
                print(f"{BRAIN_TAG} 🧰 Executing (leaked): {tool_name}({tool_args})")
                result = execute_action(tool_name, tool_args)
                conversation_history.append(message)
                conversation_history.append({"role": "tool", "tool_call_id": "leak", "content": result})
                set_status("thinking", "formulating reply")
                message = client.chat.completions.create(
                    model=model,
                    messages=conversation_history,
                    tools=ACTION_TOOLS,
                    temperature=0.6,
                    max_tokens=400
                ).choices[0].message
                continue

            cleaned_reply = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
            if cleaned_reply:
                break
            break

        if not cleaned_reply:
            cleaned_reply = "Done."

        conversation_history.append({"role": "assistant", "content": cleaned_reply})
        set_status("idle", "complete", cleaned_reply[:60])
        STATUS["last_ts"] = time.time()
        print(f"{BRAIN_TAG} 🧠 reply: {cleaned_reply[:80]}")
        return cleaned_reply
    except Exception as e:
        # Ollama runs locally: explain availability problems instead of
        # returning a generic (silent) connection error. In dual mode this
        # also covers requests routed to the local brain.
        if brain_tag == "[local]":
            ollama_msg = ollama_error_message(e)
            if ollama_msg:
                print(f"❌ Ollama Error: {e}")
                set_status("error", "ollama unavailable")
                return ollama_msg
        err_str = str(e)
        if "tool_use_failed" in err_str or "failed_generation" in err_str:
            print("🔄 Tool call parsing failed, recovering from emitted call...")
            try:
                match = re.search(r"<function=([\w]+)(\{.*?\})?(?:</function>|>)", err_str)
                if match:
                    tool_name = match.group(1)
                    try:
                        tool_args = json.loads(match.group(2) or "{}")
                    except json.JSONDecodeError:
                        tool_args = {}
                    print(f"{BRAIN_TAG} 🧰 Executing (recovered): {tool_name}({tool_args})")
                    result = execute_action(tool_name, tool_args)
                    conversation_history.append({"role": "user", "content": f"[Action {tool_name} executed: {result}]"})
                else:
                    conversation_history.append({"role": "user", "content": user_prompt})

                follow_up = client.chat.completions.create(
                    model=model,
                    messages=conversation_history,
                    temperature=0.6,
                    max_tokens=300
                )
                retry_content = follow_up.choices[0].message.content.strip()
                cleaned_retry = re.sub(r'<think>.*?</think>', '', retry_content, flags=re.DOTALL).strip()
                conversation_history.append({"role": "assistant", "content": cleaned_retry})
                set_status("idle", "complete", cleaned_retry[:60])
                STATUS["last_ts"] = time.time()
                print(f"{BRAIN_TAG} 🧠 reply: {cleaned_retry[:80]}")
                return cleaned_retry
            except Exception:
                pass
        print(f"❌ Groq API Error: {e}")
        set_status("error", "connection error")
        return "Connection error."


async def generate_speech(text: str) -> str:
    """Generate TTS audio file, return relative filename (served at /audio/<name>)."""
    audio_file = f"jarvis_{datetime.datetime.now().strftime('%H%M%S_%f')}.mp3"
    path = os.path.join(AUDIO_DIR, audio_file)
    communicate = edge_tts.Communicate(text, TTS_VOICE)
    await communicate.save(path)
    return audio_file


def get_history():
    return conversation_history


def clear_history():
    global conversation_history
    conversation_history = [conversation_history[0]]
    return True
