import { CommonModule } from '@angular/common';
import { Component, OnDestroy, OnInit } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { CaseDetail } from '../../shared/case.service';
import { CasePairsComponent } from '../../shared/case-pairs/case-pairs.component';
import { PairRun } from '../../shared/pair-run';
import { CaseWatch } from '../../shared/case-watch';
import { ValuationService } from '../valuation.service';
import { StageDef, StageProgressComponent } from '../../stage-progress/stage-progress.component';
import { SectionReportComponent } from '../../shared/section-report/section-report.component';
import { VALUATION_REPORT } from '../report/valuation-report.config';

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
  imports: [CommonModule, RouterLink, CasePairsComponent, StageProgressComponent, SectionReportComponent],
  templateUrl: './case-detail.component.html'
})
export class CaseDetailComponent implements OnInit, OnDestroy {
  readonly valuationStages = VALUATION_STAGES;
  readonly reportSpec = VALUATION_REPORT;

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

  constructor(private route: ActivatedRoute, public valuation: ValuationService) {}

  ngOnInit(): void {
    this.caseId = this.route.snapshot.paramMap.get('caseId') ?? '';
    this.loadCase();
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
    this.valuation.getCase(this.caseId).subscribe({
      next: (c) => {
        this.case = c;
        this.loading = false;
        this.watch.sync(c.status, this.analyzing, () => this.loadCase());
        // A run this page didn't start has no stream here, so the stage
        // checklist comes from the server's own snapshot instead of frames.
        if (this.serverBusy) this.watch.followProgress(this.valuation, this.caseId, this.run, c.pairs?.length ?? 1);
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

    this.valuation
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
