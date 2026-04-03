import os
import re
import sys
import glob
import subprocess
import tempfile
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from openai import OpenAI

app = Flask(__name__, static_folder=".")
CORS(app)

# LM Studio default endpoint — user can override via env var
LMSTUDIO_BASE_URL = os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADED_SUBTITLES_DIR = os.path.join(_APP_DIR, "downloaded-subtitles")


def _safe_filename_stem(name: str) -> str:
    s = re.sub(r"[^\w\s-]", "", name, flags=re.UNICODE)
    s = re.sub(r"[-\s]+", "-", s).strip("-_.")
    return (s[:200] if s else "transcript")


def save_plaintext_transcript(transcript: str, vtt_path: str, lang: str) -> str:
    """Write plain text to downloaded-subtitles/. Returns absolute path."""
    os.makedirs(DOWNLOADED_SUBTITLES_DIR, exist_ok=True)
    stem = _safe_filename_stem(os.path.splitext(os.path.basename(vtt_path))[0])
    base = f"{stem}.{lang}.txt"
    path = os.path.join(DOWNLOADED_SUBTITLES_DIR, base)
    n = 0
    while os.path.exists(path):
        n += 1
        base = f"{stem}.{lang}_{n}.txt"
        path = os.path.join(DOWNLOADED_SUBTITLES_DIR, base)
    with open(path, "w", encoding="utf-8") as f:
        f.write(transcript)
    return path


def download_subtitles(url: str, lang: str, tmpdir: str) -> str:
    """Download subtitles using yt-dlp into tmpdir. Returns path to .vtt file."""
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--write-sub",
        "--write-auto-sub",
        "--sub-lang", lang,
        "--skip-download",
        "--no-playlist",
        "-o", os.path.join(tmpdir, "%(title)s.%(ext)s"),
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp error: {result.stderr.strip()}")

    vtt_files = glob.glob(os.path.join(tmpdir, "*.vtt"))
    if not vtt_files:
        raise FileNotFoundError(
            f"No subtitle file found. The video may not have subtitles in "
            f"language '{lang}', or the URL is invalid.\n\nyt-dlp output:\n{result.stdout}"
        )

    return vtt_files[0]


def vtt_to_text(vtt_path: str) -> str:
    """Convert a .vtt file to clean plain text."""
    with open(vtt_path, encoding="utf-8") as f:
        content = f.read()

    content = re.sub(r"WEBVTT.*?\n", "", content)
    content = re.sub(r"NOTE\s.*?\n\n", "", content, flags=re.DOTALL)
    content = re.sub(
        r"\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[.,]\d{3}[^\n]*", "", content
    )
    content = re.sub(r"<[^>]+>", "", content)
    content = re.sub(r"\d+\n", "", content)

    seen = set()
    lines = []
    for line in content.splitlines():
        line = line.strip()
        if line and line not in seen:
            seen.add(line)
            lines.append(line)

    return " ".join(lines)


def summarize_with_lmstudio(transcript: str, base_url: str, model: str, api_key: str = "") -> str:
    """Send transcript to LM Studio and return the summary."""
    # Use provided key as Bearer token; fall back to dummy value if not set
    client = OpenAI(base_url=base_url, api_key=api_key or "lm-studio")

    if len(transcript) > 90_000:
        transcript = transcript[:90_000] + "\n\n[transcript truncated]"

    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You are a helpful assistant that summarizes video transcripts clearly and concisely.",
            },
            {
                "role": "user",
                "content": (
                    "Please provide a clear, structured summary of the following video transcript. "
                    "Include: a brief overview, the main topics covered, and key takeaways.\n\n"
                    f"TRANSCRIPT:\n{transcript}"
                ),
            },
        ],
        temperature=0.3,
    )

    if not response.choices:
        raise RuntimeError("Model returned no choices (empty completion).")

    msg = response.choices[0].message
    raw = getattr(msg, "content", None) if msg else None
    if raw is None and msg is not None:
        raw = getattr(msg, "refusal", None)

    if isinstance(raw, list):
        parts = []
        for p in raw:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text") or "")
            elif hasattr(p, "text"):
                parts.append(getattr(p, "text", "") or "")
            elif isinstance(p, str):
                parts.append(p)
        text = "".join(parts)
    elif raw is None:
        text = ""
    else:
        text = str(raw)

    text = text.strip()
    if not text:
        raise RuntimeError(
            "Model returned an empty summary (no text in message.content). "
            "Try another model or check LM Studio server logs."
        )
    return text


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/styles.css")
def styles():
    return send_from_directory(_APP_DIR, "styles.css", mimetype="text/css")


@app.route("/api/models", methods=["GET"])
def get_models():
    """Proxy LM Studio's model list so the frontend can populate a dropdown."""
    base_url = request.args.get("base_url", LMSTUDIO_BASE_URL).rstrip("/")
    api_key  = request.args.get("api_key", "").strip()
    try:
        client = OpenAI(base_url=base_url, api_key=api_key or "lm-studio")
        models = client.models.list()
        names = [m.id for m in models.data]
        return jsonify({"models": names})
    except Exception as e:
        return jsonify({"error": str(e), "models": []}), 200  # soft fail


@app.route("/api/summarize", methods=["POST"])
def summarize():
    data = request.get_json()
    url      = (data.get("url") or "").strip()
    lang     = (data.get("lang") or "en").strip()
    base_url = (data.get("base_url") or LMSTUDIO_BASE_URL).rstrip("/")
    model    = (data.get("model") or "").strip()
    api_key  = (data.get("api_key") or "").strip()

    if not url:
        return jsonify({"error": "No URL provided."}), 400
    if not model:
        return jsonify({"error": "No model selected. Is LM Studio running?"}), 400

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            vtt_path   = download_subtitles(url, lang, tmpdir)
            transcript = vtt_to_text(vtt_path)

        if not transcript.strip():
            return jsonify({"error": "Subtitle file was empty after parsing."}), 400

        saved_path = save_plaintext_transcript(transcript, vtt_path, lang)
        summary = summarize_with_lmstudio(transcript, base_url, model, api_key)
        rel_saved = os.path.relpath(saved_path, _APP_DIR)
        return jsonify(
            {
                "summary": summary,
                "transcript_length": len(transcript),
                "saved_transcript": rel_saved.replace(os.sep, "/"),
            }
        )

    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
