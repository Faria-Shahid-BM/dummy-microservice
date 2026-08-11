import { CommonModule } from '@angular/common';
import { Component, OnInit, ViewChild } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { CollateralResult, CollateralService } from '../collateral.service';
import { CaseDetail } from '../../shared/case.service';
import { BatchAddComponent } from '../../shared/batch-add.component';
import { applyStageEvent, freshProgress, parseSseError, ReviewProgress } from '../../sse.util';
import { StageDef, StageProgressComponent } from '../../stage-progress/stage-progress.component';

// Stage keys/order come straight from engines/collateral.py's _emit_event()
// calls — a stage's "event" arriving means every earlier stage here is done.
// "done" is handled separately as a completion signal, not a checklist step.
const COLLATERAL_STAGES: StageDef[] = [
  { key: 'extract_text', label: 'Extracting documents' },
  { key: 'extract_fields', label: 'Extracting fields' },
  { key: 'compare', label: 'Comparing fields' },
  { key: 'observations', label: 'Generating observations' }
];

@Component({
  selector: 'app-collateral-case-detail',
  standalone: true,
  imports: [CommonModule, RouterLink, BatchAddComponent, StageProgressComponent],
  templateUrl: './case-detail.component.html'
})
export class CaseDetailComponent implements OnInit {
  readonly collateralStages = COLLATERAL_STAGES;

  caseId = '';
  case: CaseDetail<CollateralResult> | null = null;
  loading = false;
  error = '';

  legalFile: File | null = null;
  propertyFile: File | null = null;
  uploadingLegal = false;
  uploadingProperty = false;
  uploadError = '';

  analyzing = false;
  analyzeError = '';
  progress: ReviewProgress = freshProgress();
  streamingText = '';

  constructor(private route: ActivatedRoute, public collateral: CollateralService) {}

  ngOnInit(): void {
    this.caseId = this.route.snapshot.paramMap.get('caseId') ?? '';
    this.loadCase();
  }

  loadCase(): void {
    if (!this.caseId) return;
    this.error = '';
    this.loading = true;
    this.collateral.getCase(this.caseId).subscribe({
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

  onLegalFile(event: Event): void {
    this.legalFile = (event.target as HTMLInputElement).files?.[0] ?? null;
  }

  onPropertyFile(event: Event): void {
    this.propertyFile = (event.target as HTMLInputElement).files?.[0] ?? null;
  }

  uploadLegal(): void {
    if (!this.legalFile) return;
    this.uploadError = '';
    this.uploadingLegal = true;
    this.collateral.uploadSlot(this.caseId, 'legal', this.legalFile).subscribe({
      next: (c) => {
        this.case = c;
        this.legalFile = null;
        this.uploadingLegal = false;
      },
      error: (err: HttpErrorResponse) => {
        this.uploadError = err.error?.detail ?? 'upload failed';
        this.uploadingLegal = false;
      }
    });
  }

  uploadProperty(): void {
    if (!this.propertyFile) return;
    this.uploadError = '';
    this.uploadingProperty = true;
    this.collateral.uploadSlot(this.caseId, 'property', this.propertyFile).subscribe({
      next: (c) => {
        this.case = c;
        this.propertyFile = null;
        this.uploadingProperty = false;
      },
      error: (err: HttpErrorResponse) => {
        this.uploadError = err.error?.detail ?? 'upload failed';
        this.uploadingProperty = false;
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
    this.streamingText = '';

    this.collateral
      .analyzeCase(this.caseId, (eventType, data) => {
        if (eventType === 'event') {
          applyStageEvent(this.progress, data);
        } else if (eventType === 'content') {
          // Live LLM output during the "observations" stage.
          try {
            this.streamingText += JSON.parse(data) as string;
          } catch {
            /* malformed chunk — skip it rather than corrupt the buffer */
          }
        } else if (eventType === 'result') {
          this.progress.complete = true;
          this.loadCase(); // pick up the now-persisted status + result
        } else if (eventType === 'error') {
          this.analyzeError = parseSseError(data);
        }
      })
      .catch((err) => {
        this.analyzeError = err instanceof Error ? err.message : 'analysis failed';
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
