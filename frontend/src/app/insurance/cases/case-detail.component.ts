import { CommonModule } from '@angular/common';
import { Component, OnDestroy, OnInit } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { CaseDetail } from '../../shared/case.service';
import { CasePairsComponent } from '../../shared/case-pairs/case-pairs.component';
import { PairRun } from '../../shared/pair-run';
import { CaseWatch } from '../../shared/case-watch';
import { BankPolicyStatus, InsuranceService } from '../insurance.service';
import { StageDef, StageProgressComponent } from '../../stage-progress/stage-progress.component';
import { SectionReportComponent } from '../../shared/section-report/section-report.component';
import { INSURANCE_REPORT } from '../report/insurance-report.config';

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
  imports: [CommonModule, RouterLink, CasePairsComponent, StageProgressComponent, SectionReportComponent],
  templateUrl: './case-detail.component.html'
})
export class CaseDetailComponent implements OnInit, OnDestroy {
  readonly insuranceStages = INSURANCE_STAGES;
  readonly reportSpec = INSURANCE_REPORT;

  caseId = '';
  case: CaseDetail<unknown> | null = null;
  loading = false;
  error = '';


  analyzing = false;
  analyzeError = '';
  /** Which pair is running, how far along, and which tab is on screen. */
  readonly run = new PairRun();
  /** Follows a review still running on the server when this page isn't the one streaming it. */
  private readonly watch = new CaseWatch();

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

  ngOnDestroy(): void {
    this.watch.stop();
  }

  /** A review is running that this page didn't start (we left and came back). */
  get serverBusy(): boolean {
    return this.case?.status === 'analyzing' && !this.analyzing;
  }

  loadCase(): void {
    if (!this.caseId) return;
    this.error = '';
    this.loading = true;
    this.insurance.getCase(this.caseId).subscribe({
      next: (c) => {
        this.case = c;
        this.loading = false;
        this.watch.sync(c.status, this.analyzing, () => this.loadCase());
        // A run this page didn't start has no stream here, so the stage
        // checklist comes from the server's own snapshot instead of frames.
        if (this.serverBusy) this.watch.followProgress(this.insurance, this.caseId, this.run, c.pairs?.length ?? 1);
        else if (!this.analyzing) this.run.stopAdopted();
      },
      error: (err: HttpErrorResponse) => {
        this.error = err.error?.detail ?? 'failed to load case';
        this.loading = false;
      }
    });
  }


  /** Pairs with no result yet — what the main button will run. */
  get pendingPairs(): number {
    return (this.case?.pairs ?? []).filter((p) => p.result == null).length;
  }

  /** Says what pressing it will actually do, so "Review" never means a re-run. */
  get analyzeLabel(): string {
    if (this.analyzing || this.serverBusy) return 'Reviewing…';
    const pending = this.pendingPairs;
    if (!pending) return 'Review everything again';
    if (this.case && this.case.pairs.length > 1) {
      return pending === this.case.pairs.length ? 'Review all' : `Review ${pending} new`;
    }
    return 'Review';
  }

  get canAnalyze(): boolean {
    return this.case?.status === 'ready' || this.case?.status === 'done' || this.case?.status === 'failed';
  }



  /**
   * `scope` defaults to the pairs that haven't been reviewed yet, so adding a
   * pair to an already-reviewed case doesn't pay for the old pairs again.
   */
  analyze(scope: 'pending' | 'all' | number = 'pending'): void {
    if (!this.canAnalyze || this.analyzing || !this.case) return;
    this.analyzeError = '';
    this.analyzing = true;
    // One tab per pair on the case; the server analyzes them in the same order.
    this.run.start(this.case.pairs.length);
    // A re-run replaces whichever pairs are in scope — clear their old
    // result/error from the UI now rather than leaving it on screen (looking
    // current) until the new one streams in. Mirrors the server's own
    // "pending" selection: a pair with no result yet (including one that only
    // has a stored error from a failed attempt) is already about to run.
    for (const pair of this.case.pairs) {
      if (scope === 'all' || scope === pair.index || (scope === 'pending' && pair.result == null)) {
        pair.result = null;
        pair.error = null;
      }
    }

    this.insurance
      .analyzeCase(this.caseId, (eventType, data) => {
        this.run.onFrame(eventType, data, (index, result) => {
          // Fill that pair's tab the moment its result lands, rather than
          // waiting for the whole run and a reload.
          const pair = this.case?.pairs[index];
          if (pair) pair.result = result as never;
        });
        if (eventType === 'error') this.analyzeError = this.run.error;
      }, scope)
      .catch((err) => {
        this.analyzeError = err instanceof Error ? err.message : 'analysis failed';
      })
      .finally(() => {
        this.analyzing = false;
        this.run.finish();
        this.loadCase();   // pick up the persisted per-pair results + status
      });
  }

}
