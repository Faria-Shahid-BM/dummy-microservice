import { Injectable } from '@angular/core';
import { KONG_BASE } from '../session.service';
import { CaseService, SlotDef } from '../shared/case.service';

export interface DiffChange {
  id: number;
  type: string;
  before: string;
  after: string;
  possibleMissingSection?: boolean;
}

export interface DiffSegment {
  op: 'equal' | 'delete' | 'insert';
  text: string;
  changeId: number | null;
}

// Images never reach the comparison: docx_to_html() drops them, because the
// returned copy is signed and the generated original never is. The counts come
// back so the view can say an image was set aside rather than let "identical"
// be read as "the signature was checked". Optional — results stored before
// this existed have no `media`.
export interface DiffMedia {
  original: number;
  returned: number;
  compared: boolean;
}

export interface DocumentDiffResult {
  render: 'html' | 'text';
  identical: boolean;
  similarity: number;
  summary: { insertions: number; deletions: number; replacements: number; changes: number };
  changes: DiffChange[];
  segments?: DiffSegment[];
  html?: string;
  missingPages?: number[];
  media?: DiffMedia;
}

// What the backend actually returns. Uploads are .docx-only now, so a fresh
// comparison always comes from engines/document_diff_html.py: `render: 'html'`
// plus a ready-made `html` redline and `possibleMissingSection` flags, and no
// `segments`. The `segments`/`text` shape is still handled because results
// stored before that change (engines/document_diff.py, back when PDFs were
// accepted) are persisted on their cases and must keep rendering; those carry
// no id/changeId linkage, which diffFor() in case-detail.component.ts
// reconstructs client-side.
export interface RawDocumentDiffResult {
  render?: 'html' | 'text';
  identical: boolean;
  similarity: number;
  summary: { insertions: number; deletions: number; replacements: number; changes: number };
  changes: { type: string; before: string; after: string; possibleMissingSection?: boolean }[];
  segments?: { op: 'equal' | 'delete' | 'insert'; text: string }[];
  html?: string;
  missingPages?: number[];
  media?: DiffMedia;
}

@Injectable({ providedIn: 'root' })
export class DocdiffService extends CaseService<RawDocumentDiffResult> {
  protected readonly apiBase = `${KONG_BASE}/api/docdiff`;
  readonly routeBase = '/docdiff';
  readonly label = 'Document Reviewer';
  readonly slots: SlotDef[] = [
    // .docx only: the comparison is a structural redline of the two documents'
    // real headings/tables/lists, which a PDF carries no usable version of.
    { key: 'original', label: 'Original document (.docx)', accept: '.docx' },
    { key: 'returned', label: 'Returned document (.docx)', accept: '.docx' }
  ];
  // No tab-per-pair UI here (see allowExtraPairs) — 'comparison' reads right
  // in the one remaining place this shows up: "Review this comparison again."
  override readonly itemNoun = 'comparison';
  // Every case here mirrors a document generated in Document Generator —
  // there's no manual "+ Add case", and `original` is never user-editable.
  override readonly hideAddCase = true;
  override readonly managedSlots = ['original'];
  override readonly allowExtraPairs = false;
  override readonly showReviewedAt = true;
}
