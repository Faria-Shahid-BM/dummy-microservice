import { CommonModule } from '@angular/common';
import { Component } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { SessionService, KONG_BASE } from '../session.service';
import { JsonViewComponent } from '../json-view/json-view.component';
import { StageDef, StageProgressComponent } from '../stage-progress/stage-progress.component';

type ServiceKind = 'diff' | 'collateral' | 'valuation' | 'insurance' | 'policyqa' | 'docgen';

interface ServiceMeta {
  key: string;
  label: string;
  kind: ServiceKind;
  path: string;
  // When set, the sidebar renders a router link to this route instead of an
  // inline panel — docgen is a whole multi-page mini-app (cases, templates,
  // approvals), not a single-result panel like the others.
  route?: string;
}

const SERVICE_CATALOG: ServiceMeta[] = [
  { key: 'docdiff', label: 'Document Reviewer', kind: 'diff', path: '/api/docdiff' },
  { key: 'collateral', label: 'Collateral Reviewer', kind: 'collateral', path: '/api/collateral' },
  { key: 'valuation', label: 'Valuation Review', kind: 'valuation', path: '/api/valuation' },
  { key: 'insurance', label: 'Insurance Review', kind: 'insurance', path: '/api/insurance' },
  { key: 'policy_qa', label: 'Policy Q&A', kind: 'policyqa', path: '/api/policyqa' },
  { key: 'docgen', label: 'Document Generation', kind: 'docgen', path: '/api/profiles', route: '/docgen' }
];

// Stage keys/order come straight from each engine's _emit_event() calls
// (engines/collateral.py, engines/valuation.py, engines/insurance.py) — a
// stage's "event" arriving means every earlier stage in this list is done.
// The final "done" stage (collateral/valuation emit it; insurance doesn't)
// is handled separately as a completion signal, not as a checklist step.
const COLLATERAL_STAGES: StageDef[] = [
  { key: 'extract_text', label: 'Extracting documents' },
  { key: 'extract_fields', label: 'Extracting fields' },
  { key: 'compare', label: 'Comparing fields' },
  { key: 'observations', label: 'Generating observations' }
];

const VALUATION_STAGES: StageDef[] = [
  { key: 'extract_text', label: 'Extracting report' },
  { key: 'extract_fields', label: 'Extracting fields' },
  { key: 'panel_check', label: 'Checking approved-valuer panel' }
];

const INSURANCE_STAGES: StageDef[] = [
  { key: 'extract', label: 'Extracting policy' },
  { key: 'structure', label: 'Structuring policy data' },
  { key: 'analyze', label: 'Analyzing against bank policy' }
];

interface ReviewProgress {
  stageKey: string | null;
  detail: string | null;
  complete: boolean;
}

function freshProgress(): ReviewProgress {
  return { stageKey: null, detail: null, complete: false };
}

interface DiffChange {
  id: number;
  type: string;
  before: string;
  after: string;
  possibleMissingSection?: boolean;
}

interface DiffSegment {
  op: 'equal' | 'delete' | 'insert';
  text: string;
  changeId: number | null;
}

interface DocumentDiffResult {
  render: 'html' | 'text';
  identical: boolean;
  similarity: number;
  summary: { insertions: number; deletions: number; replacements: number; changes: number };
  changes: DiffChange[];
  segments?: DiffSegment[];
  html?: string;
  missingPages?: number[];
}

// What docdiff-service actually returns (see engines/document_diff.py): no
// render/html/missingPages, and changes/segments lack the id/changeId
// linkage the older document-reviewer shape had. compareDocuments() below
// fills those in with safe defaults.
interface RawDocumentDiffResult {
  render?: 'html' | 'text';
  identical: boolean;
  similarity: number;
  summary: { insertions: number; deletions: number; replacements: number; changes: number };
  changes: { type: string; before: string; after: string }[];
  segments?: { op: 'equal' | 'delete' | 'insert'; text: string }[];
  html?: string;
  missingPages?: number[];
}

interface CollateralComparisonRow {
  field: string;
  label: string;
  legal_value: string | null;
  property_value: string | null;
  status: 'match' | 'mismatch' | 'missing';
}

interface CollateralResult {
  comparison: CollateralComparisonRow[];
  observations: string[];
  summary: { matches: number; mismatches: number; missing: number; fields: number };
}

