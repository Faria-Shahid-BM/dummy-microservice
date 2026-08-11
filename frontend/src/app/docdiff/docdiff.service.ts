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

export interface DocumentDiffResult {
  render: 'html' | 'text';
  identical: boolean;
  similarity: number;
  summary: { insertions: number; deletions: number; replacements: number; changes: number };
  changes: DiffChange[];
  segments?: DiffSegment[];
  html?: string;
  missingPages?: number[];
}

// What engines/document_diff.py actually returns: no render/html/
// missingPages, and changes/segments lack the id/changeId linkage the
// richer DocumentDiffResult shape above has — see linkDocDiffChanges() in
// case-detail.component.ts, which reconstructs it client-side.
export interface RawDocumentDiffResult {
  render?: 'html' | 'text';
  identical: boolean;
  similarity: number;
  summary: { insertions: number; deletions: number; replacements: number; changes: number };
  changes: { type: string; before: string; after: string }[];
  segments?: { op: 'equal' | 'delete' | 'insert'; text: string }[];
  html?: string;
  missingPages?: number[];
}

@Injectable({ providedIn: 'root' })
export class DocdiffService extends CaseService<RawDocumentDiffResult> {
  protected readonly apiBase = `${KONG_BASE}/api/docdiff`;
  readonly routeBase = '/docdiff';
  readonly label = 'Document Reviewer';
  readonly slots: SlotDef[] = [
    { key: 'original', label: 'Original document (.docx or .pdf)', accept: '.docx,.pdf' },
    { key: 'returned', label: 'Returned document (.docx or .pdf)', accept: '.docx,.pdf' }
  ];
  override readonly itemNoun = 'pair';
}
