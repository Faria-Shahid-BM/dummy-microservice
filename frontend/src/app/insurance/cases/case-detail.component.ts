import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { CaseDetail } from '../../shared/case.service';
import { InsuranceService } from '../insurance.service';
import { applyStageEvent, freshProgress, parseSseError, ReviewProgress } from '../../sse.util';
import { StageDef, StageProgressComponent } from '../../stage-progress/stage-progress.component';
import { JsonViewComponent } from '../../json-view/json-view.component';

// Stage keys/order come straight from engines/insurance.py's _emit_event()
// calls — a stage's "event" arriving means every earlier stage here is
// done. Insurance has no "done" completion event (unlike collateral/
// valuation), so progress just stays on the last stage until the result
// frame arrives.
const INSURANCE_STAGES: StageDef[] = [
  { key: 'extract', label: 'Extracting policy' },
  { key: 'structure', label: 'Structuring policy data' },
  { key: 'analyze', label: 'Analyzing against bank policy' }
];

@Component({
  selector: 'app-insurance-case-detail',
  standalone: true,
  imports: [CommonModule, RouterLink, StageProgressComponent, JsonViewComponent],
  templateUrl: './case-detail.component.html'
})
export class CaseDetailComponent implements OnInit {
  readonly insuranceStages = INSURANCE_STAGES;

  caseId = '';
  case: CaseDetail<unknown> | null = null;
  loading = false;
  error = '';

  policyFile: File | null = null;
  uploadingPolicy = false;
  uploadError = '';

  analyzing = false;
  analyzeError = '';
  progress: ReviewProgress = freshProgress();

  constructor(private route: ActivatedRoute, private insurance: InsuranceService) {}

  ngOnInit(): void {
    this.caseId = this.route.snapshot.paramMap.get('caseId') ?? '';
    this.loadCase();
  }

  loadCase(): void {
    if (!this.caseId) return;
    this.error = '';
    this.loading = true;
    this.insurance.getCase(this.caseId).subscribe({
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

  onPolicyFile(event: Event): void {
    this.policyFile = (event.target as HTMLInputElement).files?.[0] ?? null;
  }

  uploadPolicy(): void {
    if (!this.policyFile) return;
    this.uploadError = '';
    this.uploadingPolicy = true;
    this.insurance.uploadSlot(this.caseId, 'policy', this.policyFile).subscribe({
      next: (c) => {
        this.case = c;
        this.policyFile = null;
        this.uploadingPolicy = false;
      },
      error: (err: HttpErrorResponse) => {
        this.uploadError = err.error?.detail ?? 'upload failed';
        this.uploadingPolicy = false;
      }
    });
  }

  analyze(): void {
    if (!this.canAnalyze) return;
    this.analyzeError = '';
    this.analyzing = true;
    this.progress = freshProgress();

    this.insurance
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
        if (!this.case || this.case.status === 'analyzing') this.loadCase();
      });
  }
}
