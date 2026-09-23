// Shared SSE client plumbing for the LLM review pipelines (collateral,
// valuation, insurance) — see streaming.py for the server-side event
// contract these read. Split out of dashboard.component.ts so services
// migrated to a routed Cases UI (see collateral.service.ts) don't have to
// duplicate this parsing logic.

export interface TokenUsage {
  prompt: number;
  completion: number;
  total: number;
}

export interface ReviewProgress {
  stageKey: string | null;
  detail: string | null;
  complete: boolean;
  // What each finished step cost, keyed by step name, plus the running total.
  // A step that makes no LLM call still reports a zero, so a missing key means
  // "not reached yet" rather than "free".
  usageByStep: Record<string, TokenUsage>;
  usageTotal: TokenUsage;
  /** Wall-clock per step, in milliseconds, keyed the same way. */
  msByStep: Record<string, number>;
}

function zeroUsage(): TokenUsage {
  return { prompt: 0, completion: 0, total: 0 };
}

export function freshProgress(): ReviewProgress {
  return {
    stageKey: null,
    detail: null,
    complete: false,
    usageByStep: {},
    usageTotal: zeroUsage(),
    msByStep: {}
  };
}

// Posts `body` (JSON object or FormData, for the file-upload endpoints) and
// reads the response as an SSE stream per streaming.py's contract, calling
// `onFrame(eventType, data)` for each complete "event: X\ndata: Y\n\n"
// frame. Angular's HttpClient has no ergonomic incremental-read API, so this
// uses the Fetch API directly (still zone.js-patched, so change detection
// still runs after each awaited chunk).
export async function consumeSse(
  url: string,
  body: FormData | Record<string, unknown> | null,
  onFrame: (eventType: string, data: string) => void
): Promise<void> {
  const isFormData = body instanceof FormData;
  const response = await fetch(url, {
    method: 'POST',
    headers: isFormData || body === null ? {} : { 'Content-Type': 'application/json' },
    body: isFormData || body === null ? body : JSON.stringify(body)
  });

  if (!response.ok) {
    let detail = `request failed (${response.status})`;
    try {
      const errBody = await response.json();
      if (errBody?.detail) detail = errBody.detail;
    } catch {
      /* error body wasn't JSON — keep the generic status message */
    }
    throw new Error(detail);
  }
  if (!response.body) {
    throw new Error('empty response body');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let frameEnd: number;
    while ((frameEnd = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, frameEnd);
      buffer = buffer.slice(frameEnd + 2);

      let eventType = 'message';
      let data = '';
      for (const line of frame.split('\n')) {
        if (line.startsWith('event:')) eventType = line.slice(6).trim();
        else if (line.startsWith('data:')) data += line.slice(5).trim();
      }
      if (data) onFrame(eventType, data);
    }
  }
}

// Applies one "event" SSE frame's JSON payload (`{"stage": "...", ...}`, see
// engines/*.py's _emit_event) to a review pipeline's progress state. "done"
// isn't a checklist step — it just means the pipeline is finishing up,
// ahead of the final `result` frame.
export function applyStageEvent(progress: ReviewProgress, rawData: string): void {
  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(rawData) as Record<string, unknown>;
  } catch {
    return;
  }
  const stage = payload['stage'];
  if (typeof stage !== 'string') return;

  if (stage === 'done') {
    progress.complete = true;
    return;
  }
  // Cost reports ride the same channel but are not pipeline steps — recording
  // one must never advance the checklist to a stage called "usage".
  if (stage === 'usage') {
    const step = payload['step'];
    const spent = payload['usage'] as TokenUsage | undefined;
    if (typeof step === 'string' && spent) progress.usageByStep[step] = spent;
    const ms = payload['ms'];
    if (typeof step === 'string' && typeof ms === 'number') {
      progress.msByStep[step] = ms;
    }
    const cumulative = payload['cumulative'] as TokenUsage | undefined;
    if (cumulative) progress.usageTotal = cumulative;
    return;
  }
  progress.stageKey = stage;
  // `pair` tags which tab the frame belongs to (added by case_store's
  // _scoped_emit) — the tab strip already says that, so it isn't detail.
  const rest = Object.entries(payload).filter(
    ([k]) => k !== 'stage' && k !== 'status' && k !== 'pair'
  );
  progress.detail = rest.length ? rest.map(([k, v]) => `${k}: ${v}`).join(', ') : null;
}

export function parseSseError(rawData: string): string {
  try {
    return (JSON.parse(rawData) as { error: string }).error;
  } catch {
    return 'request failed';
  }
}
