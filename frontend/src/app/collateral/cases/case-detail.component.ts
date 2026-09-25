import { CommonModule } from '@angular/common';
import { Component, OnDestroy, OnInit } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { ActivatedRoute, RouterLink } from '@angular/router';
import {
  CollateralComparisonRow,
  CollateralResult,
  CollateralService,
  ExtractedDocument,
  ExtractedField
} from '../collateral.service';
import { CaseDetail, CasePair } from '../../shared/case.service';
import { CasePairsComponent } from '../../shared/case-pairs/case-pairs.component';
import { EvidencePanelComponent, EvidenceView } from '../../shared/evidence-panel.component';
import { PairRun } from '../../shared/pair-run';
import { CaseWatch } from '../../shared/case-watch';
import { StageDef, StageProgressComponent } from '../../stage-progress/stage-progress.component';

/**
 * The server's own reason for a failure, whatever shape the body arrived in.
 *
 * A request made with `responseType: 'text'` hands back the raw body as a
 * string rather than parsed JSON, so reading `err.error.detail` off it is
 * always undefined — which is how "review this pair again" came out as
 * "could not load the document text".
 */
function errorDetail(err: HttpErrorResponse, fallback: string): string {
  const body: unknown = err.error;
  if (typeof body === 'string') {
    try {
      return (JSON.parse(body) as { detail?: string })?.detail ?? body ?? fallback;
    } catch {
      return body || fallback;
    }
  }
  return (body as { detail?: string })?.detail ?? fallback;
}

// Stage keys/order come straight from engines/collateral.py's _emit_event()
// calls — a stage's "event" arriving means every earlier stage here is done.
// "done" is handled separately as a completion signal, not a checklist step.
const COLLATERAL_STAGES: StageDef[] = [
  { key: 'extract_text', label: 'Extracting documents' },
  // Confirms each upload is the kind of document its slot claimed, before any
  // of the expensive steps below run against the wrong file.
  { key: 'verify_documents', label: 'Verifying documents' },
  { key: 'extract_fields', label: 'Extracting fields' },
  { key: 'compare', label: 'Comparing fields' },
  // Only emitted when the mechanical comparison left something ambiguous; a
  // stage that never arrives is ticked off by the next one that does.
  { key: 'adjudicate', label: 'Resolving wording differences' },
  { key: 'observations', label: 'Generating observations' }
];

// A match the engine had to interpret its way to is worth flagging in the
// table: it is a weaker kind of agreement than the two documents saying the
// same thing. 'exact' and 'normalized' need no badge — those are literal.
const BASIS_BADGES: Record<string, string> = {
  fuzzy: 'near-identical',
  trimmed: 'same core value',
  semantic: 'same meaning'
};

// The two upload slots, as the engine names the documents it read them as, and
// as a reviewer reads them.
type DocKey = 'legal_opinion' | 'property_document';
const DOC_FOR_SLOT: Record<string, DocKey> = {
  legal: 'legal_opinion',
  property: 'property_document'
};
const DOC_LABEL: Record<string, string> = {
  legal: 'Legal opinion',
  property: 'Property document'
};

@Component({
  selector: 'app-collateral-case-detail',
  standalone: true,
  imports: [
    CommonModule,
    RouterLink,
    CasePairsComponent,
    StageProgressComponent,
    EvidencePanelComponent
  ],
  templateUrl: './case-detail.component.html'
})
export class CaseDetailComponent implements OnInit, OnDestroy {
  readonly collateralStages = COLLATERAL_STAGES;

  /** The value currently being traced back to its document, if any. */
  evidence: EvidenceView | null = null;
  // Clicking a second value while the first is still loading must not let the
  // slower response land in the panel; only the newest click owns it.
  private evidenceRequest = 0;
  /** Which pair the open citation belongs to, so the panel's "review again"
   * re-runs that pair rather than the case. */
  private evidencePairIndex = 0;

  caseId = '';
  case: CaseDetail<CollateralResult> | null = null;
  loading = false;
  error = '';


  analyzing = false;
  analyzeError = '';
  /** Which pair is running, how far along, and which tab is on screen. */
  readonly run = new PairRun();
  /** Follows a review still running on the server when this page isn't the one streaming it. */
  private readonly watch = new CaseWatch();

