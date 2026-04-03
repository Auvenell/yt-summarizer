import json
import os
import re
import sys
import glob
import subprocess
import tempfile
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
from flask_cors import CORS
from openai import OpenAI

app = Flask(__name__, static_folder=".")
CORS(app)

# LM Studio default endpoint — user can override via env var
LMSTUDIO_BASE_URL = os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADED_SUBTITLES_DIR = os.path.join(_APP_DIR, "downloaded-subtitles")

def _safe_downloaded_subtitle_path(rel_path: str) -> str:
    """
    Convert a client-provided relative path like 'downloaded-subtitles/x.en.txt'
    into an absolute path under DOWNLOADED_SUBTITLES_DIR. Raises ValueError if invalid.
    """
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise ValueError("Missing subtitle path.")
    p = rel_path.replace("\\", "/").lstrip("/")
    prefix = "downloaded-subtitles/"
    if not p.startswith(prefix):
        raise ValueError("Invalid subtitle path.")
    leaf = p[len(prefix):]
    if not leaf or "/" in leaf:
        raise ValueError("Invalid subtitle filename.")
    abs_path = os.path.abspath(os.path.join(DOWNLOADED_SUBTITLES_DIR, leaf))
    base = os.path.abspath(DOWNLOADED_SUBTITLES_DIR)
    if os.path.commonpath([abs_path, base]) != base:
        raise ValueError("Invalid subtitle path.")
    return abs_path


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


def _raw_content_to_text(raw) -> str:
    """Normalize message.content or delta.content (str, list of parts, or None) to plain text."""
    if isinstance(raw, list):
        parts = []
        for p in raw:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text") or "")
            elif hasattr(p, "text"):
                parts.append(getattr(p, "text", "") or "")
            elif isinstance(p, str):
                parts.append(p)
        return "".join(parts)
    if raw is None:
        return ""
    return str(raw)


MAX_TRANSCRIPT_CHARS = 90_000

# Same system string for /api/summarize and /api/chat so LM Studio can prefix-cache
# the shared system + transcript user block across both requests.
SHARED_SYSTEM = (
    "You are a helpful assistant that summarizes video transcripts clearly and concisely."
)

SUMMARIZE_INSTRUCTION_USER = (
    "Please provide a clear, structured summary of the video transcript in your previous "
    "message. Include: a brief overview, the main topics covered, and key takeaways."
)

CHAT_MODE_USER = (
    "Answer my questions using only the transcript from the first user message in this "
    "conversation. Quote or paraphrase accurately; if something is not in the transcript, say so."
)


def _transcript_user_block(transcript: str) -> str:
    """Identical transcript payload for summarize and chat (must stay byte-stable for caching)."""
    t = transcript or ""
    if len(t) > MAX_TRANSCRIPT_CHARS:
        t = t[:MAX_TRANSCRIPT_CHARS] + "\n\n[transcript truncated]"
    return f"TRANSCRIPT:\n{t}"


def _summarize_messages(transcript: str) -> list:
    return [
        {"role": "system", "content": SHARED_SYSTEM},
        {"role": "user", "content": _transcript_user_block(transcript)},
        {"role": "user", "content": SUMMARIZE_INSTRUCTION_USER},
    ]


def iter_summarize_stream(transcript: str, base_url: str, model: str, api_key: str = ""):
    """Stream summary text chunks from LM Studio (OpenAI-compatible)."""
    client = OpenAI(base_url=base_url, api_key=api_key or "lm-studio")
    stream = client.chat.completions.create(
        model=model,
        messages=_summarize_messages(transcript),
        temperature=0.3,
        stream=True,
    )
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta is None:
            continue
        raw = getattr(delta, "content", None)
        if raw is None:
            raw = getattr(delta, "refusal", None)
        piece = _raw_content_to_text(raw)
        if piece:
            yield piece


