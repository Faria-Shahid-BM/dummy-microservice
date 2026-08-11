import { CommonModule } from '@angular/common';
import { Component, Inject, OnInit } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { BatchItem, CASE_SERVICE, CaseService, CaseSummary } from './case.service';

let batchRowSeq = 0;

// Shared by every stateless review service (collateral/valuation/insurance/
// docdiff) — the list page is identical across all of them, so this is the
// one copy. Bound to a concrete CaseService via the route's `providers`
// (see app.routes.ts); it never imports a specific service itself.
@Component({
  selector: 'app-case-list',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink],
  templateUrl: './case-list.component.html'
})
export class CaseListComponent implements OnInit {
  cases: CaseSummary[] = [];
  loading = false;
  error = '';

  showAddCase = false;
  newCaseName = '';
  creating = false;
  createError = '';

  // Batch: stage several items, each becoming its own persisted case, and
  // review them all in one submission (see case_store.py's POST /cases/batch).
  // The tab strip below tracks them while they run; the results themselves live
  // on the cases, so they survive a reload and stay in the list above.
  showBatch = false;
  batchItems: BatchItem[] = [];
  batchActive = 0;
  batchRunning = false;
  batchError = '';
  // Set once the user picks a tab mid-run, so the auto-follow stops fighting them.
  private batchPinned = false;

  constructor(@Inject(CASE_SERVICE) public caseService: CaseService, private router: Router) {}

  ngOnInit(): void {
    this.load();
  }

  // --- batch review ---

  toggleBatch(): void {
    this.showBatch = !this.showBatch;
    this.batchError = '';
    if (this.showBatch && !this.batchItems.length) this.addBatchItem();
  }

  private blankItem(): BatchItem {
    const files: Record<string, File | null> = {};
    for (const slot of this.caseService.slots) files[slot.key] = null;
    return {
      key: `row-${++batchRowSeq}`,
      files,
      caseId: null,
      name: '',
      running: false,
      error: '',
      done: false
    };
  }

  addBatchItem(): void {
    this.batchItems.push(this.blankItem());
    this.batchActive = this.batchItems.length - 1;
    this.batchPinned = true;
  }

  removeBatchItem(index: number): void {
    const item = this.batchItems[index];
    if (!item || item.running) return;
    this.batchItems.splice(index, 1);
    if (!this.batchItems.length) this.addBatchItem();
    this.batchActive = Math.min(this.batchActive, this.batchItems.length - 1);
  }

  selectBatchItem(index: number): void {
    this.batchActive = index;
    this.batchPinned = true;
  }

  setBatchFile(item: BatchItem, slot: string, event: Event): void {
    if (item.running) return;
    item.files[slot] = (event.target as HTMLInputElement).files?.[0] ?? null;
    // A changed file makes any earlier outcome stale; the case it already
    // produced stays in the list, this row just goes back to unsubmitted.
    item.caseId = null;
    item.error = '';
    item.done = false;
    // Default the case name to the first slot's file name, unless it was typed.
    if (!item.nameEdited) {
      const first = this.caseService.slots[0]?.key;
      const f = first ? item.files[first] : null;
      item.name = f ? f.name.replace(/\.[^.]+$/, '') : '';
    }
  }

  /** Fully staged and not yet submitted — what Review sends. */
  get pendingBatchItems(): BatchItem[] {
    return this.batchItems.filter(
      (i) => this.caseService.slots.every((s) => !!i.files[s.key]) && !i.done && !i.error && !i.running
    );
  }

  get batchItemsWithOutcome(): BatchItem[] {
    return this.batchItems.filter((i) => i.running || i.done || i.error);
  }

  batchSubmitLabel(): string {
    if (this.batchRunning) return 'Reviewing…';
    const n = this.pendingBatchItems.length;
    const noun = this.caseService.itemNoun;
    return n > 1 ? `Review ${n} ${noun === 'policy' ? 'policies' : noun + 's'}` : 'Review';
  }

  runBatch(): void {
    const pending = this.pendingBatchItems;
    if (!pending.length || this.batchRunning) return;

    this.batchError = '';
    this.batchRunning = true;
    this.batchPinned = false;
    for (const item of pending) {
      item.running = true;
      item.error = '';
      item.done = false;
      if (!item.name.trim()) {
        const first = this.caseService.slots[0]?.key;
        const f = first ? item.files[first] : null;
        item.name = (f ? f.name.replace(/\.[^.]+$/, '') : '') || `Case ${this.batchItems.indexOf(item) + 1}`;
      }
    }

    // Cases run sequentially server-side; `current` tracks whichever case the
    // latest case_start announced, since token chunks aren't case-tagged.
    let current: BatchItem | null = null;
    let index = 0;

    this.caseService
      .analyzeBatch(pending, (eventType, data) => {
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
            // case_start is the i-th pending row — that's how a row learns the
            // case id it produced.
            current = pending[typeof payload['case'] === 'number' ? (payload['case'] as number) : index++] ?? null;
            if (current) {
              current.caseId = String(payload['case_id'] ?? '') || null;
              if (!this.batchPinned) this.batchActive = this.batchItems.indexOf(current);
            }
            return;
          }
          if (!current) return;
          if (stage === 'case_result') {
            current.done = true;
            current.running = false;
            return;
          }
          if (stage === 'case_error') {
            current.error = String(payload['error'] ?? 'review failed');
            current.running = false;
          }
        } else if (eventType === 'error') {
          this.batchError = data;
        }
      })
      .catch((err) => {
        this.batchError = err instanceof Error ? err.message : 'review failed';
      })
      .finally(() => {
        this.batchRunning = false;
        for (const item of pending) {
          if (!item.running) continue;
          item.running = false;
          if (!item.done && !item.error) item.error = this.batchError || 'review did not finish';
        }
        this.load(); // the new cases (and their results) are now in the list
      });
  }

  openBatchCase(item: BatchItem): void {
    if (!item.caseId) return;
    this.router.navigate([this.caseService.routeBase, 'cases', item.caseId]);
  }

  load(): void {
    this.error = '';
    this.loading = true;
    this.caseService.listCases().subscribe({
      next: (res) => {
        this.cases = res.cases;
        this.loading = false;
      },
      error: (err: HttpErrorResponse) => {
        this.error = err.error?.detail ?? 'failed to load cases';
        this.loading = false;
      }
    });
  }

  toggleAddCase(): void {
    this.showAddCase = !this.showAddCase;
    this.createError = '';
  }

  createCase(): void {
    if (!this.newCaseName.trim()) return;
    this.createError = '';
    this.creating = true;
    this.caseService.createCase(this.newCaseName.trim()).subscribe({
      next: (created) => {
        this.creating = false;
        this.newCaseName = '';
        this.showAddCase = false;
        this.router.navigate([this.caseService.routeBase, 'cases', created.id]);
      },
      error: (err: HttpErrorResponse) => {
        this.createError = err.error?.detail ?? 'failed to create case';
        this.creating = false;
      }
    });
  }
}
