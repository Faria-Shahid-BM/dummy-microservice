import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { KONG_BASE } from '../session.service';

// Shapes verified directly against docgen-service/app/** — see the plan's
// "Corrections to DOCGEN_API.md" section for the gaps the shipped doc left
// out or got subtly wrong (active_jobs has two different shapes depending
// on endpoint; provenance's applied/unfilled_fields are lists while the
// same-named fields on the documents list are counts; etc).

export interface DocgenProfile {
  id: string;
  name: string;
  description: string;
  is_default: boolean;
  created_at: string;
  role: string | null;
}

export type DocgenJobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';

export interface DocgenJob {
  id: string;
  kind: string;
  key: string;
  profile_id: string;
  subject_id: string;
  status: DocgenJobStatus;
  created_by: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  result: unknown;
}

export interface CaseActiveJob {
  id: string;
  kind: string;
  key: string;
  status: DocgenJobStatus;
}

export interface DocgenCase {
  id: string;
  name: string;
  status: string; // new|input|extracted|analyzed|selected|generating|generated
  input_file_name: string | null;
  created_at: string;
  has_input: boolean;
  has_case_text: boolean;
  has_analysis: boolean;
  has_selected: boolean;
  generated_count: number;
}

// Only the detail endpoint (GET .../cases/{cid}) adds this — list/create do not.
export interface DocgenCaseDetail extends DocgenCase {
  active_jobs: CaseActiveJob[];
}

export interface TextContent {
  content: string;
}

// The parsed shape of selected_docs.json (GET/PUT .../selected's `content`
// string) — see docgen-service/app/engines/docgen/prompts/selector.md for
// the schema the selector LLM is prompted to produce; case-detail renders
// this as a table instead of the raw JSON text.
export interface SelectedDocument {
  template_name: string;
  count: number;
  evidence: string;
  entities?: string[];
}

export interface AmbiguousDocument {
  template_name: string;
  reason: string;
}

export interface SelectedDocsFile {
  case_summary?: string;
  selected_documents: SelectedDocument[];
  ambiguous_documents?: AmbiguousDocument[];
}

export interface FillResponseJob {
  task_key: string;
  job_id: string;
  status: string;
}

export interface FillSkipped {
  task_key: string;
  reason: string;
}

export interface FillResponse {
  jobs: FillResponseJob[];
  skipped: FillSkipped[];
}

// Different shape from CaseActiveJob (job_id, not id) — the documents-list
// endpoint's active_jobs is its own thing, not reused from the case detail.
export interface DocumentActiveJob {
  job_id: string;
  task_key: string;
  status: DocgenJobStatus;
  created_at: string;
}

export interface GeneratedDocument {
  id: string;
  task_key: string;
  template_name: string;
  instance_label: string;
  file_name: string;
  applied_ops: number;
  failed_ops: number;
  unfilled_fields: number;
  needs_attention: boolean;
  approval_state: string; // draft|pending|approved|rejected
  approval_id: string | null;
  approval_comment: string;
  created_at: string;
}

export interface DocumentsResponse {
  documents: GeneratedDocument[];
  active_jobs: DocumentActiveJob[];
}

// Deliberately distinct from GeneratedDocument's applied_ops/failed_ops/
// unfilled_fields (counts) — provenance uses the same field names for lists
// of op objects. Conflating the two interfaces would be a real bug.
export interface DocumentProvenance {
  template: string;
  instance: string | null;
  entity_scope: string | null;
  applied: Record<string, unknown>[];
  failed: Record<string, unknown>[];
  unfilled_fields: unknown[];
  file_name: string;
}

export interface SubmitDocumentResponse {
  ok: true;
  approval_id: string;
  state: string;
}

export interface DocgenTemplate {
  id: string;
  name: string;
  language: 'en' | 'ar' | 'bilingual';
  status: string; // active|archived
  created_at: string;
  current_version_no: number | null;
  version_count: number;
}

export interface TemplateVersion {
  id: string;
  version_no: number;
  file_name: string;
  note: string;
  has_descriptor: boolean;
  is_current: boolean;
  approval_state: string | null;
  created_at: string;
}

// Only the detail endpoint (GET .../templates/{tid}) includes versions[].
export interface TemplateDetail extends DocgenTemplate {
  versions: TemplateVersion[];
}

export interface ApprovalSubject {
  name: string;
  link?: string;
}

export interface Approval {
  id: string;
  profile_id: string;
  subject_type: string; // template_version|generated_document
  subject_id: string;
  subject: ApprovalSubject;
  state: string; // draft|pending|approved|rejected
  maker: string;
  maker_id: string;
  checker: string | null;
  comment: string;
  submitted_at: string | null;
  decided_at: string | null;
}

export interface DocgenNotification {
  id: string;
  ts: string;
  type: string;
  title: string;
  body: string;
  link: string | null;
  read: boolean;
}

export interface NotificationsResponse {
  unread_count: number;
  notifications: DocgenNotification[];
}

