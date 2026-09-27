"""Dual-brain request router: is this prompt "local" or "cloud"?

Plain heuristic - no ML model, no network calls. jarvis_core only calls
classify(prompt) and gets back ("local" | "cloud", rule name for the logs).

TUNING: everything lives in ROUTING_RULES / DEFAULT_BRAIN below.

    ROUTING_RULES is an ordered list; the FIRST rule whose conditions all match
    wins, otherwise DEFAULT_BRAIN is used. A rule may contain any of:

        brain        "local" | "cloud"    where the request should go
        name         str                  shown in the logs as the route reason
        min_chars    int                  prompt length >= this
        min_words    int                  word count >= this
        max_chars    int                  prompt length <= this
        max_words    int                  word count <= this
        keywords     [str, ...]           case-insensitive SUBSTRING match on the
                                          whole prompt - use stems so Russian
                                          endings still match ("объясн" catches
                                          объясни/объяснить/объяснение)
        patterns     [regex, ...]         case-insensitive regex search - use for
                                          English where word boundaries matter
                                          (r"\\bwrite code\\b")

Conditions inside ONE rule are AND-ed, so when keywords and patterns are two
ways of saying the same thing, put them in TWO separate rules (rule 2/3 below
do exactly that). Example - "short prompt that mentions volume" goes local:

    {"brain": "local", "name": "short volume command",
     "max_chars": 80, "keywords": ["громкост", "volume"]}

Quick check from the repo root:
    venv/bin/python src/brain_router.py "объясни как работает рекурсия"
"""

import re

# What to do with a prompt that matches nothing (medium length, no keywords).
# "cloud" = quality first, "local" = speed/privacy first.
DEFAULT_BRAIN = "cloud"

ROUTING_RULES = [
    # --- 1. long context / multi-step -> cloud ---
    {"brain": "cloud", "name": "long prompt (>400 chars)",
     "min_chars": 400},
    {"brain": "cloud", "name": "long prompt (>60 words)",
     "min_words": 60},

    # --- 2. reasoning, code, generation, comparison -> cloud ---
    {"brain": "cloud", "name": "reasoning/code keywords",
     "keywords": [
         # Russian (stems)
         "объясн", "почему", "сравн", "напиши код", "напиши скрипт",
         "напиши программ", "сгенерируй", "допиши", "рефактор", "оптимизируй",
         "пошаг", "план", "разбер", "проанализ", "докаж", "придумай",
         "посчитай", "реши задач", "переведи", "напиши эссе", "напиши текст",
         "расскажи про", "как работает", "в чем разница",
         "посовет", "порекоменд", "прикинь",
         # English
         "explain", "compare", "write code", "write a script", "refactor",
         "optimize", "step by step", "design ", "implement", "debug",
         "analyze", "prove", "summar", "translate", "draft ", "pros and cons",
         "suggest", "recommend", "give me ideas",
     ]},
    {"brain": "cloud", "name": "reasoning regex patterns",
     "patterns": [
         r"\bwhy\b", r"\bhow (does|do|to)\b", r"\bwrite\b.*\b(code|script|function|app)\b",
         r"\bgenerat", r"\bregex\b", r"\bsql\b", r"\bapi\b", r"\balgorithm\b",
         r"\bthe difference\b", r"\bdiff between\b", r"\bconvert\b", r"\bconvert the\b",
         r"\bоцен", r"\bсравн",   # word-boundary Cyrillic (avoid "процентов" ~ "оцен")
     ]},

    # --- 3. explicit built-in tool intent -> local (fast, cheap) ---
    {"brain": "local", "name": "builtin tool command",
     "keywords": [
         # time
         "который час", "сколько времени", "текущее время", "какое сегодня",
         "what time", "what's the date", "what is the date",
         # apps / windows / tabs
         "открой", "закрой", "запусти", "выключи программ", "список програм",
         "какие програм", "установленн", "закрой вкладк", "закрой окн",
         "open app", "close app", "list apps", "installed apps", "close window",
         "close tab",
         # websites
         "открой сайт", "открой ютуб", "зайди на", "open site", "open youtube",
         # volume / lock / screenshot / screen
         "громкост", "звук", "заглуш", "без звука", "заблокируй", "блокировка",
         "скриншот", "снимок экрана", "что на экране", "что у меня на экране",
         "volume", "mute", "unmute", "lock screen", "screenshot",
         "what's on my screen", "what is on my screen",
         # files / search / memory
         "найди файл", "найди в системе", "поиск в интернет", "найди в интернет",
         "погод", "новост", "what's the weather", "search the web", "find file",
         "очисти память", "забудь", "clear memory", "forget",
     ]},
    {"brain": "local", "name": "builtin tool regex patterns",
     "patterns": [
         r"\bwhat time\b", r"\bopen\b", r"\bclose\b", r"\bvolume\b",
         r"\block\b", r"\bscreenshot\b", r"\blist (my )?apps\b", r"\bset .{0,12}volume\b",
     ]},

    # --- 4. short prompt -> local (this is what dual mode is for) ---
    {"brain": "local", "name": "short prompt (<=100 chars / 14 words)",
     "max_chars": 100, "max_words": 14},
]


def _rule_matches(rule: dict, raw: str, text: str) -> bool:
    """All conditions present in the rule must hold (AND)."""
    if "min_chars" in rule and len(raw) < rule["min_chars"]:
        return False
    if "max_chars" in rule and len(raw) > rule["max_chars"]:
        return False
    words = len(raw.split())
    if "min_words" in rule and words < rule["min_words"]:
        return False
    if "max_words" in rule and words > rule["max_words"]:
        return False
    if "keywords" in rule:
        if not any(str(kw).lower() in text for kw in rule["keywords"]):
            return False
    if "patterns" in rule:
        if not any(re.search(pat, raw, re.IGNORECASE) for pat in rule["patterns"]):
            return False
    # a rule with no conditions at all would match everything - that is allowed
    # (handy as an explicit catch-all), so nothing else to check here.
    return True


def classify(prompt: str):
    """Return (brain, reason) where brain is "local" | "cloud"."""
    raw = str(prompt or "")
    text = raw.lower()
    for rule in ROUTING_RULES:
        if _rule_matches(rule, raw, text):
            return rule.get("brain", "cloud"), rule.get("name", "unnamed rule")
    return DEFAULT_BRAIN, "default (no rule matched)"


if __name__ == "__main__":
    import sys

    samples = sys.argv[1:] or [
        "what time is it",
        "открой ютуб",
        "объясни как работает рекурсия и напиши пример кода",
        "сравни python и go",
        "привет",
        "сделай громче",
        "Напиши мне пять вариантов заголовка для статьи про локальные LLM "
        "и объясни, чем каждый из них хорош",
    ]
    for s in samples:
        brain, why = classify(s)
        print(f"[{brain:6}] {why:34} | {s[:70]}")
