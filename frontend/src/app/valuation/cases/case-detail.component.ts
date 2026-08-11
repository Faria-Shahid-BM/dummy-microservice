import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { CaseDetail } from '../../shared/case.service';
import { CasePairsComponent } from '../../shared/case-pairs.component';
import { PairRun } from '../../shared/pair-run';
import { ValuationService } from '../valuation.service';
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
  imports: [CommonModule, RouterLink, CasePairsComponent, StageProgressComponent, JsonViewComponent],
  templateUrl: './case-detail.component.html'
})
export class CaseDetailComponent implements OnInit {
  readonly valuationStages = VALUATION_STAGES;

  caseId = '';
  case: CaseDetail<unknown> | null = null;
  loading = false;
  error = '';


  analyzing = false;
  analyzeError = '';
  /** Which pair is running, how far along, and which tab is on screen. */
  readonly run = new PairRun();

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


  /** Pairs with no result yet — what the main button will run. */
  get pendingPairs(): number {
    return (this.case?.pairs ?? []).filter((p) => p.result == null).length;
  }

  /** Says what pressing it will actually do, so "Review" never means a re-run. */
  get analyzeLabel(): string {
    if (this.analyzing) return 'Reviewing…';
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
