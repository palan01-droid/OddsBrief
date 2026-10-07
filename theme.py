import json
import re

import ai

FONTS = ["Inter", "Lora", "Space Grotesk", "IBM Plex Sans", "Merriweather", "DM Sans", "Fraunces", "Work Sans"]

DEFAULT = {
    "bg": "#ffffff", "card": "#ffffff", "text": "#222222", "muted": "#666666",
    "accent": "#1a56db", "up": "#15803d", "down": "#c00000", "font": "Inter", "mood": "",
}

THEME_PROMPT = """You design the look of a web page about today's prediction markets.
Pick colors and a font that fit today's mood. Today's biggest stories:
{stories}

Reply with JSON only, with these keys:
"bg" (page background), "card" (card background), "text" (main text), "muted" (secondary text),
"accent" (headings and links), "up" (price going up), "down" (price going down) - all hex colors like "#1a2b3c".
"font": one of {fonts}.
"mood": a 2-4 word name for the theme.
"emojis": a list with one emoji for each story, in the same order.
Keep text easy to read on the backgrounds."""


def luminance(hex_color):
    rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = sorted([luminance(a), luminance(b)], reverse=True)
    return (la + 0.05) / (lb + 0.05)


def is_readable(t):
    # WCAG: 4.5 for normal text, 3 for big numbers and secondary text
    return (contrast(t["text"], t["bg"]) >= 4.5 and contrast(t["text"], t["card"]) >= 4.5
            and contrast(t["muted"], t["card"]) >= 3 and contrast(t["accent"], t["bg"]) >= 3
            and contrast(t["up"], t["card"]) >= 3 and contrast(t["down"], t["card"]) >= 3)


def mix(color, target, amount):
    a = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(target[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(a, b))


def make_readable(color, background, minimum):
    # push a color toward black or white (whichever is further from the background) until it's readable
    target = "#000000" if luminance(background) > 0.18 else "#ffffff"
    for step in range(11):
        fixed = mix(color, target, step / 10)
        if contrast(fixed, background) >= minimum:
            return fixed
    return target


def check(raw, n_stories):
    t = json.loads(raw)
    theme = {}
    for k in ("bg", "card", "text", "muted", "accent", "up", "down"):
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(t.get(k, ""))):
            return None
        theme[k] = t[k].lower()

    # cards must be on the same light/dark side as the page, or no text color can work on both
    if (luminance(theme["bg"]) > 0.18) != (luminance(theme["card"]) > 0.18):
        theme["card"] = theme["bg"]
    theme["text"] = make_readable(theme["text"], theme["card"], 4.5)
    theme["text"] = make_readable(theme["text"], theme["bg"], 4.5)
    theme["accent"] = make_readable(theme["accent"], theme["bg"], 3)
    for k in ("muted", "up", "down"):
        theme[k] = make_readable(theme[k], theme["card"], 3)
    if not is_readable(theme):
        return None
    theme["font"] = t.get("font") if t.get("font") in FONTS else "Inter"
    theme["mood"] = str(t.get("mood", ""))[:40]
    emojis = t.get("emojis") if isinstance(t.get("emojis"), list) else []
    theme["emojis"] = [str(e)[:8] for e in emojis[:n_stories]] + [""] * (n_stories - len(emojis))
    return theme


def pick_theme(movers):
    stories = "\n".join(f"- {m['event']} ({m['tag'] or m['category']})" for m in movers)
    for attempt in range(2):
        try:
            theme = check(ai.ask(THEME_PROMPT.format(stories=stories, fonts=", ".join(FONTS)), json_mode=True), len(movers))
            if theme:
                return theme
        except Exception as e:
            print("theme failed:", e)
    return dict(DEFAULT, emojis=[""] * len(movers))


if __name__ == "__main__":
    assert round(contrast("#000000", "#ffffff")) == 21
    assert is_readable(DEFAULT)
    good = json.dumps(dict(DEFAULT, emojis=["🏈"]))
    assert check(good, 2)["emojis"] == ["🏈", ""]
    fixed = check(json.dumps(dict(DEFAULT, text="#eeeeee")), 1)
    assert contrast(fixed["text"], "#ffffff") >= 4.5, "light gray on white gets darkened"
    dark = check(json.dumps(dict(DEFAULT, bg="#101010", card="#ffffff", text="#333333", muted="#444444",
                                 accent="#222222", up="#0a3", down="#a00")), 1)
    assert dark is None, "bad hex (#0a3) still rejected"
    dark = check(json.dumps(dict(DEFAULT, bg="#101010", card="#ffffff", text="#333333", muted="#444444",
                                 accent="#222222", up="#00aa33", down="#aa0000")), 1)
    assert dark["card"] == "#101010" and is_readable(dark), "light card on dark page gets fixed"
    assert check(json.dumps(dict(DEFAULT, bg="red")), 1) is None, "not a hex color"
    assert check(json.dumps(dict(DEFAULT, font="Comic Sans")), 1)["font"] == "Inter"
    print("theme.py ok")