interface PolicyQaStatus {
  has_own_index: boolean;
  bundled_available: boolean;
}

interface PolicyQaMessage {
  role: 'user' | 'assistant';
  content: string;
  sources?: string[];
  streaming?: boolean;
}

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink, JsonViewComponent, StageProgressComponent],
  templateUrl: './dashboard.component.html'
})
export class DashboardComponent {
  readonly collateralStages = COLLATERAL_STAGES;
  readonly valuationStages = VALUATION_STAGES;
  readonly insuranceStages = INSURANCE_STAGES;

  selected: ServiceMeta | null = null;

  originalFile: File | null = null;
  returnedFile: File | null = null;
  diffResult: DocumentDiffResult | null = null;
  diffError = '';
  comparingDiff = false;

  legalFile: File | null = null;
  propertyFile: File | null = null;
  collateralResult: CollateralResult | null = null;
  collateralError = '';
  comparingCollateral = false;
  collateralProgress: ReviewProgress = freshProgress();
  collateralStreamingText = '';

  valuationFile: File | null = null;
  valuationResult: unknown = null;
  valuationError = '';
  reviewingValuation = false;
  valuationProgress: ReviewProgress = freshProgress();

  insuranceFile: File | null = null;
  insuranceResult: unknown = null;
  insuranceError = '';
  reviewingInsurance = false;
  insuranceProgress: ReviewProgress = freshProgress();

  policyStatus: PolicyQaStatus | null = null;
  policyMessages: PolicyQaMessage[] = [];
  policyQuery = '';
  policyIngestFile: File | null = null;
  policyBusy = false;
  policyError = '';
  policyChatError = '';

  constructor(public session: SessionService, private http: HttpClient) {}

  get entitledServices(): ServiceMeta[] {
    const granted = this.session.session?.scopes ?? [];
    return SERVICE_CATALOG.filter((s) => granted.includes(s.key));
  }

  select(meta: ServiceMeta): void {
    this.selected = meta;
    this.diffResult = null;
    this.diffError = '';
    this.collateralResult = null;
    this.collateralError = '';
    this.collateralProgress = freshProgress();
    this.collateralStreamingText = '';
    this.valuationResult = null;
    this.valuationError = '';
    this.valuationProgress = freshProgress();
    this.insuranceResult = null;
    this.insuranceError = '';
    this.insuranceProgress = freshProgress();
    this.policyError = '';
    this.policyChatError = '';

    if (meta.kind === 'policyqa') {
      this.loadPolicyStatus();
    }
  }

  // --- document-reviewer (docdiff-service) ---

  onOriginalFile(event: Event): void {
    this.originalFile = (event.target as HTMLInputElement).files?.[0] ?? null;
  }

  onReturnedFile(event: Event): void {
    this.returnedFile = (event.target as HTMLInputElement).files?.[0] ?? null;
  }

  compareDocuments(): void {
    if (!this.originalFile || !this.returnedFile) return;
    this.diffError = '';
    this.comparingDiff = true;
    const form = new FormData();
    form.append('original', this.originalFile);
    form.append('returned', this.returnedFile);
    this.http
      .post<RawDocumentDiffResult>(`${KONG_BASE}/api/docdiff/compare`, form, { headers: this.session.authHeaders() })
      .subscribe({
        next: (res) => {
          // docdiff-service's engine doesn't emit render/html/missingPages, or
          // link changes[] to segments[] with a shared id — reconstruct that
          // linkage ourselves (see linkDocDiffChanges) so "jump to change"
          // actually scrolls to the right spot instead of being a no-op.
          const { changes, segments } = this.linkDocDiffChanges(res);
          this.diffResult = {
            identical: res.identical,
            similarity: res.similarity,
            summary: res.summary,
            html: res.html,
            missingPages: res.missingPages,
            render: res.render ?? (res.segments ? 'text' : 'html'),
            changes,
            segments
          };
          this.comparingDiff = false;
        },
        error: (err: HttpErrorResponse) => {
          this.diffError = err.error?.detail ?? 'comparison failed';
          this.comparingDiff = false;
        }
      });
  }