@Injectable({ providedIn: 'root' })
export class DocgenService {
  // Set once by DocgenShellComponent on init; read directly by every child
  // route component (same "plain property on a root-provided singleton"
  // pattern SessionService already uses for `session` — no need for a
  // resolver or RxJS state store for a value this simple). DOCGEN_API.md:
  // only one non-default "Workspace" profile is seeded, so there's no
  // profile-switcher UI — this is just whichever non-default profile (or
  // the only profile) came back first.
  activeProfile: DocgenProfile | null = null;

  constructor(private http: HttpClient) {}

  // --- profiles ---

  listProfiles(): Observable<{ profiles: DocgenProfile[] }> {
    return this.http.get<{ profiles: DocgenProfile[] }>(`${KONG_BASE}/api/profiles`);
  }

  // --- cases ---

  listCases(profileId: string): Observable<{ cases: DocgenCase[] }> {
    return this.http.get<{ cases: DocgenCase[] }>(`${KONG_BASE}/api/profiles/${profileId}/cases`);
  }

  createCase(profileId: string, name: string): Observable<DocgenCase> {
    return this.http.post<DocgenCase>(`${KONG_BASE}/api/profiles/${profileId}/cases`, { name });
  }

  getCase(profileId: string, caseId: string): Observable<DocgenCaseDetail> {
    return this.http.get<DocgenCaseDetail>(`${KONG_BASE}/api/profiles/${profileId}/cases/${caseId}`);
  }

  uploadCaseInput(profileId: string, caseId: string, file: File): Observable<DocgenCase> {
    const form = new FormData();
    form.append('file', file);
    return this.http.post<DocgenCase>(`${KONG_BASE}/api/profiles/${profileId}/cases/${caseId}/input`, form);
  }

  runExtract(profileId: string, caseId: string): Observable<DocgenJob> {
    return this.http.post<DocgenJob>(`${KONG_BASE}/api/profiles/${profileId}/cases/${caseId}/extract`, {});
  }

  getCaseText(profileId: string, caseId: string): Observable<TextContent> {
    return this.http.get<TextContent>(`${KONG_BASE}/api/profiles/${profileId}/cases/${caseId}/case-text`);
  }

  putCaseText(profileId: string, caseId: string, content: string): Observable<TextContent> {
    return this.http.put<TextContent>(`${KONG_BASE}/api/profiles/${profileId}/cases/${caseId}/case-text`, {
      content
    });
  }

  runAnalyze(profileId: string, caseId: string): Observable<DocgenJob> {
    return this.http.post<DocgenJob>(`${KONG_BASE}/api/profiles/${profileId}/cases/${caseId}/analyze`, {});
  }

  getAnalysis(profileId: string, caseId: string): Observable<TextContent> {
    return this.http.get<TextContent>(`${KONG_BASE}/api/profiles/${profileId}/cases/${caseId}/analysis`);
  }

  putAnalysis(profileId: string, caseId: string, content: string): Observable<TextContent> {
    return this.http.put<TextContent>(`${KONG_BASE}/api/profiles/${profileId}/cases/${caseId}/analysis`, {
      content
    });
  }

  runSelect(profileId: string, caseId: string): Observable<DocgenJob> {
    return this.http.post<DocgenJob>(`${KONG_BASE}/api/profiles/${profileId}/cases/${caseId}/select`, {});
  }

  getSelected(profileId: string, caseId: string): Observable<TextContent> {
    return this.http.get<TextContent>(`${KONG_BASE}/api/profiles/${profileId}/cases/${caseId}/selected`);
  }

  putSelected(profileId: string, caseId: string, content: string): Observable<TextContent> {
    return this.http.put<TextContent>(`${KONG_BASE}/api/profiles/${profileId}/cases/${caseId}/selected`, {
      content
    });
  }

  runFill(profileId: string, caseId: string, tasks?: string[]): Observable<FillResponse> {
    return this.http.post<FillResponse>(
      `${KONG_BASE}/api/profiles/${profileId}/cases/${caseId}/fill`,
      tasks ? { tasks } : {}
    );
  }

  // --- generated documents ---

  listDocuments(profileId: string, caseId: string): Observable<DocumentsResponse> {
    return this.http.get<DocumentsResponse>(`${KONG_BASE}/api/profiles/${profileId}/cases/${caseId}/documents`);
  }

  submitDocument(profileId: string, caseId: string, docId: string): Observable<SubmitDocumentResponse> {
    return this.http.post<SubmitDocumentResponse>(
      `${KONG_BASE}/api/profiles/${profileId}/cases/${caseId}/documents/${docId}/submit`,
      {}
    );
  }

  documentDownloadUrl(profileId: string, caseId: string, docId: string): string {
    return `${KONG_BASE}/api/profiles/${profileId}/cases/${caseId}/documents/${docId}/download`;
  }

  getProvenance(profileId: string, caseId: string, docId: string): Observable<DocumentProvenance> {
    return this.http.get<DocumentProvenance>(
      `${KONG_BASE}/api/profiles/${profileId}/cases/${caseId}/documents/${docId}/provenance`
    );
  }

