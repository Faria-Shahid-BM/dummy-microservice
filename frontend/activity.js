/*
 * activity.js — reusable "behind the scenes" activity feed for the streaming
 * LLM services. Drop-in, no dependencies. It POSTs a request, consumes the
 * Server-Sent Events stream (see the backend streaming.py contract), and
 * renders a live stage checklist plus the model's tokens as they arrive.
 *
 * Usage:
 *   const result = await Activity.run({
 *     url:      KONG + '/api/collateral/review/stream',
 *     formData: fd,                 // multipart body (files, etc.)
 *     token:    jwt,                // optional Bearer token
 *     mount:    someElement,        // where to render the feed
 *     labels:   { extract_fields: 'Reading fields' }  // optional label overrides
 *   })
 *   // -> resolves with the final `result` event payload, or throws on error.
 *
 * The event stream is generic; the default stage labels below match the
 * collateral engine but any service reusing this can override them via `labels`
 * (unknown stages just show their raw key).
 */
(function () {
  const STYLE_ID = 'activity-widget-style'

  const CSS = `
  .act { font-family: 'Segoe UI', system-ui, sans-serif; }
  .act-feed { display: flex; flex-direction: column; gap: 6px; }
  .act-step {
    display: flex; align-items: center; gap: 10px;
    font-size: 13px; color: #94a3b8;
    padding: 6px 0;
  }
  .act-step.done { color: #cbd5e1; }
  .act-step .act-ic {
    width: 16px; height: 16px; flex: 0 0 16px;
    display: flex; align-items: center; justify-content: center;
    font-size: 13px; font-weight: 700; color: #22c55e;
  }
  .act-step.active .act-txt { color: #e2e8f0; }
  .act-detail { margin-left: auto; font-size: 11px; color: #475569; font-family: monospace; }
  .act-spin {
    width: 12px; height: 12px;
    border: 2px solid #334155; border-top-color: #6366f1;
    border-radius: 50%; animation: act-spin .6s linear infinite;
  }
  @keyframes act-spin { to { transform: rotate(360deg); } }

  .act-out-wrap { margin-top: 14px; }
  .act-out-label {
    font-size: 11px; font-weight: 600; letter-spacing: .06em;
    text-transform: uppercase; color: #475569; margin-bottom: 6px;
  }
  .act-out {
    background: #0f1420; border: 1px solid #1e2535; border-radius: 6px;
    padding: 12px; max-height: 220px; overflow: auto;
    font-family: monospace; font-size: 12px; line-height: 1.6;
    color: #a3e635; white-space: pre-wrap; word-break: break-word;
  }
  .act-out::-webkit-scrollbar { width: 5px; }
  .act-out::-webkit-scrollbar-thumb { background: #2d3748; border-radius: 3px; }
  `

  // Default human labels for the collateral engine's stage keys.
  const DEFAULT_LABELS = {
    extract_text:   'Extracting text',
    extract_fields: 'Extracting fields from both documents',
    compare:        'Comparing fields',
    observations:   'Generating observations',
    done:           'Done',
  }

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return
    const s = document.createElement('style')
    s.id = STYLE_ID
    s.textContent = CSS
    document.head.appendChild(s)
  }

  function stageLabel(ev, labels) {
    const key = ev.stage
    let text = labels[key] || DEFAULT_LABELS[key] || key
    if (key === 'extract_text' && ev.document) text += ' — ' + String(ev.document).replace(/_/g, ' ')
    if (key === 'compare' && ev.fields != null) text += ' (' + ev.fields + ' fields)'
    return text
  }

  // Parse one raw SSE frame ("event: x\ndata: y") into {event, data}.
  function parseFrame(frame) {
    let event = 'message', data = ''
    for (const line of frame.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) data += line.slice(5).replace(/^ /, '')
    }
    return { event, data }
  }

  async function run({ url, formData, token, mount, labels }) {
    ensureStyle()
    labels = labels || {}
    mount.innerHTML = ''

    const root = document.createElement('div'); root.className = 'act'
    const feed = document.createElement('div'); feed.className = 'act-feed'
    const outWrap = document.createElement('div'); outWrap.className = 'act-out-wrap'; outWrap.style.display = 'none'
    outWrap.innerHTML = '<div class="act-out-label">LLM output — live</div><div class="act-out"></div>'
    root.appendChild(feed); root.appendChild(outWrap)
    mount.appendChild(root)
    const out = outWrap.querySelector('.act-out')

    const steps = new Map()   // key -> { el, txt, detail, done }

    function completeActive() {
      for (const o of steps.values()) {
        if (o.el.classList.contains('active')) {
          o.el.classList.remove('active'); o.el.classList.add('done')
          o.el.querySelector('.act-ic').innerHTML = '✓'
        }
      }
    }
    function activeStep() {
      for (const o of steps.values()) if (o.el.classList.contains('active')) return o
      return null
    }
    function ensureStep(key) {
      if (steps.has(key)) return steps.get(key)
      const el = document.createElement('div'); el.className = 'act-step active'
      el.innerHTML = '<span class="act-ic"><span class="act-spin"></span></span>' +
                     '<span class="act-txt"></span><span class="act-detail"></span>'
      feed.appendChild(el)
      const o = { el, txt: el.querySelector('.act-txt'), detail: el.querySelector('.act-detail'), done: 0 }
      steps.set(key, o)
      return o
    }

    function onEvent(obj) {
      if (obj.stage) {
        if (obj.stage === 'done') { completeActive(); return }
        completeActive()   // previous stage finished when the next starts
        const key = obj.stage === 'extract_text' ? 'extract_text:' + (obj.document || '') : obj.stage
        ensureStep(key).txt.textContent = stageLabel(obj, labels)
      } else if (obj.event) {
        // page-level OCR progress attaches to the current (text-extraction) step
        const s = activeStep()
        if (!s) return
        if (obj.event === 'init') s.detail.textContent = '0/' + obj.total + ' pages'
        else if (obj.event === 'page_complete' || obj.event === 'page_failed') {
          s.done += 1; s.detail.textContent = s.done + '/' + obj.total + ' pages'
        } else if (obj.event === 'page_start' && !s.detail.textContent) {
          s.detail.textContent = '0/' + obj.total + ' pages'
        }
      }
    }

    function onContent(chunk) {
      outWrap.style.display = 'block'
      out.textContent += chunk
      out.scrollTop = out.scrollHeight
    }

    const res = await fetch(url, {
      method: 'POST',
      headers: token ? { Authorization: 'Bearer ' + token } : {},
      body: formData,
    })

    // Non-streaming failure (401/403/504/…): body may be plain JSON, not SSE.
    if (!res.ok || !res.body) {
      let data
      try { data = await res.json() } catch { data = { error: 'Request failed (' + res.status + ')' } }
      const err = new Error(data.detail || data.error || ('HTTP ' + res.status))
      err.status = res.status; err.data = data
      throw err
    }

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let result = null, errorPayload = null

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let idx
      while ((idx = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)
        const { event, data } = parseFrame(frame)
        if (!data) continue
        let payload
        try { payload = JSON.parse(data) } catch { continue }
        if (event === 'content' || event === 'reasoning') onContent(payload)
        else if (event === 'event') onEvent(payload)
        else if (event === 'result') result = payload
        else if (event === 'error') errorPayload = payload
        // 'open' is just a connect ack — ignore
      }
    }

    completeActive()
    if (errorPayload) {
      const err = new Error(errorPayload.error || 'Pipeline error')
      err.data = errorPayload
      throw err
    }
    return result
  }

  window.Activity = { run }
})()
