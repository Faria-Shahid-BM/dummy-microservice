import { CommonModule } from '@angular/common';
import { Component, Input, TemplateRef } from '@angular/core';
import { StageDef, StageProgressComponent } from '../stage-progress/stage-progress.component';
import { BatchItem, ReviewBatch } from './review-batch';

export interface SlotDef {
  /** Upload slot name — must match the service's form field (see batch.py). */
  key: string;
  label: string;
  /** `accept` attribute for the file input. */
  accept: string;
}

// The whole batch UI for one reviewer: a repeatable upload block per item with
// an add/remove control, a Compare button that submits only the not-yet-reviewed
// items, and a tab per item showing its live progress or its result.
//
// Everything except the result body is identical across the three reviewers, so
// the caller passes the result markup in as a template:
//
//   <app-review-batch [batch]="valuationBatch" ... [resultTemplate]="valResult">
//   <ng-template #valResult let-result>…</ng-template>
@Component({
  selector: 'app-review-batch',
  standalone: true,
  imports: [CommonModule, StageProgressComponent],
  templateUrl: './review-batch.component.html'
})
export class ReviewBatchComponent<TResult> {
  @Input({ required: true }) batch!: ReviewBatch<TResult>;
  @Input({ required: true }) slots: SlotDef[] = [];
  @Input({ required: true }) stages: StageDef[] = [];
  /** Rendered per finished item with the result as $implicit context. */
  @Input({ required: true }) resultTemplate!: TemplateRef<{ $implicit: TResult }>;

  /** Singular noun for one item, e.g. "pair" / "report" / "policy". */
  @Input() itemNoun = 'document';
  /** Plural of `itemNoun` — set it where adding "s" is wrong ("policies"). */
  @Input() itemNounPlural = '';
  /** Per-item block and tab label, e.g. "Pair 1" / "Report 1". */
  @Input() itemLabel = 'Document';
  /** Heading of the upload card. */
  @Input() heading = 'Documents';
  /** Verb on the submit button, e.g. "Review" / "Compare". */
  @Input() actionLabel = 'Review';
  /** Submit button text while a batch is in flight. */
  @Input() busyLabel = 'Reviewing…';
  /** Shown under the submit button. */
  @Input() hint = 'Calls a real LLM — this can take a little while.';

  get plural(): string {
    return this.itemNounPlural || `${this.itemNoun}s`;
  }

  get submitLabel(): string {
    if (this.batch.submitting) return this.busyLabel;
    const n = this.batch.pending.length;
    return n > 1 ? `${this.actionLabel} ${n} ${this.plural}` : this.actionLabel;
  }

  trackById = (_: number, item: BatchItem<TResult>) => item.id;
}
