"""Cross-platform system operations for JARVIS (Linux + Windows 11).

Security note (command injection): every external program is started with
subprocess and an ARGUMENT LIST - there is no shell anywhere in this module,
so model-supplied strings can never be parsed as shell syntax. On top of that
`is_safe_label()` rejects control characters in names before they are used.
Where a string has to be tokenized (.desktop Exec line) shlex.split() is used.
"""
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

IS_WINDOWS = sys.platform.startswith("win")

APP_SEARCH_DIRS = []


def _init():
    global APP_SEARCH_DIRS
    if IS_WINDOWS:
        APP_SEARCH_DIRS = [
            os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
            os.path.join(os.environ.get("PROGRAMDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
        ]
    else:
        APP_SEARCH_DIRS = [
            os.path.expanduser("~/.local/share/applications"),
            "/usr/share/applications",
            "/usr/local/share/applications",
        ]


_init()


# ---------- input validation for tool arguments ----------
# Arguments arrive from the LLM (tool calls). subprocess + argument list already
# makes shell injection impossible; this is defence in depth on top: reject
# names containing shell/OS control characters or line breaks.
_UNSAFE_CHARS = set(";&|><\n\r") | {chr(i) for i in range(32)}


def is_safe_label(value) -> bool:
    """True for a plain application/process name: not empty, sane length,
    no control characters (;, &, |, >, <, newlines, ...)."""
    text = str(value or "")
    if not text or len(text) > 200:
        return False
    return not any(ch in _UNSAFE_CHARS for ch in text)


def _run_quiet(argv, timeout: int = 15) -> subprocess.CompletedProcess:
    """Run a program without a shell (argument list => nothing to quote/escape)."""
    return subprocess.run(argv, capture_output=True, timeout=timeout)


# ---------- applications ----------

def list_apps() -> list:
    """Return a list of available application names."""
    apps = set()
    if IS_WINDOWS:
        for base in APP_SEARCH_DIRS:
            if not os.path.isdir(base):
                continue
            for root, dirs, files in os.walk(base):
                for f in files:
                    if f.lower().endswith(".lnk"):
                        apps.add(os.path.splitext(f)[0])
        return sorted(apps)

    for base in APP_SEARCH_DIRS:
        if not os.path.isdir(base):
            continue
        for f in os.listdir(base):
            if not f.endswith(".desktop"):
                continue
            path = os.path.join(base, f)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        if line.startswith("Name="):
                            apps.add(line.strip()[5:])
                            break
            except Exception:
                continue
    return sorted(apps)


def _find_desktop_exec(app_name: str):
    """Return the Exec command for a matching .desktop file, or None."""
    target = app_name.lower()
    best = None
    for base in APP_SEARCH_DIRS:
        if not os.path.isdir(base):
            continue
        for f in os.listdir(base):
            if not f.endswith(".desktop"):
                continue
            path = os.path.join(base, f)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    name = ""
                    exec_cmd = ""
                    hidden = False
                    for line in fh:
                        if line.startswith("Name=") and not name:
                            name = line.strip()[5:]
                        elif line.startswith("Exec=") and not exec_cmd:
                            exec_cmd = line.strip()[5:]
                        elif line.startswith("Hidden="):
                            hidden = line.strip()[7:].strip() == "true"
                    if hidden:
                        continue
                    if name.lower() == target:
                        return exec_cmd
                    if target in name.lower() and best is None:
                        best = exec_cmd
            except Exception:
                continue
    return best


def open_app(app_name: str) -> str:
    if not app_name:
        return "No app name given."
    if not is_safe_label(app_name):
        return "Invalid application name (control characters are not allowed)."
    if IS_WINDOWS:
        exe_map = {
            "chrome": "chrome", "firefox": "firefox", "edge": "msedge",
            "discord": "discord", "steam": "steam", "spotify": "spotify",
            "calculator": "calc", "notepad": "notepad", "explorer": "explorer",
            "cmd": "cmd", "terminal": "wt", "vscode": "code", "code": "code",
        }
        exe = exe_map.get(app_name.lower(), app_name.lower())
        # No shell and no `start`: CreateProcess resolves bare names via PATH,
        # and a list argument cannot be reinterpreted as shell syntax.
        try:
            subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            return f"Could not open {app_name} ({e})."
        return f"Opened {app_name}."
    # Linux
    exec_cmd = _find_desktop_exec(app_name)
    if exec_cmd:
        exec_cmd = re.sub(r"%(f|u|F|U|i|c|k)", "", exec_cmd).strip()
        try:
            argv = shlex.split(exec_cmd)   # tokenize the .desktop Exec line
        except ValueError as e:
            return f"Could not open {app_name} ({e})."
        if not argv or not is_safe_label(argv[0]):
            return f"Could not open {app_name}: unusable .desktop Exec line."
        try:
            # replaces `nohup ... >/dev/null 2>&1 &` without involving a shell
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            return f"Could not open {app_name} ({e})."
        return f"Opened {app_name}."
    exe = shutil.which(app_name.lower())
    if exe:
        try:
            subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            return f"Could not open {app_name} ({e})."
        return f"Opened {app_name}."
    return f"App '{app_name}' not found on the system."


def close_app(app_name: str) -> str:
    if not app_name:
        return "No app name given."
    if not is_safe_label(app_name):
        return "Invalid application name (control characters are not allowed)."
    if IS_WINDOWS:
        exe_map = {
            "chrome": "chrome.exe", "firefox": "firefox.exe", "edge": "msedge.exe",
            "discord": "Discord.exe", "steam": "steam.exe", "spotify": "Spotify.exe",
            "notepad": "notepad.exe", "calculator": "CalculatorApp.exe", "calc": "CalculatorApp.exe",
            "vscode": "Code.exe", "code": "Code.exe", "explorer": "explorer.exe",
        }
        exe = exe_map.get(app_name.lower(), f"{app_name}.exe")
        # argument list => taskkill never sees shell syntax
        try:
            _run_quiet(["taskkill", "/F", "/IM", exe])
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"Could not close {app_name} ({e})."
        return f"Closed {app_name}."
    proc_map = {
        "chrome": "chrome", "firefox": "firefox", "edge": "microsoft-edge",
        "discord": "discord", "steam": "steam", "spotify": "spotify",
        "terminal": "kitty", "files": "nautilus", "code": "code", "vscode": "code",
    }
    proc = proc_map.get(app_name.lower(), app_name.lower())
    try:
        _run_quiet(["pkill", "-f", proc])
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"Could not close {app_name} ({e})."
    return f"Closed {app_name}."


# ---------- volume ----------

def set_volume(level: int) -> str:
    level = max(0, min(100, level))
    if IS_WINDOWS:
        try:
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = interface.QueryInterface(IAudioEndpointVolume)
            volume.SetMasterVolumeLevelScalar(level / 100.0, None)
        except Exception:
            return "Volume control failed."
    else:
        # level is a clamped int -> the argv is fully static
        try:
            _run_quiet(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{level}%"], timeout=10)
            _run_quiet(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"], timeout=10)
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"Volume control failed ({e})."
    return f"Volume set to {level}%."


def mute_volume(mute: bool) -> str:
    if IS_WINDOWS:
        try:
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = interface.QueryInterface(IAudioEndpointVolume)
            volume.SetMute(1 if mute else 0, None)
        except Exception:
            return "Volume control failed."
    else:
        try:
            _run_quiet(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1" if mute else "0"],
                       timeout=10)
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"Volume control failed ({e})."
    return "Muted." if mute else "Unmuted."


def volume_step(delta: int) -> str:
    if IS_WINDOWS:
        try:
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = interface.QueryInterface(IAudioEndpointVolume)
            current = volume.GetMasterVolumeLevelScalar()
            volume.SetMasterVolumeLevelScalar(max(0.0, min(1.0, current + delta / 100.0)), None)
        except Exception:
            return "Volume control failed."
    else:
        sign = "+" if delta > 0 else "-"
        try:
            _run_quiet(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{abs(delta)}%{sign}"],
                       timeout=10)
            _run_quiet(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"], timeout=10)
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"Volume control failed ({e})."
    return "Volume up." if delta > 0 else "Volume down."


# ---------- system ----------

def lock_screen() -> str:
    # static argv, no shell; NOT executed during tests - it locks the session
    try:
        if IS_WINDOWS:
            _run_quiet(["rundll32.exe", "user32.dll,LockWorkStation"], timeout=10)
        else:
            _run_quiet(["loginctl", "lock-session"], timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"Could not lock the screen ({e})."
    return "Screen locked."


def close_window() -> str:
    if IS_WINDOWS:
        try:
            import keyboard
            keyboard.send("alt+f4")
        except Exception:
            pass
    else:
        try:
            _run_quiet(["wmctrl", "-c", ":ACTIVE:"], timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return "Closed the active window."


def close_tab() -> str:
    if IS_WINDOWS:
        try:
            import keyboard
            keyboard.send("ctrl+w")
        except Exception:
            pass
    else:
        try:
            import keyboard
            keyboard.send("ctrl+w")
        except Exception:
            try:
                _run_quiet(["wmctrl", "-c", ":ACTIVE:"], timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                pass
    return "Closed the tab."


# ---------- files ----------

def find_files(query: str, max_results: int = 8) -> str:
    query = query.strip().lower()
    if not query:
        return "No file name given."
    home = os.path.expanduser("~")
    skip = {"node_modules", ".git", "__pycache__", ".cache", "venv", ".venv", "AppData"}
    matches = []
    for root, dirs, files in os.walk(home):
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        for f in files:
            if query in f.lower():
                matches.append(os.path.join(root, f))
                if len(matches) >= max_results:
                    return "\n".join(matches)
    return "No files found." if not matches else "\n".join(matches)


# ---------- web search ----------

def _http_get(url: str, timeout: int = 12) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _wikipedia_search(query: str, limit: int = 3) -> str:
    try:
        import json
        url = ("https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch="
               + urllib.parse.quote(query) + f"&format=json&srlimit={limit}")
        data = json.loads(_http_get(url))
        lines = []
        for item in data.get("query", {}).get("search", []):
            snippet = re.sub(r"<[^>]+>", "", item.get("snippet", "")).strip()
            lines.append(f"{item.get('title')}: {snippet}")
        return "\n".join(lines)
    except Exception as e:
        return f"Wikipedia search failed: {e}"


def _ddg_instant(query: str) -> str:
    try:
        import json
        url = ("https://api.duckduckgo.com/?q=" + urllib.parse.quote(query)
               + "&format=json&no_html=1&skip_disambig=1")
        data = json.loads(_http_get(url))
        abstract = data.get("AbstractText", "")
        if abstract:
            return f"{data.get('Heading', '')}: {abstract}"
        topics = [t.get("Text", "") for t in data.get("RelatedTopics", [])
                  if isinstance(t, dict) and t.get("Text")]
        return "\n".join(topics[:4])
    except Exception as e:
        return f"Search failed: {e}"


def web_search(query: str, max_results: int = 5) -> str:
    """Search the web via Wikipedia + DuckDuckGo Instant Answers (no API key)."""
    parts = []
    wiki = _wikipedia_search(query)
    if wiki and not wiki.startswith("Wikipedia search failed"):
        parts.append("WIKIPEDIA:\n" + wiki)
    ddg = _ddg_instant(query)
    if ddg and not ddg.startswith("Search failed") and ddg != "WIKIPEDIA:\n" + wiki:
        parts.append("OTHER SOURCES:\n" + ddg)

    if not parts:
        return "No web results found. Try asking a more specific question."
    return "\n\n".join(parts)


# ---------- time ----------

def current_time() -> str:
    import datetime
    now = datetime.datetime.now()
    return f"The current time is {now.strftime('%I:%M %p')} on {now.strftime('%A, %B %d, %Y')}."
