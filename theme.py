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


def to_rgb(hex_color):
    # "#1a2b3c" -> [26, 43, 60]
    return [int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)]


def luminance(hex_color):
    # how bright a color looks, from the WCAG formula
    values = []
    for c in to_rgb(hex_color):
        c = c / 255
        if c <= 0.03928:
            values.append(c / 12.92)
        else:
            values.append(((c + 0.055) / 1.055) ** 2.4)
    r, g, b = values
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    # 1 = same color, 21 = black on white
    lighter = max(luminance(a), luminance(b))
    darker = min(luminance(a), luminance(b))
    return (lighter + 0.05) / (darker + 0.05)


def is_readable(t):
    # WCAG says 4.5 for normal text, 3 for big text
    if contrast(t["text"], t["bg"]) < 4.5 or contrast(t["text"], t["card"]) < 4.5:
        return False
    if contrast(t["accent"], t["bg"]) < 3:
        return False
    for key in ["muted", "up", "down"]:
        if contrast(t[key], t["card"]) < 3:
            return False
    return True


def mix(color, target, amount):
    # move a color part of the way toward another color
    a = to_rgb(color)
    b = to_rgb(target)
    result = "#"
    for i in range(3):
        value = round(a[i] + (b[i] - a[i]) * amount)
        result += format(value, "02x")
    return result


def make_readable(color, background, minimum):
    # darken (or lighten on dark backgrounds) the color until it's readable
    if luminance(background) > 0.18:
        target = "#000000"
    else:
        target = "#ffffff"
    for step in range(11):
        fixed = mix(color, target, step / 10)
        if contrast(fixed, background) >= minimum:
            return fixed
    return target


def check(raw, n_stories):
    # make sure what the AI sent back is usable, fix colors that are hard to read
    t = json.loads(raw)
    theme = {}
    for key in ["bg", "card", "text", "muted", "accent", "up", "down"]:
        color = str(t.get(key, ""))
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            return None
        theme[key] = color.lower()

    # if the page is light and the cards are dark (or the other way), no text color works on both
    page_is_light = luminance(theme["bg"]) > 0.18
    card_is_light = luminance(theme["card"]) > 0.18
    if page_is_light != card_is_light:
        theme["card"] = theme["bg"]

    theme["text"] = make_readable(theme["text"], theme["card"], 4.5)
    theme["text"] = make_readable(theme["text"], theme["bg"], 4.5)
    theme["accent"] = make_readable(theme["accent"], theme["bg"], 3)
    for key in ["muted", "up", "down"]:
        theme[key] = make_readable(theme[key], theme["card"], 3)
    if not is_readable(theme):
        return None

    if t.get("font") in FONTS:
        theme["font"] = t["font"]
    else:
        theme["font"] = "Inter"
    theme["mood"] = str(t.get("mood", ""))[:40]

    # one emoji per story, blank if the AI didn't give enough
    emojis = t.get("emojis")
    if not isinstance(emojis, list):
        emojis = []
    theme["emojis"] = []
    for i in range(n_stories):
        if i < len(emojis):
            theme["emojis"].append(str(emojis[i])[:8])
        else:
            theme["emojis"].append("")
    return theme


def default_theme(n_stories):
    theme = DEFAULT.copy()
    theme["emojis"] = [""] * n_stories
    return theme


def pick_theme(movers):
    stories = ""
    for m in movers:
        stories += "- " + m["event"] + " (" + (m["tag"] or m["category"]) + ")\n"
    prompt = THEME_PROMPT.format(stories=stories, fonts=", ".join(FONTS))

    # try twice, then give up and use the plain theme
    for attempt in range(2):
        try:
            theme = check(ai.ask(prompt, json_mode=True), len(movers))
            if theme:
                return theme
        except Exception as e:
            print("theme failed:", e)
    return default_theme(len(movers))


# quick test: python theme.py
if __name__ == "__main__":
    def test_theme(**changes):
        t = DEFAULT.copy()
        t.update(changes)
        return json.dumps(t)

    assert round(contrast("#000000", "#ffffff")) == 21
    assert is_readable(DEFAULT)
    assert check(test_theme(emojis=["🏈"]), 2)["emojis"] == ["🏈", ""]

    fixed = check(test_theme(text="#eeeeee"), 1)
    assert contrast(fixed["text"], "#ffffff") >= 4.5, "light gray on white should get darker"

    dark = check(test_theme(bg="#101010", card="#ffffff", text="#333333", muted="#444444",
                            accent="#222222", up="#0a3", down="#a00"), 1)
    assert dark is None, "#0a3 isn't a full hex color"

    dark = check(test_theme(bg="#101010", card="#ffffff", text="#333333", muted="#444444",
                            accent="#222222", up="#00aa33", down="#aa0000"), 1)
    assert dark["card"] == "#101010" and is_readable(dark), "light cards on a dark page should get fixed"

    assert check(test_theme(bg="red"), 1) is None
    assert check(test_theme(font="Comic Sans"), 1)["font"] == "Inter"
    print("theme.py ok")