  // docdiff-service's engine (engines/document_diff.py) emits `changes` and
  // `segments` independently, with no shared id between them: `changes` is
  // the opcode list with whitespace-only spans suppressed; `segments` is the
  // full lossless token stream (whitespace-only spans kept). Both are walked
  // in the same left-to-right order by the engine, so we can regroup
  // `segments` into the same change groups (a run of `delete` segments
  // optionally followed by a run of `insert` segments), apply the same
  // whitespace-only suppression rule (both sides trim to empty — verified
  // against a live docdiff-service response), and zip the result 1:1 against
  // `changes`. If the counts don't line up the way that predicts, don't risk
  // mislabeling — fall back to unlinked segments instead.
  private linkDocDiffChanges(
    res: RawDocumentDiffResult
  ): { changes: DiffChange[]; segments: DiffSegment[] } {
    const rawSegments = res.segments ?? [];

    interface ChangeGroup {
      segmentIndices: number[];
      before: string;
      after: string;
    }

    const groups: ChangeGroup[] = [];
    let i = 0;
    while (i < rawSegments.length) {
      if (rawSegments[i].op === 'equal') {
        i++;
        continue;
      }
      const segmentIndices: number[] = [];
      let before = '';
      while (i < rawSegments.length && rawSegments[i].op === 'delete') {
        segmentIndices.push(i);
        before += rawSegments[i].text;
        i++;
      }
      let after = '';
      while (i < rawSegments.length && rawSegments[i].op === 'insert') {
        segmentIndices.push(i);
        after += rawSegments[i].text;
        i++;
      }
      groups.push({ segmentIndices, before, after });
    }

    const realGroups = groups.filter((g) => g.before.trim() !== '' || g.after.trim() !== '');

    const changeIdBySegmentIndex = new Map<number, number>();
    if (realGroups.length === res.changes.length) {
      realGroups.forEach((g, id) => g.segmentIndices.forEach((idx) => changeIdBySegmentIndex.set(idx, id)));
    }

    const changes = res.changes.map((c, id) => ({ id, type: c.type, before: c.before, after: c.after }));
    const segments = rawSegments.map((s, idx) => ({
      op: s.op,
      text: s.text,
      changeId: changeIdBySegmentIndex.get(idx) ?? null
    }));

    return { changes, segments };
  }

  jumpToChange(id: number): void {
    // text-mode anchors are a real Angular [id] binding (untouched by any
    // sanitizer); html-mode anchors are baked into raw HTML rendered via
    // [innerHTML], where Angular's sanitizer strips `id` but keeps `class`
    // verbatim — so that path is a class instead (see document-reviewer).
    const el =
      document.getElementById('doc-change-' + id) ?? document.querySelector('.doc-change-' + id);
    if (!el) return;
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    el.classList.add('flash');
    setTimeout(() => el.classList.remove('flash'), 1500);
  }

  // --- collateral-reviewer (collateral-service) ---

  onLegalFile(event: Event): void {
    this.legalFile = (event.target as HTMLInputElement).files?.[0] ?? null;
  }

  onPropertyFile(event: Event): void {
    this.propertyFile = (event.target as HTMLInputElement).files?.[0] ?? null;
  }

  compareCollateral(): void {
    if (!this.legalFile || !this.propertyFile) return;
    this.collateralError = '';
    this.comparingCollateral = true;
    this.collateralResult = null;
    this.collateralProgress = freshProgress();
    this.collateralStreamingText = '';

    const form = new FormData();
    form.append('legal', this.legalFile);
    form.append('property', this.propertyFile);

    this.consumeSse(`${KONG_BASE}/api/collateral/review/stream`, form, (eventType, data) => {
      if (eventType === 'event') {
        this.applyStageEvent(this.collateralProgress, data);
      } else if (eventType === 'content') {
        // Live LLM output during the "observations" stage — collateral is
        // the only one of these three pipelines with real token streaming
        // (engines/collateral.py's generate_observations); valuation and
        // insurance only ever emit stage events.
        try {
          this.collateralStreamingText += JSON.parse(data) as string;
        } catch {
          /* malformed chunk — skip it rather than corrupt the buffer */
        }
      } else if (eventType === 'result') {
        try {
          this.collateralResult = JSON.parse(data) as CollateralResult;
          this.collateralProgress.complete = true;
        } catch {
          this.collateralError = 'received an unreadable result';
        }
      } else if (eventType === 'error') {
        this.collateralError = this.parseSseError(data);
      }
    })
      .catch((err) => {
        this.collateralError = err instanceof Error ? err.message : 'comparison failed';
      })
      .finally(() => {
        this.comparingCollateral = false;
      });
  }

