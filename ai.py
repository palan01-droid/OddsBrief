import os

import requests

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")


def ask(prompt, system="", json_mode=False):
    # use gemini if there's an api key, otherwise use llama3.2 running locally with ollama
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        url = "https://generativelanguage.googleapis.com/v1beta/models/" + GEMINI_MODEL + ":generateContent"
        r = requests.post(url, headers={"x-goog-api-key": key}, json=body, timeout=60)
        r.raise_for_status()
        answer = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return answer.strip()

    body = {
        "model": "llama3.2",
        "prompt": prompt,
        "system": system,
        "stream": False,
        "options": {"temperature": 0},
    }
    if json_mode:
        body["format"] = "json"
    r = requests.post("http://localhost:11434/api/generate", json=body, timeout=120)
    r.raise_for_status()
    return r.json()["response"].strip()