  downloadAllUrl(profileId: string, caseId: string, approvedOnly = true): string {
    return `${KONG_BASE}/api/profiles/${profileId}/cases/${caseId}/documents/download-all?approved_only=${approvedOnly}`;
  }

  // --- template library ---

  listTemplates(profileId: string): Observable<{ templates: DocgenTemplate[] }> {
    return this.http.get<{ templates: DocgenTemplate[] }>(`${KONG_BASE}/api/profiles/${profileId}/templates`);
  }

  createTemplate(
    profileId: string,
    file: File,
    name: string,
    language: 'en' | 'ar' | 'bilingual',
    note: string
  ): Observable<TemplateDetail> {
    const form = new FormData();
    form.append('file', file);
    form.append('name', name);
    form.append('language', language);
    form.append('note', note);
    return this.http.post<TemplateDetail>(`${KONG_BASE}/api/profiles/${profileId}/templates`, form);
  }

  getTemplate(profileId: string, templateId: string): Observable<TemplateDetail> {
    return this.http.get<TemplateDetail>(`${KONG_BASE}/api/profiles/${profileId}/templates/${templateId}`);
  }

  uploadTemplateVersion(
    profileId: string,
    templateId: string,
    file: File,
    note: string
  ): Observable<TemplateVersion> {
    const form = new FormData();
    form.append('file', file);
    form.append('note', note);
    return this.http.post<TemplateVersion>(
      `${KONG_BASE}/api/profiles/${profileId}/templates/${templateId}/versions`,
      form
    );
  }

  analyzeTemplateVersion(profileId: string, templateId: string, versionId: string): Observable<DocgenJob> {
    return this.http.post<DocgenJob>(
      `${KONG_BASE}/api/profiles/${profileId}/templates/${templateId}/versions/${versionId}/analyze`,
      {}
    );
  }

  getDescriptor(profileId: string, templateId: string, versionId: string): Observable<{ descriptor: string }> {
    return this.http.get<{ descriptor: string }>(
      `${KONG_BASE}/api/profiles/${profileId}/templates/${templateId}/versions/${versionId}/descriptor`
    );
  }

  putDescriptor(
    profileId: string,
    templateId: string,
    versionId: string,
    descriptor: string
  ): Observable<{ descriptor: string }> {
    return this.http.put<{ descriptor: string }>(
      `${KONG_BASE}/api/profiles/${profileId}/templates/${templateId}/versions/${versionId}/descriptor`,
      { descriptor }
    );
  }

  templateVersionFileUrl(profileId: string, templateId: string, versionId: string): string {
    return `${KONG_BASE}/api/profiles/${profileId}/templates/${templateId}/versions/${versionId}/file`;
  }

  // --- approvals ---

  listApprovals(
    profileId: string,
    state?: string,
    subjectType?: string
  ): Observable<{ approvals: Approval[] }> {
    const params: Record<string, string> = {};
    if (state) params['state'] = state;
    if (subjectType) params['subject_type'] = subjectType;
    return this.http.get<{ approvals: Approval[] }>(`${KONG_BASE}/api/profiles/${profileId}/approvals`, {
      params
    });
  }

  submitApproval(approvalId: string): Observable<Approval> {
    return this.http.post<Approval>(`${KONG_BASE}/api/approvals/${approvalId}/submit`, {});
  }

  decideApproval(approvalId: string, approve: boolean, comment: string): Observable<Approval> {
    return this.http.post<Approval>(`${KONG_BASE}/api/approvals/${approvalId}/decide`, { approve, comment });
  }

  // --- jobs (polling) ---

  getJob(jobId: string): Observable<DocgenJob> {
    return this.http.get<DocgenJob>(`${KONG_BASE}/api/jobs/${jobId}`);
  }

  // --- notifications ---

  listNotifications(unreadOnly = false): Observable<NotificationsResponse> {
    return this.http.get<NotificationsResponse>(`${KONG_BASE}/api/notifications`, {
      params: { unread: String(unreadOnly) }
    });
  }

  markNotificationsRead(ids?: string[]): Observable<{ ok: true }> {
    return this.http.post<{ ok: true }>(`${KONG_BASE}/api/notifications/read`, ids ? { ids } : {});
  }

  // --- authenticated file download ---

  // Downloads (documents, download-all zip, template version files) sit
  // behind the same JWT as everything else, carried automatically by the
  // httpOnly session cookie (see session.service.ts) — no header to attach
  // ourselves. Still fetched as a blob rather than a plain <a href> so
  // failures (expired session, 404) surface as a real error instead of the
  // browser silently rendering Kong/FastAPI's error JSON as a "download".
  async downloadFile(url: string, suggestedName?: string): Promise<void> {
    const response = await fetch(url);
    if (!response.ok) {
      let detail = `download failed (${response.status})`;
      try {
        const body = await response.json();
        if (body?.detail) detail = body.detail;
      } catch {
        /* error body wasn't JSON — keep the generic status message */
      }
      throw new Error(detail);
    }
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = objectUrl;
    a.download = suggestedName ?? '';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(objectUrl);
  }
}
