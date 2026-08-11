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

// Only present on the detail payload (see case_store.py's _detail_payload).
export interface CaseDetail<TResult = unknown> extends CaseSummary {
  result: TResult | null;
  error: string | null;
}

// One upload slot of a service's case — the `key`s are the multipart field
// names case_store.py's router expects (see each service's upload_slots).
export interface SlotDef {
  key: string;
  label: string;
  /** `accept` attribute for the file input. */
  accept: string;
}

// One staged item of a batch submission: one file per slot, which the server
// turns into its own persisted case (see case_store.py's POST /cases/batch).
export interface BatchItem {
  /** Client-side row id; the server's case id arrives on the case_start frame. */
  key: string;
  files: Record<string, File | null>;
  caseId: string | null;
  name: string;
  /** True once the user types a name, so picking a file stops overwriting it. */
  nameEdited?: boolean;
  running: boolean;
  error: string;
  done: boolean;
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

  /**
   * Create and analyze several cases in one request, streaming each one's
   * outcome as it lands (see case_store.py's POST /cases/batch). One repeated
   * form field per slot, paired by position, plus a `names` field per item —
   * so item i is `slots.map(s => files[s][i])`, named `names[i]`.
   */
  analyzeBatch(items: BatchItem[], onFrame: (eventType: string, data: string) => void): Promise<void> {
    const form = new FormData();
    for (const item of items) {
      for (const slot of this.slots) form.append(slot.key, item.files[slot.key]!);
      form.append('names', item.name);
    }
    return consumeSse(`${this.apiBase}/cases/batch`, form, onFrame);
  }
}

export const CASE_SERVICE = new InjectionToken<CaseService>('CASE_SERVICE');
