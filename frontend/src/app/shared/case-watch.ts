// Keeps a case page honest about a review that is running somewhere other than
// this page's own stream.
//
// case_store.py's analyze keeps going after the browser that started it leaves
// (sse_stream awaits its worker regardless), persisting each pair as it lands
// and holding the case at `analyzing` until the last one. So coming back to a
// case mid-run finds status `analyzing` with no stream to follow: this polls
// the case until the server says it's done, and the page shows each pair's
// result as it arrives.
//
// Shared by all four reviewers' case pages, like PairRun.

import { CaseService } from './case.service';
import { PairRun } from './pair-run';

const POLL_MS = 3000;

export class CaseWatch {
  private timer: ReturnType<typeof setTimeout> | null = null;

  /**
   * Call after every case load. Schedules one more load while the server is
   * still analyzing — unless this page started that run itself (`ownRun`), in
   * which case its own stream is already delivering everything and polling
   * would only fight it. Each load re-arms (or ends) the poll.
   */
  sync(status: string | undefined, ownRun: boolean, reload: () => void): void {
    this.stop();
    if (status === 'analyzing' && !ownRun) {
      this.timer = setTimeout(reload, POLL_MS);
    }
  }

  /**
   * Pull the stage that run has reached and show it on `run`, so a reloaded
   * page gets the same checklist as the page that started it instead of a
   * bare "in progress". Best-effort: if this fails, the poll above still
   * delivers each pair's result as it lands, which is the part that matters.
   */
  followProgress<T>(
    service: CaseService<T>,
    caseId: string,
    run: PairRun,
    pairCount: number
  ): void {
    service.progress(caseId).subscribe({
      next: (reply) => {
        if (reply.status === 'analyzing') run.adoptServerProgress(pairCount, reply.progress);
        else run.stopAdopted();
      },
      error: () => undefined
    });
  }

  stop(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }
}