  // --- valuation-service ---

  onValuationFile(event: Event): void {
    this.valuationFile = (event.target as HTMLInputElement).files?.[0] ?? null;
  }

  reviewValuation(): void {
    if (!this.valuationFile) return;
    this.valuationError = '';
    this.reviewingValuation = true;
    this.valuationResult = null;
    this.valuationProgress = freshProgress();

    const form = new FormData();
    form.append('report', this.valuationFile);

    this.consumeSse(`${KONG_BASE}/api/valuation/review/stream`, form, (eventType, data) => {
      if (eventType === 'event') {
        this.applyStageEvent(this.valuationProgress, data);
      } else if (eventType === 'result') {
        try {
          this.valuationResult = JSON.parse(data);
          this.valuationProgress.complete = true;
        } catch {
          this.valuationError = 'received an unreadable result';
        }
      } else if (eventType === 'error') {
        this.valuationError = this.parseSseError(data);
      }
    })
      .catch((err) => {
        this.valuationError = err instanceof Error ? err.message : 'review failed';
      })
      .finally(() => {
        this.reviewingValuation = false;
      });
  }

  // --- insurance-service ---

  onInsuranceFile(event: Event): void {
    this.insuranceFile = (event.target as HTMLInputElement).files?.[0] ?? null;
  }

  reviewInsurance(): void {
    if (!this.insuranceFile) return;
    this.insuranceError = '';
    this.reviewingInsurance = true;
    this.insuranceResult = null;
    this.insuranceProgress = freshProgress();

    const form = new FormData();
    form.append('policy', this.insuranceFile);

    this.consumeSse(`${KONG_BASE}/api/insurance/review/stream`, form, (eventType, data) => {
      if (eventType === 'event') {
        this.applyStageEvent(this.insuranceProgress, data);
      } else if (eventType === 'result') {
        try {
          this.insuranceResult = JSON.parse(data);
          this.insuranceProgress.complete = true;
        } catch {
          this.insuranceError = 'received an unreadable result';
        }
      } else if (eventType === 'error') {
        this.insuranceError = this.parseSseError(data);
      }
    })
      .catch((err) => {
        this.insuranceError = err instanceof Error ? err.message : 'review failed';
      })
      .finally(() => {
        this.reviewingInsurance = false;
      });
  }

  // --- policyqa-service ---

  loadPolicyStatus(): void {
    this.http
      .get<PolicyQaStatus>(`${KONG_BASE}/api/policyqa/status`, { headers: this.session.authHeaders() })
      .subscribe({
        next: (res) => (this.policyStatus = res),
        error: (err: HttpErrorResponse) => {
          this.policyError = err.error?.detail ?? 'failed to load status';
        }
      });
  }

  onPolicyIngestFile(event: Event): void {
    this.policyIngestFile = (event.target as HTMLInputElement).files?.[0] ?? null;
  }

  ingestPolicy(): void {
    if (!this.policyIngestFile) return;
    this.policyError = '';
    this.policyBusy = true;
    const form = new FormData();
    form.append('file', this.policyIngestFile);
    this.http
      .post(`${KONG_BASE}/api/policyqa/ingest`, form, { headers: this.session.authHeaders() })
      .subscribe({
        next: () => {
          this.policyIngestFile = null;
          this.policyBusy = false;
          this.loadPolicyStatus();
        },
        error: (err: HttpErrorResponse) => {
          this.policyError = err.error?.detail ?? 'ingest failed';
          this.policyBusy = false;
        }
      });
  }

  deletePolicyIndex(): void {
    this.policyError = '';
    this.http
      .delete(`${KONG_BASE}/api/policyqa/index`, { headers: this.session.authHeaders() })
      .subscribe({
        next: () => this.loadPolicyStatus(),
        error: (err: HttpErrorResponse) => {
          this.policyError = err.error?.detail ?? 'delete failed';
        }
      });
  }

