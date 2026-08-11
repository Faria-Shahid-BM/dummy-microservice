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
}

function freshRun(): PairRunState {
  return { progress: freshProgress(), running: false, error: '', streamingText: '' };
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
