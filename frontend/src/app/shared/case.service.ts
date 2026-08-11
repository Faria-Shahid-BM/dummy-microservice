import { HttpClient } from '@angular/common/http';
import { Injectable, InjectionToken } from '@angular/core';
import { Observable } from 'rxjs';
import { consumeSse } from '../sse.util';

export interface CaseSummary {
  id: string;
  name: string;
  status: 'new' | 'ready' | 'analyzing' | 'done' | 'failed';
  uploads: Record<string, string>;
  created_at: string;
}

// One pair on a case: a full set of the service's slots, analyzed in its own
// pass and keeping its own result (see case_store.py). Pair 0 is the case's own
// uploads; extras are added with addPair().
export interface CasePair<TResult = unknown> {
  index: number;
  uploads: Record<string, string>;
  result: TResult | null;
  error: string | null;
}

// Only present on the detail payload (see case_store.py's _detail_payload).
export interface CaseDetail<TResult = unknown> extends CaseSummary {
  result: TResult | null;
  error: string | null;
  /** Always present, always includes pair 0 — one entry per pair to render. */
  pairs: CasePair<TResult>[];
}

// One upload slot of a service's case — the `key`s are the multipart field
// names case_store.py's router expects (see each service's upload_slots).
export interface SlotDef {
  key: string;
  label: string;
  /** `accept` attribute for the file input. */
  accept: string;
}

// Every stateless review service (collateral/valuation/insurance/docdiff)
// exposes the identical /cases REST+SSE shape (see case_store.py) — this is
// the shared client for it. A concrete per-service class just supplies
// `apiBase`/`routeBase`; list/create/get/delete/upload/analyze are 100%
// shared here. Bound to CASE_SERVICE per-route (see app.routes.ts) so the
// one shared CaseListComponent can depend on this abstract type without
// knowing which concrete service backs the route it's mounted under.
@Injectable()
export abstract class CaseService<TResult = unknown> {
  /** e.g. '/api/collateral' — case_store.py's router is always mounted at `${apiBase}/cases`. */
  protected abstract readonly apiBase: string;
  /** e.g. '/collateral' — the Angular route these cases live under. */
  abstract readonly routeBase: string;
  /** e.g. 'Collateral' — shown as "<label> Cases" by the shared list page. */
  abstract readonly label: string;
  /** This service's upload slots, in the order the server pairs them. */
  abstract readonly slots: SlotDef[];
  /** Singular noun for one batch item, e.g. 'pair' / 'report' / 'policy'. */
  readonly itemNoun: string = 'document';

  constructor(protected http: HttpClient) {}

  listCases(): Observable<{ cases: CaseSummary[] }> {
    return this.http.get<{ cases: CaseSummary[] }>(`${this.apiBase}/cases`);
  }

  createCase(name: string): Observable<CaseDetail<TResult>> {
    return this.http.post<CaseDetail<TResult>>(`${this.apiBase}/cases`, { name });
  }

  getCase(caseId: string): Observable<CaseDetail<TResult>> {
    return this.http.get<CaseDetail<TResult>>(`${this.apiBase}/cases/${caseId}`);
  }

  deleteCase(caseId: string): Observable<{ ok: true }> {
    return this.http.delete<{ ok: true }>(`${this.apiBase}/cases/${caseId}`);
  }

  uploadSlot(caseId: string, slot: string, file: File): Observable<CaseDetail<TResult>> {
    const form = new FormData();
    form.append('file', file);
    return this.http.post<CaseDetail<TResult>>(`${this.apiBase}/cases/${caseId}/uploads/${slot}`, form);
  }

  // The analyze endpoint IS the SSE stream (see case_store.py) — same
  // consumeSse()-over-fetch() pattern dashboard.component.ts used for the
  // old one-shot /review/stream, just pointed at a case instead.
  analyzeCase(caseId: string, onFrame: (eventType: string, data: string) => void): Promise<void> {
    return consumeSse(`${this.apiBase}/cases/${caseId}/analyze`, null, onFrame);
  }

  // --- extra pairs on a case ---
  // Another full set of this service's slots, reviewed in its own pass and
  // keeping its own result, all on the same case.

  addPair(caseId: string): Observable<CaseDetail<TResult>> {
    return this.http.post<CaseDetail<TResult>>(`${this.apiBase}/cases/${caseId}/pairs`, {});
  }

  removePair(caseId: string, index: number): Observable<CaseDetail<TResult>> {
    return this.http.delete<CaseDetail<TResult>>(`${this.apiBase}/cases/${caseId}/pairs/${index}`);
  }

  uploadPairSlot(caseId: string, index: number, slot: string, file: File): Observable<CaseDetail<TResult>> {
    const form = new FormData();
    form.append('file', file);
    return this.http.post<CaseDetail<TResult>>(
      `${this.apiBase}/cases/${caseId}/pairs/${index}/uploads/${slot}`, form);
  }
}

export const CASE_SERVICE = new InjectionToken<CaseService>('CASE_SERVICE');
