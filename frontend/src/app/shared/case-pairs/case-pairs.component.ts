import { CommonModule } from '@angular/common';
import { Component, EventEmitter, Input, Output, TemplateRef } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { StageDef, StageProgressComponent } from '../../stage-progress/stage-progress.component';
import { CaseDetail, CasePair, CaseService } from '../case.service';
import { DocPreviewComponent } from '../doc-preview/doc-preview.component';
import { PairRun } from '../pair-run';

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
  templateUrl: './case-pairs.component.html',
  styleUrl: './case-pairs.component.css'
})
export class CasePairsComponent<TResult> {
  @Input({ required: true }) part!: 'uploads' | 'results';
  @Input({ required: true }) service!: CaseService<TResult>;
  @Input({ required: true }) case!: CaseDetail<TResult>;
  @Input({ required: true }) run!: PairRun;
  /** Stage checklist for this service's pipeline. */
  @Input() stages: StageDef[] = [];
  /** Rendered per finished pair, with that pair's result as $implicit and the
   * pair itself as `pair` — a result template that needs to call back to the
   * server (e.g. for the document text a citation points into) needs to know
   * which pair it belongs to. */
  @Input() resultTemplate?: TemplateRef<{ $implicit: TResult; pair: CasePair<TResult> }>;
  /** Extra buttons for the uploads tab bar's action cluster, left of Add and
   * Remove. For a case-level action a service owns (insurance's bank policy):
   * on its own line it reads as a step in the per-pair flow, which it isn't. */
  @Input() tabActions?: TemplateRef<void>;
  /** True while an analysis is in flight — freezes staging. */
  @Input() analyzing = false;

  /** The case reloaded after an upload/add/remove, so the parent can adopt it. */
  @Output() caseChanged = new EventEmitter<CaseDetail<TResult>>();
  /** Re-run just this pair (its tab's button) — the parent owns analysis. */
  @Output() analyzePair = new EventEmitter<number>();

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

  /** The pair whose tab is open — the one a tab-bar-level action applies to. */
  get activePair(): CasePair<TResult> | undefined {
    return this.pairs.find((p) => p.index === this.run.active);
  }

  /** Whether to show a tab per pair. A case that can't hold extra pairs only
   * ever has pair 0, so tabs (in either part) would just be noise. */
  get tabbed(): boolean {
    return this.service.allowExtraPairs;
  }

  label(pair: CasePair<TResult>): string {
    return pair.index === 0 ? `${this.noun} 1` : `${this.noun} ${pair.index + 1}`;
  }

  /**
   * Being reviewed right now — by this page's own stream, or by a run the
   * server is still doing for a page that has since been left and reopened
   * (case status `analyzing`, and this pair has no result to show yet).
   * Pairs run in order, so a not-yet-reached pair reads the same as the
   * running one: the server doesn't say which, and "in progress" is true of both.
   *
   * `ready` is what keeps that guess honest. The case-level status says a run
   * is happening somewhere on this case, not which pairs are in it — so after
   * re-running one pair and reloading the page, a pair still missing a
   * document was reported as under review, when the server would have refused
   * it ("Missing required upload(s)"). A pair that can't be run isn't running.
   */
  inProgress(pair: CasePair<TResult>): boolean {
    return (
      this.run.state(pair.index).running ||
      (this.case?.status === 'analyzing' && this.ready(pair) && pair.result == null && !pair.error)
    );
  }

  /** A pair is ready when every required slot has a file. */
  ready(pair: CasePair<TResult>): boolean {
    return this.service.slots.every((s) => !!pair.uploads?.[s.key]);
  }

  /**
   * What this pair is still waiting for, named — "the property / title
   * document", not "a document". Which slots exist is per service, so the
   * count and the wording both have to come from the service rather than
   * being written into the markup.
   */
  missingDocuments(pair: CasePair<TResult>): string {
    const missing = this.service.slots
      .filter((s) => !pair.uploads?.[s.key])
      // The accept list in the label ("(.docx or .pdf)") is guidance for the
      // picker, not part of the document's name.
      .map((s) => s.label.replace(/\s*\([^)]*\)\s*$/, '').toLowerCase());
    if (!missing.length) return '';
    if (missing.length === 1) return `the ${missing[0]}`;
    return `the ${missing.slice(0, -1).join(', the ')} and the ${missing[missing.length - 1]}`;
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

  // Slots the user has asked to swap a document into, `${pairIndex}:${slot}`.
  // A slot that already holds a document hides its picker — an upload is done,
  // and leaving the control there invites an accidental re-pick — so this is
  // what brings the picker back for that one slot.
  private replacing = new Set<string>();

  private slotId(pair: CasePair<TResult>, slotKey: string): string {
    return `${pair.index}:${slotKey}`;
  }

  /**
   * What this slot should show: the document, a picker, or nothing yet.
   *
   * Computed here rather than as three conditions in the template, because the
   * three states are mutually exclusive and saying so once keeps them that way.
   */
  slotState(pair: CasePair<TResult>, slotKey: string): 'filled' | 'picking' | 'waiting' {
    const hasFile = !!pair.uploads?.[slotKey];
    // A managed slot is never picked into, so it only ever has the document or
    // is still waiting for Document Generator to produce one.
    if (this.service.managedSlots.includes(slotKey)) return hasFile ? 'filled' : 'waiting';
    if (this.replacing.has(this.slotId(pair, slotKey))) return 'picking';
    return hasFile ? 'filled' : 'picking';
  }

  startReplace(pair: CasePair<TResult>, slotKey: string): void {
    this.unlock(pair);   // a reviewed pair has to re-open before its picker works
    this.replacing.add(this.slotId(pair, slotKey));
  }

  cancelReplace(pair: CasePair<TResult>, slotKey: string): void {
    this.replacing.delete(this.slotId(pair, slotKey));
  }

  addPair(): void {
    this.error = '';
    this.busy = true;
    this.service.addPair(this.case.id).subscribe({
      next: (c) => {
        this.adopt(c);
        // Open the pair that was just added, rather than leaving the user on
        // the tab they were on with no sign anything happened.
        const added = c.pairs?.[c.pairs.length - 1];
        if (added) this.run.select(added.index);
      },
      error: (err: HttpErrorResponse) => this.fail(err, 'could not add a pair')
    });
  }

  removePair(pair: CasePair<TResult>): void {
    this.error = '';
    this.busy = true;
    this.service.removePair(this.case.id, pair.index).subscribe({
      next: (c) => {
        this.adopt(c);
        // `active` is an index into the pairs that are left; without this it
        // can point past the end and every panel's *ngIf fails, leaving the
        // section blank with no tab appearing selected.
        const last = (c.pairs?.length ?? 1) - 1;
        if (this.run.active > last) this.run.select(Math.max(0, last));
      },
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
    // The swap is done — every slot goes back to showing its document.
    this.replacing.clear();
    this.caseChanged.emit(c);
  }

  private fail(err: HttpErrorResponse, fallback: string): void {
    this.error = err.error?.detail ?? fallback;
    this.busy = false;
  }
}
