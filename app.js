const $ = id => document.getElementById(id);

let lastSummaryMarkdown = '';

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function safeHref(url) {
  const u = String(url).trim();
  if (/^https?:\/\//i.test(u)) return escapeHtml(u);
  if (/^mailto:[^\s"'<>]+$/i.test(u)) return escapeHtml(u);
  if (/^#[-.\w]*$/i.test(u)) return escapeHtml(u);
  return '#';
}

/** Inline markdown on an already HTML-escaped string. */
function inlineMarkdown(escaped) {
  let s = escaped;
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, t, h) =>
    '<a href="' + safeHref(h) + '" rel="noopener noreferrer" target="_blank">' + t + '</a>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  return s;
}

function renderBlock(paragraph) {
  const raw = paragraph.trim();
  if (!raw) return '';

  const lines = paragraph.split(/\r?\n/);

  if (/^(-{3,}|\*{3,}|_{3,})$/.test(raw)) return '<hr>';

  const singleLine = lines.length === 1 ? lines[0].trim() : '';
  const hm = singleLine.match(/^(#{1,6})\s+(.+)$/);
  if (hm) {
    const level = hm[1].length;
    return '<h' + level + '>' + inlineMarkdown(escapeHtml(hm[2])) + '</h' + level + '>';
  }

  const nonempty = lines.filter(l => l.trim());
  if (nonempty.length && nonempty.every(l => /^[-*]\s+/.test(l))) {
    const items = nonempty.map(l =>
      '<li>' + inlineMarkdown(escapeHtml(l.replace(/^[-*]\s+/, ''))) + '</li>');
    return '<ul>' + items.join('') + '</ul>';
  }
  if (nonempty.length && nonempty.every(l => /^\d+\.\s+/.test(l))) {
    const items = nonempty.map(l =>
      '<li>' + inlineMarkdown(escapeHtml(l.replace(/^\d+\.\s+/, ''))) + '</li>');
    return '<ol>' + items.join('') + '</ol>';
  }

  if (lines.every(l => !l.trim() || /^>\s?/.test(l))) {
    const inner = nonempty.map(l => l.replace(/^>\s?/, '')).join('\n');
    return '<blockquote>' + renderMarkdownBlocks(inner) + '</blockquote>';
  }

  const body = lines.map(l => inlineMarkdown(escapeHtml(l))).join('<br>');
  return '<p>' + body + '</p>';
}

function renderMarkdownBlocks(text) {
  return text
    .split(/\n{2,}/)
    .map(p => renderBlock(p))
    .filter(Boolean)
    .join('\n');
}

/** Minimal markdown → HTML (no external libs). Fenced code, lists, headers, links, emphasis. */
function markdownToHtml(md) {
  const chunks = [];
  const fenceRe = /^```([\w-]*)\r?\n([\s\S]*?)\r?\n?```/gm;
  let last = 0;
  let m;
  while ((m = fenceRe.exec(md)) !== null) {
    if (m.index > last) chunks.push({ md: md.slice(last, m.index) });
    chunks.push({ html: '<pre><code>' + escapeHtml(m[2]) + '</code></pre>' });
    last = fenceRe.lastIndex;
  }
  if (last < md.length) chunks.push({ md: md.slice(last) });

  return chunks
    .map(part => (part.html ? part.html : renderMarkdownBlocks(part.md)))
    .join('\n');
}

function setSummaryFromMarkdown(markdown) {
  lastSummaryMarkdown = markdown;
  $('summary-output').innerHTML = markdownToHtml(markdown);
}

/** Parse one SSE block (lines ending with blank line already stripped). */
function parseSseBlock(raw) {
  let eventName = 'message';
  const dataLines = [];
  for (const line of raw.split('\n')) {
    if (line.startsWith('event:')) eventName = line.slice(6).trim();
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^\s/, ''));
  }
  if (!dataLines.length) return null;
  const dataStr = dataLines.join('\n').trim();
  try {
    return { event: eventName, data: JSON.parse(dataStr) };
  } catch {
    return { event: eventName, data: { raw: dataStr } };
  }
}

async function readSseStream(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    buf += decoder.decode(value || new Uint8Array(), { stream: !done }).replace(/\r\n/g, '\n');
    let sep;
    while ((sep = buf.indexOf('\n\n')) !== -1) {
      const block = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      const evt = parseSseBlock(block);
      if (evt) onEvent(evt);
    }
    if (done) break;
  }
}

function timestamp() {
  return new Date().toLocaleTimeString('en-US', { hour12: false });
}

function log(msg, type = '') {
  const box = $('log-box');
  const line = document.createElement('div');
  line.className = `log-line ${type}`;
  line.innerHTML = `<span class="ts">[${timestamp()}]</span><span class="msg">${msg}</span>`;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

function setRunning(running) {
  const btn = $('run-btn');
  btn.disabled = running;
  $('btn-icon').innerHTML = running ? '<span class="spinner"></span>' : '▶';
  $('btn-label').textContent = running ? 'RUNNING...' : 'RUN';
}

function showError(msg) {
  $('error-msg').textContent = msg;
  $('error-banner').style.display = 'block';
}

function clearError() {
  $('error-banner').style.display = 'none';
  $('error-msg').textContent = '';
}

async function loadModels() {
  const baseUrl = $('base-url').value.trim();
  const apiKey  = $('api-key').value.trim();
  const sel     = $('model-select');
  sel.innerHTML = '<option value="">Loading…</option>';
  try {
    const params = new URLSearchParams({ base_url: baseUrl });
    if (apiKey) params.append('api_key', apiKey);
    const res  = await fetch(`/api/models?${params}`);
    const data = await res.json();
    if (data.models && data.models.length > 0) {
      sel.innerHTML = data.models.map(m => `<option value="${m}">${m}</option>`).join('');
    } else {
      sel.innerHTML = '<option value="">No models found — is LM Studio running?</option>';
    }
  } catch {
    sel.innerHTML = '<option value="">Could not connect to LM Studio</option>';
  }
}

async function runSummarize() {
  const url     = $('yt-url').value.trim();
  const lang    = $('lang').value.trim() || 'en';
  const baseUrl = $('base-url').value.trim();
  const model   = $('model-select').value;
  const apiKey  = $('api-key').value.trim();

  if (!url)   { showError('Please enter a YouTube URL.'); return; }
  if (!model) { showError('Please select a model (click ↻ to load models from LM Studio).'); return; }

  clearError();
  lastSummaryMarkdown = '';
  $('result-section').style.display = 'none';
  $('summary-output').innerHTML = '';
  $('log-section').style.display = 'block';
  $('log-box').innerHTML = '';
  setRunning(true);

  log('Initializing...', 'info');
  log(`Target URL: ${url}`);
  log(`Subtitle language: ${lang}`);
  log(`Model: ${model}`);
  if (apiKey) log('API key set — will send as Authorization: Bearer');
  log('Calling yt-dlp to fetch subtitles...');

  try {
    const res = await fetch('/api/summarize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, lang, base_url: baseUrl, model, api_key: apiKey || null }),
    });

    const ct = (res.headers.get('content-type') || '').toLowerCase();

    if (ct.includes('application/json')) {
      let data;
      try {
        data = await res.json();
      } catch {
        log('Error: response was not valid JSON', 'err');
        showError('Server returned a non-JSON response. Check the Flask terminal for errors.');
        setRunning(false);
        return;
      }
      if (!res.ok || data.error) {
        log(`Error: ${data.error}`, 'err');
        showError(data.error || 'Request failed');
        setRunning(false);
        return;
      }
      setRunning(false);
      return;
    }

    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try {
        const errBody = await res.json();
        if (errBody.error) msg = errBody.error;
      } catch { /* ignore */ }
      log(`Error: ${msg}`, 'err');
      showError(msg);
      setRunning(false);
      return;
    }

    if (!ct.includes('text/event-stream')) {
      log('Error: expected streamed summary (text/event-stream)', 'err');
      showError('Unexpected response from server.');
      setRunning(false);
      return;
    }

    let summaryMarkdown = '';
    let rafPending = null;
    const flushSummary = () => {
      rafPending = null;
      setSummaryFromMarkdown(summaryMarkdown);
    };
    const scheduleFlush = () => {
      if (rafPending === null) rafPending = requestAnimationFrame(flushSummary);
    };

    let streamError = null;
    await readSseStream(res, evt => {
      if (evt.event === 'ready') {
        log('Subtitles downloaded and parsed.', 'ok');
        const tlen = evt.data.transcript_length;
        if (typeof tlen === 'number') {
          log(`Transcript length: ${tlen.toLocaleString()} chars`);
        }
        log('Streaming summary from model...', 'info');
        $('result-section').style.display = 'block';
        $('result-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
        return;
      }
      if (evt.event === 'token' && evt.data && typeof evt.data.t === 'string') {
        summaryMarkdown += evt.data.t;
        scheduleFlush();
        return;
      }
      if (evt.event === 'done') {
        return;
      }
      if (evt.event === 'error' && evt.data && evt.data.error) {
        streamError = evt.data.error;
      }
    });

    if (rafPending !== null) {
      cancelAnimationFrame(rafPending);
      rafPending = null;
    }
    setSummaryFromMarkdown(summaryMarkdown);

    if (streamError) {
      log(`Error: ${streamError}`, 'err');
      showError(streamError);
      setRunning(false);
      return;
    }

    if (!summaryMarkdown.trim()) {
      const hint =
        'The stream ended without summary text. Pick another model or check LM Studio.';
      log(hint, 'err');
      showError(hint);
      setRunning(false);
      return;
    }

    log('Summary complete.', 'ok');
  } catch (err) {
    log(`Error: ${err.message}`, 'err');
    showError(err.message || String(err));
  }

  setRunning(false);
}

function copySummary() {
  const text = lastSummaryMarkdown || $('summary-output').innerText;
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.querySelector('.copy-btn');
    btn.textContent = '[ copied! ]';
    setTimeout(() => btn.textContent = '[ copy ]', 2000);
  });
}

$('yt-url').addEventListener('keydown', e => { if (e.key === 'Enter') runSummarize(); });
window.addEventListener('load', loadModels);
