# yt-summarizer (LM Studio)

A local web app that downloads YouTube subtitles and summarizes them using a model
running in [LM Studio](https://lmstudio.ai).

## Requirements

- Python 3.9+
- [LM Studio](https://lmstudio.ai) running locally with a model loaded and the
  local server enabled (default: `http://localhost:1234`)

## Setup

```bash
# 1. (Recommended) Create and activate a virtual environment
python3 -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 2. Install dependencies (includes yt-dlp)
pip install -r requirements.txt

# 3. Start LM Studio, load a model, and enable the local server
#    (LM Studio → Local Server tab → Start Server)

# 4. Run the app
python app.py
```

Then open **http://localhost:5000** in your browser.

## Usage

1. The app will auto-detect models loaded in LM Studio on page load.
   Click **↻** to refresh the model list at any time.
2. Paste a YouTube URL.
3. Set the subtitle language (default: `en`).
4. Click **Summarize**.

## Configuration

| Env var            | Default                      | Description                        |
|--------------------|------------------------------|------------------------------------|
| `LMSTUDIO_BASE_URL`| `http://localhost:1234/v1`   | LM Studio server URL               |

You can also change the server URL directly in the UI.

## Notes

- Subtitles are downloaded to a temp directory and deleted after each request.
- If a video has no human subtitles, auto-generated ones are used as fallback.
- Transcripts longer than 90,000 characters are truncated before being sent.
- No data leaves your machine — everything runs locally.
