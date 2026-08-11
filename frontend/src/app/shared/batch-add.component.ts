import { CommonModule } from '@angular/common';
import { Component, EventEmitter, Input, Output } from '@angular/core';
import { Router } from '@angular/router';
import { BatchItem, CaseService } from './case.service';

let batchRowSeq = 0;

// The "+ Add another" half of a case's upload block: extra documents staged
// right beside this case's own uploads, each becoming its own persisted case
// when the case is reviewed (see case_store.py's POST /cases/batch).
//
// Sits inside a case detail page rather than being a separate mode, because
// that's where you already are when you notice there's a stack to get through.
// Dropped in by all four reviewers (collateral/valuation/insurance/docdiff) —
// the upload slots come from the service, so this is the one implementation.
//
// The parent owns the Review button; it calls run() after its own case's
// analysis finishes. Progress for the extra items shows in the tab strip here,
// and each finished tab links to the case it produced — the results themselves
// live on those cases, so they survive a reload.
@Component({
  selector: 'app-batch-add',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './batch-add.component.html'
})
export class BatchAddComponent {
  @Input({ required: true }) service!: CaseService;
  /** True while the parent case is analyzing — freezes staging. */
  @Input() disabled = false;
  /** Fires once a batch run finishes, so the parent can refresh if it wants. */
  @Output() finished = new EventEmitter<void>();

  items: BatchItem[] = [];
  active = 0;
  running = false;
  error = '';

  // Set once the user picks a tab mid-run, so the auto-follow stops fighting them.
  private pinned = false;

  constructor(private router: Router) {}

  get noun(): string {
    return this.service.itemNoun;
  }

  get plural(): string {
    return this.noun === 'policy' ? 'policies' : `${this.noun}s`;
  }

  /** Fully staged and not yet submitted — what run() sends. */
  get pending(): BatchItem[] {
    return this.items.filter(
      (i) => this.service.slots.every((s) => !!i.files[s.key]) && !i.done && !i.error && !i.running
    );
  }

  get hasPending(): boolean {
    return this.pending.length > 0;
  }

  get withOutcome(): BatchItem[] {
    return this.items.filter((i) => i.running || i.done || i.error);
  }

  add(): void {
    const files: Record<string, File | null> = {};
    for (const slot of this.service.slots) files[slot.key] = null;
    this.items.push({
      key: `row-${++batchRowSeq}`,
      files,
      caseId: null,
      name: '',
      running: false,
      error: '',
      done: false
    });
    this.active = this.items.length - 1;
    this.pinned = true;
  }

  remove(index: number): void {
    const item = this.items[index];
    if (!item || item.running) return;
    this.items.splice(index, 1);
    this.active = Math.max(0, Math.min(this.active, this.items.length - 1));
  }

  select(index: number): void {
    this.active = index;
    this.pinned = true;
  }

  setFile(item: BatchItem, slot: string, event: Event): void {
    if (item.running) return;
    item.files[slot] = (event.target as HTMLInputElement).files?.[0] ?? null;
    // A changed file makes any earlier outcome stale. The case it already
    // produced stays where it is; this row just goes back to unsubmitted.
    item.caseId = null;
    item.error = '';
    item.done = false;
    item.name = this.defaultName(item);
  }

  private defaultName(item: BatchItem): string {
    const first = this.service.slots[0]?.key;
    const file = first ? item.files[first] : null;
    return file ? file.name.replace(/\.[^.]+$/, '') : '';
  }

  /**
   * Create and review every staged item, each as its own case. Resolves when
   * the whole batch has finished (the parent awaits this after its own case).
   */
  async run(): Promise<void> {
    const pending = this.pending;
    if (!pending.length || this.running) return;

    this.error = '';
    this.running = true;
    this.pinned = false;
    for (const item of pending) {
      item.running = true;
      item.error = '';
      item.done = false;
      if (!item.name.trim()) {
        item.name = this.defaultName(item) || `${this.noun} ${this.items.indexOf(item) + 1}`;
      }
    }

    // Cases run sequentially server-side; `current` tracks whichever case the
    // latest case_start announced, since token chunks aren't case-tagged.
    let current: BatchItem | null = null;

    try {
      await this.service.analyzeBatch(pending, (eventType, data) => {
        if (eventType === 'event') {
          let payload: Record<string, unknown>;
          try {
            payload = JSON.parse(data) as Record<string, unknown>;
          } catch {
            return;
          }
          const stage = payload['stage'];
          if (stage === 'case_start') {
            // The server reports items in submission order, so the i-th
            // case_start is the i-th row sent — that's how a row learns the id
            // of the case it produced.
            const idx = typeof payload['case'] === 'number' ? (payload['case'] as number) : 0;
            current = pending[idx] ?? null;
            if (current) {
              current.caseId = String(payload['case_id'] ?? '') || null;
              if (!this.pinned) this.active = this.items.indexOf(current);
            }
            return;
          }
          if (!current) return;
          if (stage === 'case_result') {
            current.done = true;
            current.running = false;
          } else if (stage === 'case_error') {
            current.error = String(payload['error'] ?? 'review failed');
            current.running = false;
          }
        } else if (eventType === 'error') {
          try {
            this.error = (JSON.parse(data) as { error: string }).error;
          } catch {
            this.error = 'review failed';
          }
        }
      });
    } catch (err) {
      this.error = err instanceof Error ? err.message : 'review failed';
    } finally {
      this.running = false;
      // Anything still marked running never got a terminal frame (stream cut
      // short, or a batch-level error) — don't leave it spinning forever.
      for (const item of pending) {
        if (!item.running) continue;
        item.running = false;
        if (!item.done && !item.error) item.error = this.error || 'review did not finish';
      }
      this.finished.emit();
    }
  }

  open(item: BatchItem): void {
    if (!item.caseId) return;
    this.router.navigate([this.service.routeBase, 'cases', item.caseId]);
  }
}
