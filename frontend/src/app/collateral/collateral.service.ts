import { Injectable } from '@angular/core';
import { Observable } from 'rxjs';
import { shareReplay } from 'rxjs/operators';
import { KONG_BASE } from '../session.service';
import { CaseService, SlotDef } from '../shared/case.service';

/**
 * How the engine decided a row matched. The two documents word the same fact
 * differently all the time, so a match can rest on anything from a literal
 * agreement ('exact') to an LLM judging the two values to name the same thing
 * ('semantic') — the table shows the difference rather than flattening it.
 * Null on a non-match. Optional: results stored before this existed have none.
 */
export type MatchBasis = 'exact' | 'normalized' | 'fuzzy' | 'trimmed' | 'semantic';

export interface CollateralComparisonRow {
  field: string;
  label: string;
  legal_value: string | null;
  property_value: string | null;
  status: 'match' | 'mismatch' | 'missing';
  match_basis?: MatchBasis | null;
  /** The adjudicator's one-clause reason, on a 'semantic' match only. */
  match_reason?: string | null;
  /** 0–1 textual similarity of the canonical forms; null when not scored. */
  similarity?: number | null;
}

/**
 * Where an extracted value sits in the document text the engine read — what
 * makes a field a citation rather than a claim. `found_by` says how precisely:
 * 'exact' (character for character) → 'whitespace' (same once spacing and case
 * are ignored) → 'core' (the trimmed core matched) → 'approximate' (only part of
 * the value is in the document, `confidence` = how much of it).
 *
 * A field whose `evidence` is null is NOT a rendering problem: the value the
 * model reported is not in the document at all, which is worth surfacing.
 *
 * `page` is null whenever the extraction carried no page markers — a .docx has
 * no pages and a text-layer PDF arrives unmarked, so null means "unknowable",
 * never page 1. Check `sources[doc].paged` before showing any page at all.
 */
export interface EvidenceSpan {
  start: number;
  end: number;
  page: number | null;
  found_by: 'exact' | 'whitespace' | 'core' | 'approximate';
  confidence: number;
}

/** One leaf of the engine's extraction schema. */
export interface ExtractedField {
  value: string | null;
  source_page: number | null;
  /** The value with surrounding narrative trimmed off — used for comparing. */
  core: string | null;
  evidence: EvidenceSpan | null;
}

/** What the engine read one document as: {section: {field: leaf}}. */
export type ExtractedDocument = Record<string, Record<string, ExtractedField>>;

/** Per-document account of the text the values were read from. */
export interface SourceReport {
  chars: number;
  pages_total: number;
  pages_failed: number[];
  /** Whether page numbers are knowable here at all (see EvidenceSpan.page). */
  paged: boolean;
  values_located: number;
  values_total: number;
}

export interface CollateralResult {
  /** Optional: results stored before evidence tracking existed have neither. */
  extracted?: Record<'legal_opinion' | 'property_document', ExtractedDocument>;
  sources?: Record<'legal_opinion' | 'property_document', SourceReport>;
  comparison: CollateralComparisonRow[];
  observations: string[];
  summary: {
    matches: number;
    mismatches: number;
    missing: number;
    /** Matches reached by judgement rather than literal agreement. */
    adjudicated?: number;
    fields: number;
  };
}

@Injectable({ providedIn: 'root' })
export class CollateralService extends CaseService<CollateralResult> {
  protected readonly apiBase = `${KONG_BASE}/api/collateral`;
  readonly routeBase = '/collateral';
  readonly label = 'Collateral';
  readonly slots: SlotDef[] = [
    { key: 'legal', label: 'Legal opinion (.docx or .pdf)', accept: '.docx,.pdf' },
    { key: 'property', label: 'Property / title document (.docx or .pdf)', accept: '.docx,.pdf' }
  ];
  override readonly itemNoun = 'pair';

  // One in-flight/completed request per document, shared by every field the
  // reviewer clicks: the text is the same for all nine fields of a document, so
  // fetching it once per (case, pair, slot) is what makes clicking through them
  // feel instant. shareReplay(1) both caches and de-duplicates concurrent asks.
  private readonly sourceTexts = new Map<string, Observable<string>>();

  /**
   * The exact text one document was read as — what a review's evidence offsets
   * index into (see collateral-service). 404s until that pair has been
   * analyzed, which the caller should present as "review this pair again"
   * rather than as a failure.
   */
  sourceText(caseId: string, pairIndex: number, slot: string): Observable<string> {
    const key = `${caseId}/${pairIndex}/${slot}`;
    let stream = this.sourceTexts.get(key);
    if (!stream) {
      stream = this.http
        .get(`${this.apiBase}/cases/${caseId}/pairs/${pairIndex}/source/${slot}`, {
          responseType: 'text'
        })
        .pipe(shareReplay(1));
      this.sourceTexts.set(key, stream);
    }
    return stream;
  }

  /** Re-analysis rewrites the text those offsets point into, so the cached
   * copies stop being the ones the new result cites. */
  clearSourceTexts(): void {
    this.sourceTexts.clear();
  }
}
