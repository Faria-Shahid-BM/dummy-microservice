import { CommonModule } from '@angular/common';
import { Component, OnInit, ViewChild } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { CaseDetail } from '../../shared/case.service';
import { BatchAddComponent } from '../../shared/batch-add.component';
import { ValuationService } from '../valuation.service';
import { applyStageEvent, freshProgress, parseSseError, ReviewProgress } from '../../sse.util';
import { StageDef, StageProgressComponent } from '../../stage-progress/stage-progress.component';
import { JsonViewComponent } from '../../json-view/json-view.component';

// Stage keys/order come straight from engines/valuation.py's _emit_event()
// calls — a stage's "event" arriving means every earlier stage here is
// done. "done" is a completion signal, not a checklist step.
const VALUATION_STAGES: StageDef[] = [
  { key: 'extract_text', label: 'Extracting report' },
  { key: 'extract_fields', label: 'Extracting fields' },
  { key: 'panel_check', label: 'Checking approved-valuer panel' }
];

@Component({
  selector: 'app-valuation-case-detail',
  standalone: true,
  imports: [CommonModule, RouterLink, BatchAddComponent, StageProgressComponent, JsonViewComponent],
  templateUrl: './case-detail.component.html'
})
export class CaseDetailComponent implements OnInit {
  readonly valuationStages = VALUATION_STAGES;

  caseId = '';
  case: CaseDetail<unknown> | null = null;
  loading = false;
  error = '';

  reportFile: File | null = null;
  uploadingReport = false;
  uploadError = '';

  analyzing = false;
  analyzeError = '';
  progress: ReviewProgress = freshProgress();

  constructor(private route: ActivatedRoute, public valuation: ValuationService) {}

  ngOnInit(): void {
    this.caseId = this.route.snapshot.paramMap.get('caseId') ?? '';
    this.loadCase();
  }

  loadCase(): void {
    if (!this.caseId) return;
    this.error = '';
    this.loading = true;
    this.valuation.getCase(this.caseId).subscribe({
      next: (c) => {
        this.case = c;
        this.loading = false;
      },
      error: (err: HttpErrorResponse) => {
        this.error = err.error?.detail ?? 'failed to load case';
        this.loading = false;
      }
    });
  }

  get canAnalyze(): boolean {
    return this.case?.status === 'ready' || this.case?.status === 'done' || this.case?.status === 'failed';
  }

  onReportFile(event: Event): void {
    this.reportFile = (event.target as HTMLInputElement).files?.[0] ?? null;
  }

  uploadReport(): void {
    if (!this.reportFile) return;
    this.uploadError = '';
    this.uploadingReport = true;
    this.valuation.uploadSlot(this.caseId, 'report', this.reportFile).subscribe({
      next: (c) => {
        this.case = c;
        this.reportFile = null;
        this.uploadingReport = false;
      },
      error: (err: HttpErrorResponse) => {
        this.uploadError = err.error?.detail ?? 'upload failed';
        this.uploadingReport = false;
      }
    });
  }

  analyze(): void {
    // Reviewing only the extra items is legitimate — this case may have
    // nothing uploaded yet.
    if (!this.canAnalyze) {
      this.runExtras();
      return;
    }
    this.analyzeError = '';
    this.analyzing = true;
    this.progress = freshProgress();

    this.valuation
      .analyzeCase(this.caseId, (eventType, data) => {
        if (eventType === 'event') {
          applyStageEvent(this.progress, data);
        } else if (eventType === 'result') {
          this.progress.complete = true;
          this.loadCase(); // pick up the now-persisted status + result
        } else if (eventType === 'error') {
          this.analyzeError = parseSseError(data);
        }
      })
      .catch((err) => {
        this.analyzeError = err instanceof Error ? err.message : 'review failed';
      })
      .finally(() => {
        this.analyzing = false;
        this.runExtras();
        if (!this.case || this.case.status === 'analyzing') this.loadCase();
      });
  }

  // Extra documents staged beside this case's uploads (app-batch-add), each
  // becoming its own case. Reviewed after this one, so a stack goes through in
  // a single press of Review.
  @ViewChild(BatchAddComponent) extras?: BatchAddComponent;

  get hasExtras(): boolean {
    return this.extras?.hasPending ?? false;
  }

  private runExtras(): void {
    if (this.extras?.hasPending) void this.extras.run();
  }
}
