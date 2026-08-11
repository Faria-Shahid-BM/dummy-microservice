import { CommonModule } from '@angular/common';
import { Component, OnInit, ViewChild } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { CaseDetail } from '../../shared/case.service';
import { BatchAddComponent } from '../../shared/batch-add.component';
import { BankPolicyStatus, InsuranceService } from '../insurance.service';
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
  imports: [CommonModule, RouterLink, BatchAddComponent, StageProgressComponent, JsonViewComponent],
  templateUrl: './case-detail.component.html'
})
export class CaseDetailComponent implements OnInit {
  readonly insuranceStages = INSURANCE_STAGES;

  caseId = '';
  case: CaseDetail<unknown> | null = null;
  loading = false;
  error = '';

  uploadError = '';
  /** Per-slot in-flight flag, keyed by slot name. */
  uploading: Record<string, boolean> = {};

  analyzing = false;
  analyzeError = '';
  progress: ReviewProgress = freshProgress();

  // The bank policy this account's reviews are graded against — standing
  // configuration shared by every case, not part of this one (see
  // insurance-service/main.py). Shown here because it's what the analysis
  // below is measured against, so it belongs where you press Analyze.
  bankPolicy: BankPolicyStatus | null = null;
  bankPolicyBusy = false;
  bankPolicyError = '';

  constructor(private route: ActivatedRoute, public insurance: InsuranceService) {}

  ngOnInit(): void {
    this.caseId = this.route.snapshot.paramMap.get('caseId') ?? '';
    this.loadCase();
    this.loadBankPolicy();
  }

  loadBankPolicy(): void {
    this.insurance.getBankPolicy().subscribe({
      next: (res) => (this.bankPolicy = res),
      error: (err: HttpErrorResponse) => {
        this.bankPolicyError = err.error?.detail ?? 'failed to load the bank policy';
      }
    });
  }

  // One-step upload: choosing a file sends it, so every review from here on is
  // graded against it (the file input itself is hidden behind a small button).
  uploadBankPolicy(event: Event): void {
    const file = (event.target as HTMLInputElement).files?.[0];
    if (!file) return;
    this.bankPolicyError = '';
    this.bankPolicyBusy = true;
    this.insurance.uploadBankPolicy(file).subscribe({
      next: (res) => {
        this.bankPolicy = res;
        this.bankPolicyBusy = false;
      },
      error: (err: HttpErrorResponse) => {
        this.bankPolicyError = err.error?.detail ?? 'upload failed';
        this.bankPolicyBusy = false;
      }
    });
  }

  deleteBankPolicy(): void {
    this.bankPolicyError = '';
    this.bankPolicyBusy = true;
    this.insurance.deleteBankPolicy().subscribe({
      next: (res) => {
        this.bankPolicy = res;
        this.bankPolicyBusy = false;
      },
      error: (err: HttpErrorResponse) => {
        this.bankPolicyError = err.error?.detail ?? 'delete failed';
        this.bankPolicyBusy = false;
      }
    });
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

  // One step: picking a file uploads it. A separate "Upload" button per slot
  // was two clicks for one intent, and left a chosen-but-not-uploaded state
  // that looked identical to uploaded.
  onSlotFile(slot: string, event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    input.value = '';   // so re-picking the same file still fires (change)
    if (!file) return;
    this.uploadError = '';
    this.uploading[slot] = true;
    this.insurance.uploadSlot(this.caseId, slot, file).subscribe({
      next: (c) => {
        this.case = c;
        this.uploading[slot] = false;
      },
      error: (err: HttpErrorResponse) => {
        this.uploadError = err.error?.detail ?? 'upload failed';
        this.uploading[slot] = false;
      }
    });
  }

  get canAnalyze(): boolean {
    return this.case?.status === 'ready' || this.case?.status === 'done' || this.case?.status === 'failed';
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
