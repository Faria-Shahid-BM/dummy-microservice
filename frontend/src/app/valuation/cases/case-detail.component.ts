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

  uploadError = '';
  /** Per-slot in-flight flag, keyed by slot name. */
  uploading: Record<string, boolean> = {};

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
    this.valuation.uploadSlot(this.caseId, slot, file).subscribe({
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
      })
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