  async sendPolicyQuery(): Promise<void> {
    const query = this.policyQuery.trim();
    if (!query || this.policyBusy) return;
    this.policyChatError = '';
    this.policyBusy = true;

    const history = this.policyMessages.map((m) => ({ role: m.role, content: m.content }));
    this.policyMessages.push({ role: 'user', content: query });
    this.policyQuery = '';

    // Streaming placeholder: shows the typing-dots indicator until the first
    // token arrives, then grows in place as content events stream in.
    const assistantMsg: PolicyQaMessage = { role: 'assistant', content: '', streaming: true };
    this.policyMessages.push(assistantMsg);

    try {
      await this.consumeSse(`${KONG_BASE}/api/policyqa/chat/stream`, { query, history }, (eventType, data) => {
        if (eventType === 'content') {
          try {
            assistantMsg.content += JSON.parse(data) as string;
          } catch {
            /* malformed chunk — skip it rather than corrupt the message */
          }
        } else if (eventType === 'result') {
          try {
            const result = JSON.parse(data) as { answer: string; sources: string[] };
            assistantMsg.content = result.answer;
            assistantMsg.sources = result.sources;
          } catch {
            /* keep the accumulated content as-is */
          }
        } else if (eventType === 'error') {
          this.policyChatError = this.parseSseError(data);
        }
      });
    } catch (err) {
      this.policyChatError = err instanceof Error ? err.message : 'chat failed';
      if (!assistantMsg.content) {
        const idx = this.policyMessages.indexOf(assistantMsg);
        if (idx !== -1) this.policyMessages.splice(idx, 1);
      }
    } finally {
      assistantMsg.streaming = false;
      this.policyBusy = false;
    }
  }

  // --- shared SSE plumbing (collateral/valuation/insurance/policyqa) ---

  // Posts `body` (JSON object or FormData, for the file-upload endpoints) and
  // reads the response as an SSE stream per streaming.py's contract, calling
  // `onFrame(eventType, data)` for each complete "event: X\ndata: Y\n\n"
  // frame. Angular's HttpClient has no ergonomic incremental-read API, so
  // this uses the Fetch API directly (still zone.js-patched, so change
  // detection still runs after each awaited chunk).
  private async consumeSse(
    url: string,
    body: FormData | Record<string, unknown>,
    onFrame: (eventType: string, data: string) => void
  ): Promise<void> {
    const isFormData = body instanceof FormData;
    const response = await fetch(url, {
      method: 'POST',
      headers: isFormData
        ? this.session.authHeaders()
        : { 'Content-Type': 'application/json', ...this.session.authHeaders() },
      body: isFormData ? body : JSON.stringify(body)
    });

    if (!response.ok) {
      let detail = `request failed (${response.status})`;
      try {
        const errBody = await response.json();
        if (errBody?.detail) detail = errBody.detail;
      } catch {
        /* error body wasn't JSON — keep the generic status message */
      }
      throw new Error(detail);
    }
    if (!response.body) {
      throw new Error('empty response body');
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let frameEnd: number;
      while ((frameEnd = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, frameEnd);
        buffer = buffer.slice(frameEnd + 2);

        let eventType = 'message';
        let data = '';
        for (const line of frame.split('\n')) {
          if (line.startsWith('event:')) eventType = line.slice(6).trim();
          else if (line.startsWith('data:')) data += line.slice(5).trim();
        }
        if (data) onFrame(eventType, data);
      }
    }
  }

  // Applies one "event" SSE frame's JSON payload (`{"stage": "...", ...}`,
  // see engines/*.py's _emit_event) to a review pipeline's progress state.
  // "done" isn't a checklist step (see COLLATERAL_STAGES etc.) — it just
  // means the pipeline is finishing up, ahead of the final `result` frame.
  private applyStageEvent(progress: ReviewProgress, rawData: string): void {
    let payload: Record<string, unknown>;
    try {
      payload = JSON.parse(rawData) as Record<string, unknown>;
    } catch {
      return;
    }
    const stage = payload['stage'];
    if (typeof stage !== 'string') return;

    if (stage === 'done') {
      progress.complete = true;
      return;
    }
    progress.stageKey = stage;
    const rest = Object.entries(payload).filter(([k]) => k !== 'stage' && k !== 'status');
    progress.detail = rest.length ? rest.map(([k, v]) => `${k}: ${v}`).join(', ') : null;
  }

  private parseSseError(rawData: string): string {
    try {
      return (JSON.parse(rawData) as { error: string }).error;
    } catch {
      return 'request failed';
    }
  }
}
