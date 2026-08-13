import { CommonModule } from '@angular/common';
import { Component, EventEmitter, Input, Output, TemplateRef } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { StageDef, StageProgressComponent } from '../stage-progress/stage-progress.component';
import { CaseDetail, CasePair, CaseService } from './case.service';
import { DocPreviewComponent } from './doc-preview.component';
import { PairRun } from './pair-run';

// The pairs of one case: the extra-pair upload blocks that sit beside the case's
// own uploads, and the result tabs — one tab per pair, each holding that pair's
// own result (see case_store.py's per-pair analyze).
//
// Shared by all four reviewers; the result markup is the only per-service part,
// passed in as a template. Two placements in the page, hence the `part` input:
//   <app-case-pairs part="uploads" …>   beside the case's own upload fields
//   <app-case-pairs part="results" …>   below the Review button
@Component({
  selector: 'app-case-pairs',
  standalone: true,
  imports: [CommonModule, StageProgressComponent, DocPreviewComponent],
  templateUrl: './case-pairs.component.html'
})
export class CasePairsComponent<TResult> {
  @Input({ required: true }) part!: 'uploads' | 'results';
  @Input({ required: true }) service!: CaseService<TResult>;
  @Input({ required: true }) case!: CaseDetail<TResult>;
  @Input({ required: true }) run!: PairRun;
  /** Stage checklist for this service's pipeline. */
  @Input() stages: StageDef[] = [];
  /** Rendered per finished pair, with that pair's result as $implicit. */
  @Input() resultTemplate?: TemplateRef<{ $implicit: TResult }>;
  /** True while an analysis is in flight — freezes staging. */
  @Input() analyzing = false;

  /** The case reloaded after an upload/add/remove, so the parent can adopt it. */
  @Output() caseChanged = new EventEmitter<CaseDetail<TResult>>();
  /** Re-run just this pair (its tab's button) — the parent owns analysis. */
  @Output() reanalyzePair = new EventEmitter<number>();

  busy = false;
  error = '';

  get noun(): string {
    return this.service.itemNoun;
  }

  /** Pairs beyond the case's own uploads — the ones this component stages. */
  get extras(): CasePair<TResult>[] {
    return (this.case?.pairs ?? []).filter((p) => p.index > 0);
  }

  get pairs(): CasePair<TResult>[] {
    return this.case?.pairs ?? [];
  }

  label(pair: CasePair<TResult>): string {
    return pair.index === 0 ? `${this.noun} 1` : `${this.noun} ${pair.index + 1}`;
  }

  /** A pair is ready when every required slot has a file. */
  ready(pair: CasePair<TResult>): boolean {
    return this.service.slots.every((s) => !!pair.uploads?.[s.key]);
  }

  /** Reviewed already — a stored error counts as not reviewed, so it retries. */
  reviewed(pair: CasePair<TResult>): boolean {
    return pair.result != null;
  }

  // Pairs the user has explicitly unlocked to swap a document into, keyed by
  // index. Cleared on upload, since the server drops that pair's result and the
  // pair is unreviewed again anyway.
  private unlocked = new Set<number>();

  /** Can this pair's files still be chosen? Reviewed pairs are locked, and a
   * managed slot (populated from an external source, e.g. a generated
   * document) is locked always — unlocking a reviewed pair doesn't apply to it. */
  editable(pair: CasePair<TResult>, slotKey?: string): boolean {
    if (slotKey && this.service.managedSlots.includes(slotKey)) return false;
    return !this.reviewed(pair) || this.unlocked.has(pair.index);
  }

  unlock(pair: CasePair<TResult>): void {
    this.unlocked.add(pair.index);
  }

  addPair(): void {
    this.error = '';
    this.busy = true;
    this.service.addPair(this.case.id).subscribe({
      next: (c) => this.adopt(c),
      error: (err: HttpErrorResponse) => this.fail(err, 'could not add a pair')
    });
  }

  removePair(pair: CasePair<TResult>): void {
    this.error = '';
    this.busy = true;
    this.service.removePair(this.case.id, pair.index).subscribe({
      next: (c) => this.adopt(c),
      error: (err: HttpErrorResponse) => this.fail(err, 'could not remove the pair')
    });
  }

  // One step: picking a file uploads it.
  onFile(pair: CasePair<TResult>, slot: string, event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    input.value = '';   // so re-picking the same file still fires (change)
    if (!file) return;
    this.error = '';
    this.busy = true;
    // Pair 0 IS the case's own upload slots — it has no entry in `extra_pairs`,
    // so it goes to /uploads/{slot}, not /pairs/0/uploads/{slot}.
    const upload =
      pair.index === 0
        ? this.service.uploadSlot(this.case.id, slot, file)
        : this.service.uploadPairSlot(this.case.id, pair.index, slot, file);
    upload.subscribe({
      next: (c) => this.adopt(c),
      error: (err: HttpErrorResponse) => this.fail(err, 'upload failed')
    });
  }

  private adopt(c: CaseDetail<TResult>): void {
    this.busy = false;
    // The server drops a pair's result when its document is replaced, so the
    // pair is unreviewed again and the lock is moot.
    this.unlocked.clear();
    this.caseChanged.emit(c);
  }

  private fail(err: HttpErrorResponse, fallback: string): void {
    this.error = err.error?.detail ?? fallback;
    this.busy = false;
  }
}
