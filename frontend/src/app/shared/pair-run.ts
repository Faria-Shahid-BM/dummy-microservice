// Client half of case_store.py's per-pair analyze contract: one case, N pairs,
// each analyzed in its own pass with its own result.
//
// Shared by all four reviewers' case pages — only the result markup differs, so
// only that stays in the per-service templates.

import { applyStageEvent, freshProgress, parseSseError, ReviewProgress } from '../sse.util';

export interface PairRunState {
  progress: ReviewProgress;
  running: boolean;
  /** Stream-level failure for this pair, as opposed to a stored case error. */
  error: string;
  /** Live LLM output for this pair (collateral's observations step streams). */
  streamingText: string;
  /**
   * Finished this session, but the reviewer hasn't moved on yet. Holds the
   * progress panel — with its final per-step token and timing figures — on
   * screen instead of swapping straight to the results table, which otherwise
   * happens the instant the last step lands and is impossible to read.
   * Never set for a pair whose result was loaded from storage.
   */
  awaitingNext: boolean;
}

function freshRun(): PairRunState {
  return {
    progress: freshProgress(),
    running: false,
    error: '',
    streamingText: '',
    awaitingNext: false
  };
}

/**
 * Tracks which pair is being analyzed and how far along it is, driven by the
 * SSE frames of one `POST /cases/{id}/analyze`.
 *
 * Pairs run sequentially server-side, so `current` (set by each `pair_start`)
 * is what untagged frames — the engine's live token chunks — belong to.
 */
export class PairRun {
  /** Per-pair live state, indexed by pair index. */
  runs: PairRunState[] = [];
  /** The tab on screen. */
  active = 0;
  /** A whole-request failure (transport/auth), not one pair's. */
  error = '';

  private current: number | null = null;
  // Set once the user picks a tab mid-run, so the auto-follow stops fighting them.
  private pinned = false;

  /** Called before a run: one fresh slot per pair on the case. */
  start(pairCount: number): void {
    this.runs = Array.from({ length: Math.max(1, pairCount) }, freshRun);
    this.error = '';
    this.current = null;
    this.pinned = false;
  }

  select(index: number): void {
    this.active = index;
    this.pinned = true;
  }

  /** Dismiss the held progress panel and reveal this pair's results. */
  advance(index: number): void {
    const run = this.runs[index];
    if (run) run.awaitingNext = false;
  }

  state(index: number): PairRunState {
    return this.runs[index] ?? freshRun();
  }

  get anyRunning(): boolean {
    return this.runs.some((r) => r.running);
  }

  /**
   * Feed one SSE frame in. `onPairResult` is called with each pair's result as
   * it lands, so the page can fill that tab immediately instead of waiting for
   * the whole run and a reload.
   */
  onFrame(
    eventType: string,
    data: string,
    onPairResult?: (index: number, result: unknown) => void
  ): void {
    if (eventType === 'error') {
      this.error = parseSseError(data);
      return;
    }
    if (eventType === 'content') {
      // Token chunks are bare strings with nowhere to carry a pair tag — they
      // belong to whichever pair is running (pairs are sequential).
      if (this.current !== null && this.runs[this.current]) {
        try {
          this.runs[this.current].streamingText += JSON.parse(data) as string;
        } catch {
          /* malformed chunk — skip it rather than corrupt the buffer */
        }
      }
      return;
    }
    if (eventType !== 'event') return;

    let payload: Record<string, unknown>;
    try {
      payload = JSON.parse(data) as Record<string, unknown>;
    } catch {
      return;
    }
    const stage = payload['stage'];
    const tagged = typeof payload['pair'] === 'number' ? (payload['pair'] as number) : null;

    if (stage === 'pair_start') {
      this.current = tagged;
      if (tagged !== null) {
        this.runs[tagged] = { ...freshRun(), running: true };
        if (!this.pinned) this.active = tagged;
      }
      return;
    }
    const index = tagged ?? this.current;
    if (index === null || !this.runs[index]) return;

    if (stage === 'pair_result') {
      this.runs[index].progress.complete = true;
      this.runs[index].running = false;
      this.runs[index].awaitingNext = true;
      if (onPairResult) onPairResult(index, payload['result']);
      return;
    }
    if (stage === 'pair_error') {
      this.runs[index].error = String(payload['error'] ?? 'review failed');
      this.runs[index].running = false;
      return;
    }
    // Anything else is the engine's own stage event for this pair.
    applyStageEvent(this.runs[index].progress, data);
  }

  /**
   * Show where a run this page did NOT start has got to.
   *
   * The SSE stream belongs to the request that began the run, so a page that
   * reloaded mid-run has no frames — only the snapshot the server kept (see
   * case_store's _RUN_PROGRESS). Replaying that through the same progress
   * object the live path writes means the checklist renders identically
   * whether this page started the run or joined it late.
   */
  adoptServerProgress(pairCount: number, snapshot: Record<string, unknown> | null): void {
    if (!snapshot) return;
    const stage = snapshot['stage'];
    // A finished pair arrives on the case itself; this is only for movement.
    if (stage === 'pair_result' || stage === 'pair_error') return;
    const index = typeof snapshot['pair'] === 'number' ? (snapshot['pair'] as number) : 0;
    // Size once. Re-creating the slots on every poll would throw away the
    // progress just written into them.
    if (this.runs.length < Math.max(pairCount, index + 1)) {
      this.runs = Array.from({ length: Math.max(1, pairCount, index + 1) }, freshRun);
    }
    const run = this.runs[index];
    if (!run) return;
    run.running = true;
    this.current = index;
    if (!this.pinned) this.active = index;
    // `pair_start` says which pair, not which step — onFrame treats it the
    // same way. Applying it would set a stage key no checklist has.
    if (stage !== 'pair_start') applyStageEvent(run.progress, JSON.stringify(snapshot));
  }

  /**
   * Stop showing an adopted run as live, because the case says it is over.
   * Unlike finish(), this invents no error — the outcome is on the case now,
   * and this page was never the one that could have failed.
   */
  stopAdopted(): void {
    for (const run of this.runs) run.running = false;
    this.current = null;
  }

  /** Called when the request settles, so nothing is left spinning. */
  finish(): void {
    for (const run of this.runs) {
      if (!run.running) continue;
      run.running = false;
      if (!run.progress.complete && !run.error) {
        run.error = this.error || 'review did not finish';
      }
    }
    this.current = null;
  }
}
