// Client half of batch.py's contract: N independent review items submitted in
// one request, each with its own progress, live output and result.
//
// Used by all three streaming reviewers (collateral: legal+property per item;
// valuation: report; insurance: policy). Only the slot names, the endpoint and
// the result shape differ — everything below is shared, and rendered by
// ReviewBatchComponent.

export interface ReviewProgress {
  stageKey: string | null;
  detail: string | null;
  complete: boolean;
}

export function freshProgress(): ReviewProgress {
  return { stageKey: null, detail: null, complete: false };
}

// One unit of review the user has staged: one file per upload slot.
export interface BatchItem<TResult> {
  id: string;
  files: Record<string, File | null>;
  result: TResult | null;
  error: string;
  running: boolean;
  progress: ReviewProgress;
  streamingText: string;
}

// Posts a body and calls back per SSE frame — DashboardComponent's consumeSse.
export type SseConsumer = (
  url: string,
  body: FormData,
  onFrame: (eventType: string, data: string) => void
) => Promise<void>;

let itemSeq = 0;

export function parseSseError(rawData: string): string {
  try {
    return (JSON.parse(rawData) as { error: string }).error;
  } catch {
    return 'request failed';
  }
}

function parseJson<T>(raw: string): T | null {
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

// Applies one "event" SSE frame's JSON payload (`{"stage": "...", ...}`, see
// engines/*.py's _emit_event) to an item's progress state. "done" isn't a
// checklist step (see the *_STAGES lists) — it just means the pipeline is
// finishing up, ahead of that item's result.
export function applyStageEvent(progress: ReviewProgress, payload: Record<string, unknown>): void {
  const stage = payload['stage'];
  if (typeof stage !== 'string') return;

  if (stage === 'done') {
    progress.complete = true;
    return;
  }
  progress.stageKey = stage;
  // item/item_id are batch routing metadata, not something to show in the line.
  const noise = ['stage', 'status', 'item', 'item_id'];
  const rest = Object.entries(payload).filter(([k]) => !noise.includes(k));
  progress.detail = rest.length ? rest.map(([k, v]) => `${k}: ${v}`).join(', ') : null;
}

export class ReviewBatch<TResult> {
  items: BatchItem<TResult>[] = [];
  /** Index of the result tab on screen. */
  active = 0;
  /** A batch request is in flight. */
  submitting = false;
  /** Batch-level failure (transport / auth), as opposed to a per-item error. */
  error = '';

  // Set once the user picks a tab during a batch, so the auto-follow (which
  // otherwise jumps to whichever item just started) stops fighting them.
  private pinned = false;

  constructor(
    /** Upload slot names, in the order the service pairs them. */
    readonly slots: string[],
    private readonly url: string,
    private readonly sse: SseConsumer
  ) {
    this.reset();
  }

  reset(): void {
    this.items = [this.blank()];
    this.active = 0;
    this.pinned = false;
    this.error = '';
  }

  private blank(): BatchItem<TResult> {
    const files: Record<string, File | null> = {};
    for (const slot of this.slots) files[slot] = null;
    return {
      id: `item-${++itemSeq}`,
      files,
      result: null,
      error: '',
      running: false,
      progress: freshProgress(),
      streamingText: ''
    };
  }

  add(): void {
    this.items.push(this.blank());
    this.active = this.items.length - 1;
    this.pinned = true;
  }

  remove(index: number): void {
    const item = this.items[index];
    if (!item || item.running || this.items.length === 1) return;
    this.items.splice(index, 1);
    this.active = Math.min(this.active, this.items.length - 1);
  }

  select(index: number): void {
    this.active = index;
    this.pinned = true;
  }

  setFile(item: BatchItem<TResult>, slot: string, event: Event): void {
    item.files[slot] = (event.target as HTMLInputElement).files?.[0] ?? null;
    this.invalidate(item);
  }

  /** Every slot filled — the item is submittable. */
  staged(item: BatchItem<TResult>): boolean {
    return this.slots.every((slot) => !!item.files[slot]);
  }

  /** Items with something to show in a tab. */
  get withOutcome(): BatchItem<TResult>[] {
    return this.items.filter((i) => i.running || i.result || i.error);
  }

  /**
   * Fully staged but not yet reviewed — what submit() sends. Already-reviewed
   * items are deliberately excluded so their results survive.
   */
  get pending(): BatchItem<TResult>[] {
    return this.items.filter((i) => this.staged(i) && !i.result && !i.error && !i.running);
  }

  /** Swapping a file makes the result on screen stale, so drop it — that also
   * puts the item back in `pending` so the next submit re-runs it. */
  private invalidate(item: BatchItem<TResult>): void {
    if (item.running) return;
    item.result = null;
    item.error = '';
    item.progress = freshProgress();
    item.streamingText = '';
  }

  /** Clears a finished item's outcome and re-runs just that item (a failed item
   * is otherwise excluded from `pending`, so submit() would skip it). */
  retry(item: BatchItem<TResult>): void {
    if (this.submitting || !this.staged(item)) return;
    this.invalidate(item);
    this.submit();
  }

  submit(): void {
    const pending = this.pending;
    if (!pending.length || this.submitting) return;

    this.error = '';
    this.submitting = true;
    this.pinned = false;

    const form = new FormData();
    for (const item of pending) {
      item.running = true;
      item.error = '';
      item.progress = freshProgress();
      item.streamingText = '';
      // Order matters: the service pairs slot lists by position, and echoes
      // item_ids[i] back on every event so we can route frames to this row.
      for (const slot of this.slots) form.append(slot, item.files[slot]!);
      form.append('item_ids', item.id);
    }

    // Items run sequentially server-side; `current` tracks whichever item the
    // latest item_start announced, since token chunks aren't item-tagged.
    let current: BatchItem<TResult> | null = null;

    this.sse(this.url, form, (eventType, data) => {
      if (eventType === 'event') {
        const payload = parseJson<Record<string, unknown>>(data);
        if (!payload) return;
        const item = this.find(payload['item_id']) ?? current;

        if (payload['stage'] === 'item_start') {
          current = item;
          if (item && !this.pinned) this.active = this.items.indexOf(item);
          return;
        }
        if (!item) return;

        if (payload['stage'] === 'item_result') {
          item.result = payload['result'] as TResult;
          item.progress.complete = true;
          item.running = false;
          return;
        }
        if (payload['stage'] === 'item_error') {
          item.error = String(payload['error'] ?? 'review failed');
          item.running = false;
          return;
        }
        applyStageEvent(item.progress, payload);
      } else if (eventType === 'content') {
        // Live LLM output (collateral's observations step is the only pipeline
        // with real token streaming; the others emit stage events only).
        const chunk = parseJson<string>(data);
        if (chunk && current) current.streamingText += chunk;
      } else if (eventType === 'result') {
        // Batch summary, repeating every item — a backstop in case an
        // item_result frame was missed.
        const batch = parseJson<{ results: { item_id: string; result?: TResult; error?: string }[] }>(data);
        for (const entry of batch?.results ?? []) {
          const item = this.find(entry.item_id);
          if (!item || item.result || item.error) continue;
          if (entry.result) {
            item.result = entry.result;
            item.progress.complete = true;
          } else {
            item.error = entry.error ?? 'review failed';
          }
          item.running = false;
        }
      } else if (eventType === 'error') {
        this.error = parseSseError(data);
      }
    })
      .catch((err) => {
        this.error = err instanceof Error ? err.message : 'review failed';
      })
      .finally(() => {
        this.submitting = false;
        // Anything still marked running never got a terminal frame (stream cut
        // short, or a batch-level error) — don't leave it spinning forever.
        for (const item of pending) {
          if (!item.running) continue;
          item.running = false;
          if (!item.result && !item.error) {
            item.error = this.error || 'review did not finish';
          }
        }
      });
  }

  private find(id: unknown): BatchItem<TResult> | null {
    if (typeof id !== 'string') return null;
    return this.items.find((i) => i.id === id) ?? null;
  }
}
