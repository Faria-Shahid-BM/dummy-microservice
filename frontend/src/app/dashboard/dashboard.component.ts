import { CommonModule } from '@angular/common';
import { Component } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { SessionService, KONG_BASE } from '../session.service';
import { JsonViewComponent } from '../json-view/json-view.component';
import { StageDef } from '../stage-progress/stage-progress.component';
import { ReviewBatchComponent, SlotDef } from '../review-batch/review-batch.component';
import { ReviewBatch, parseSseError } from '../review-batch/review-batch';

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

// Upload slots per reviewer — the `key`s are the multipart field names the
// services expect (see each service's main.py _SLOTS).
const COLLATERAL_SLOTS: SlotDef[] = [
  { key: 'legal', label: 'Legal opinion (.docx or .pdf)', accept: '.docx,.pdf' },
  { key: 'property', label: 'Property / title document (.docx or .pdf)', accept: '.docx,.pdf' }
];

const VALUATION_SLOTS: SlotDef[] = [
  { key: 'report', label: 'Valuation report (.docx or .pdf)', accept: '.docx,.pdf' }
];

const INSURANCE_SLOTS: SlotDef[] = [
  { key: 'policy', label: 'Insurance policy (.docx or .pdf)', accept: '.docx,.pdf' }
];

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

// GET /api/insurance/policy — which bank policy the reviews are graded against.
interface BankPolicyStatus {
  has_own_policy: boolean;
  file_name: string | null;
  chars: number | null;
  uploaded_at: string | null;
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
  imports: [CommonModule, FormsModule, RouterLink, JsonViewComponent, ReviewBatchComponent],
  templateUrl: './dashboard.component.html'
})
export class DashboardComponent {
  readonly collateralStages = COLLATERAL_STAGES;
  readonly valuationStages = VALUATION_STAGES;
  readonly insuranceStages = INSURANCE_STAGES;

  readonly collateralSlots = COLLATERAL_SLOTS;
  readonly valuationSlots = VALUATION_SLOTS;
  readonly insuranceSlots = INSURANCE_SLOTS;

  selected: ServiceMeta | null = null;

  originalFile: File | null = null;
  returnedFile: File | null = null;
  diffResult: DocumentDiffResult | null = null;
  diffError = '';
  comparingDiff = false;

  // The three streaming reviewers all run N independent items per submission,
  // each with its own result tab — see review-batch.ts / batch.py.
  readonly collateralBatch: ReviewBatch<CollateralResult>;
  readonly valuationBatch: ReviewBatch<unknown>;
  readonly insuranceBatch: ReviewBatch<unknown>;

  // The bank policy insurance reviews are graded against — uploaded per user,
  // falling back to the engine's bundled policy.txt when absent.
  bankPolicy: BankPolicyStatus | null = null;
  bankPolicyBusy = false;
  bankPolicyError = '';

  policyStatus: PolicyQaStatus | null = null;
  policyMessages: PolicyQaMessage[] = [];
  policyQuery = '';
  policyIngestFile: File | null = null;
  policyBusy = false;
  policyError = '';
  policyChatError = '';

  constructor(public session: SessionService, private http: HttpClient) {
    const sse = (url: string, body: FormData, onFrame: (t: string, d: string) => void) =>
      this.consumeSse(url, body, onFrame);
    const batchUrl = (service: string) => `${KONG_BASE}/api/${service}/review/batch/stream`;

    this.collateralBatch = new ReviewBatch<CollateralResult>(
      COLLATERAL_SLOTS.map((s) => s.key), batchUrl('collateral'), sse);
    this.valuationBatch = new ReviewBatch<unknown>(
      VALUATION_SLOTS.map((s) => s.key), batchUrl('valuation'), sse);
    this.insuranceBatch = new ReviewBatch<unknown>(
      INSURANCE_SLOTS.map((s) => s.key), batchUrl('insurance'), sse);
  }

  get entitledServices(): ServiceMeta[] {
    const granted = this.session.session?.scopes ?? [];
    return SERVICE_CATALOG.filter((s) =>
      // docgen is entitled by either of its scopes — a checker holds
      // "docgen_check", which on its own must still show the entry.
      s.key === 'docgen' ? this.session.canUseDocgen : granted.includes(s.key)
    );
  }

  select(meta: ServiceMeta): void {
    this.selected = meta;
    this.diffResult = null;
    this.diffError = '';
    this.collateralBatch.reset();
    this.valuationBatch.reset();
    this.insuranceBatch.reset();
    this.policyError = '';
    this.policyChatError = '';
    this.bankPolicyError = '';

    if (meta.kind === 'policyqa') {
      this.loadPolicyStatus();
    }
    if (meta.kind === 'insurance') {
      this.loadBankPolicy();
    }
  }

  // --- insurance bank policy (insurance-service /policy) ---

  loadBankPolicy(): void {
    this.http
      .get<BankPolicyStatus>(`${KONG_BASE}/api/insurance/policy`, { headers: this.session.authHeaders() })
      .subscribe({
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
    const form = new FormData();
    form.append('file', file);
    this.http
      .post(`${KONG_BASE}/api/insurance/policy`, form, { headers: this.session.authHeaders() })
      .subscribe({
        next: () => {
          this.bankPolicyBusy = false;
          this.loadBankPolicy();
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
    this.http
      .delete(`${KONG_BASE}/api/insurance/policy`, { headers: this.session.authHeaders() })
      .subscribe({
        next: () => {
          this.bankPolicyBusy = false;
          this.loadBankPolicy();
        },
        error: (err: HttpErrorResponse) => {
          this.bankPolicyError = err.error?.detail ?? 'delete failed';
          this.bankPolicyBusy = false;
        }
      });
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
          this.policyChatError = parseSseError(data);
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
}