  constructor(private route: ActivatedRoute, public collateral: CollateralService) {}

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
    this.collateral.getCase(this.caseId).subscribe({
      next: (c) => {
        this.case = c;
        this.loading = false;
        this.watch.sync(c.status, this.analyzing, () => this.loadCase());
        // A run this page didn't start has no stream here, so the stage
        // checklist comes from the server's own snapshot instead of frames.
        if (this.serverBusy) this.watch.followProgress(this.collateral, this.caseId, this.run, c.pairs?.length ?? 1);
        else if (!this.analyzing) this.run.stopAdopted();
      },
      error: (err: HttpErrorResponse) => {
        this.error = err.error?.detail ?? 'failed to load case';
        this.loading = false;
      }
    });
  }


  // --- tracing a value back to the document it came from ---
  // The engine reports each document as {section: {field: leaf}}; the table
  // works in flat field names, so flatten once per document and remember it
  // (change detection calls the template's lookups constantly).
  private readonly fieldsByName = new WeakMap<object, Map<string, ExtractedField>>();

  private leaf(result: CollateralResult, doc: DocKey, field: string): ExtractedField | null {
    const document: ExtractedDocument | undefined = result.extracted?.[doc];
    if (!document) return null;
    let index = this.fieldsByName.get(document as object);
    if (!index) {
      index = new Map<string, ExtractedField>();
      for (const section of Object.values(document)) {
        for (const [name, field_] of Object.entries(section)) index.set(name, field_);
      }
      this.fieldsByName.set(document as object, index);
    }
    return index.get(field) ?? null;
  }

  /** Results stored before citations existed have nothing to trace to, so they
   * render as plain text with no affordance at all. */
  inspectable(result: CollateralResult): boolean {
    return !!result.extracted;
  }

  /** Whether this value was actually found in its document. */
  hasEvidence(result: CollateralResult, doc: DocKey, row: CollateralComparisonRow): boolean {
    return !!this.leaf(result, doc, row.field)?.evidence;
  }

  evidenceHint(result: CollateralResult, doc: DocKey, row: CollateralComparisonRow): string {
    return this.hasEvidence(result, doc, row)
      ? 'Show where this came from in the document'
      : 'This value was not found in the document — open to check';
  }

  /** Open the panel on one field of one document of one pair. */
  showEvidence(
    pair: CasePair<CollateralResult>,
    slot: 'legal' | 'property',
    row: CollateralComparisonRow
  ): void {
    const result = pair.result;
    if (!result?.extracted) return;
    const doc = DOC_FOR_SLOT[slot];
    const leaf = this.leaf(result, doc, row.field);
    const source = result.sources?.[doc];
    const request = ++this.evidenceRequest;
    this.evidencePairIndex = pair.index;

    this.evidence = {
      docLabel: DOC_LABEL[slot],
      fileName: pair.uploads?.[slot] ?? '',
      fieldLabel: row.label,
      value: (slot === 'legal' ? row.legal_value : row.property_value) ?? '',
      span: leaf?.evidence ?? null,
      // A page number only means something where the document was read page by
      // page — a .docx has no pages, so naming one would be inventing it.
      page: source?.paged ? leaf?.evidence?.page ?? null : null,
      text: null,
      loading: true,
      error: '',
      stale: false
    };

    this.collateral.sourceText(this.caseId, pair.index, slot).subscribe({
      next: (text) => {
        if (request !== this.evidenceRequest || !this.evidence) return;
        this.evidence = { ...this.evidence, text, loading: false };
      },
      error: (err: HttpErrorResponse) => {
        if (request !== this.evidenceRequest || !this.evidence) return;
        this.evidence = {
          ...this.evidence,
          loading: false,
          // 404 = the text was never written for this pair (a review from
          // before citations were recorded). The panel offers the re-run that
          // fixes it instead of reporting a failure the reviewer can't act on.
          stale: err.status === 404,
          error: err.status === 404 ? '' : errorDetail(err, 'could not load the document text')
        };
      }
    });
  }

  /** From the panel's stale-citation notice: re-run just that pair, which
   * rewrites both the result and the text its offsets point into. */
  rerunForEvidence(): void {
    const index = this.evidencePairIndex;
    this.closeEvidence();
    this.analyze(index);
  }

  closeEvidence(): void {
    this.evidenceRequest++;   // orphan any response still in flight
    this.evidence = null;
  }

  /** Short badge for a match that needed interpreting, '' for a literal one. */
  basisBadge(row: CollateralComparisonRow): string {
    return (row.match_basis && BASIS_BADGES[row.match_basis]) || '';
  }

  /** Hover text: why the engine let this row pass, and how close the wording was. */
  basisTitle(row: CollateralComparisonRow): string {
    const reason = row.match_reason?.trim();
    if (reason) return reason;
    if (row.match_basis === 'fuzzy' && row.similarity != null) {
      return `Wording differs but the values agree once normalized (${Math.round(row.similarity * 100)}% similar).`;
    }
    return 'Matched on meaning rather than on identical wording.';
  }

  /** Pairs with no result yet — what the main button will run. */
  get pendingPairs(): number {
    return (this.case?.pairs ?? []).filter((p) => p.result == null).length;
  }

  /** Says what pressing it will actually do, so "Compare" never means a re-run. */
  get analyzeLabel(): string {
    if (this.analyzing || this.serverBusy) return 'Comparing…';
    const pending = this.pendingPairs;
    if (!pending) return 'Review everything again';
    if (this.case && this.case.pairs.length > 1) {
      return pending === this.case.pairs.length ? 'Compare all' : `Compare ${pending} new`;
    }
    return 'Compare';
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
    // A re-run rewrites both the citations and the text they point into, so
    // anything on screen or cached from the previous run is about to be stale.
    this.closeEvidence();
    this.collateral.clearSourceTexts();
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

    this.collateral
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