def _normalize_chat_tail(messages: list, limit: int = 4) -> list:
    cleaned = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        cleaned.append({"role": role, "content": content})
    return cleaned[-limit:] if limit else cleaned


def iter_chat_stream(messages: list, base_url: str, model: str, api_key: str = ""):
    """Stream chat completion text chunks from LM Studio (OpenAI-compatible)."""
    client = OpenAI(base_url=base_url, api_key=api_key or "lm-studio")
    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.4,
        stream=True,
    )
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta is None:
            continue
        raw = getattr(delta, "content", None)
        if raw is None:
            raw = getattr(delta, "refusal", None)
        piece = _raw_content_to_text(raw)
        if piece:
            yield piece


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/styles.css")
def styles():
    return send_from_directory(_APP_DIR, "styles.css", mimetype="text/css")


@app.route("/app.js")
def app_js():
    return send_from_directory(_APP_DIR, "app.js", mimetype="application/javascript")


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
        rel_saved = os.path.relpath(saved_path, _APP_DIR)
        meta = {
            "transcript_length": len(transcript),
            "saved_transcript": rel_saved.replace(os.sep, "/"),
            "transcript": transcript,
        }

        def event_stream():
            yield f"event: ready\ndata: {json.dumps(meta)}\n\n"
            total = []
            try:
                for piece in iter_summarize_stream(transcript, base_url, model, api_key):
                    total.append(piece)
                    yield f"event: token\ndata: {json.dumps({'t': piece})}\n\n"
                full = "".join(total).strip()
                if not full:
                    err = (
                        "Model returned an empty summary (no streamed text). "
                        "Try another model or check LM Studio server logs."
                    )
                    yield f"event: error\ndata: {json.dumps({'error': err})}\n\n"
                    return
                yield "event: done\ndata: {}\n\n"
            except Exception as e:
                yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

        return Response(
            stream_with_context(event_stream()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"}), 500


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json() or {}
    transcript = data.get("transcript")
    transcript = transcript.strip() if isinstance(transcript, str) else ""
    raw_messages = data.get("messages")
    if not isinstance(raw_messages, list):
        raw_messages = []
    base_url = (data.get("base_url") or LMSTUDIO_BASE_URL).rstrip("/")
    model = (data.get("model") or "").strip()
    api_key = (data.get("api_key") or "").strip()

    if not transcript:
        return jsonify({"error": "Transcript is required for chat."}), 400
    if not model:
        return jsonify({"error": "No model selected."}), 400

    tail = _normalize_chat_tail(raw_messages, 4)
    if not tail:
        return jsonify({"error": "No chat messages to process."}), 400
    if tail[-1]["role"] != "user":
        return jsonify({"error": "The latest message must be from the user."}), 400

    api_messages = [
        {"role": "system", "content": SHARED_SYSTEM},
        {"role": "user", "content": _transcript_user_block(transcript)},
        {"role": "user", "content": CHAT_MODE_USER},
        *tail,
    ]

    def event_stream():
        total = []
        try:
            for piece in iter_chat_stream(api_messages, base_url, model, api_key):
                total.append(piece)
                yield f"event: token\ndata: {json.dumps({'t': piece})}\n\n"
            full = "".join(total).strip()
            if not full:
                err = (
                    "Model returned an empty reply (no streamed text). "
                    "Try another model or check LM Studio server logs."
                )
                yield f"event: error\ndata: {json.dumps({'error': err})}\n\n"
                return
            yield "event: done\ndata: {}\n\n"
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

    return Response(
        stream_with_context(event_stream()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

@app.route("/api/subtitle", methods=["GET"])
def get_subtitle():
    rel = (request.args.get("path") or "").strip()
    try:
        abs_path = _safe_downloaded_subtitle_path(rel)
        if not os.path.exists(abs_path):
            return jsonify({"error": "Subtitle file not found."}), 404
        with open(abs_path, "r", encoding="utf-8") as f:
            txt = f.read()
        return Response(txt, mimetype="text/plain; charset=utf-8")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
