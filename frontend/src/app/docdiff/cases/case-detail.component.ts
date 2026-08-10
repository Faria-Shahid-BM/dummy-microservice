import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { CaseDetail } from '../../shared/case.service';
import { DiffChange, DiffSegment, DocdiffService, DocumentDiffResult, RawDocumentDiffResult } from '../docdiff.service';
import { parseSseError } from '../../sse.util';

@Component({
  selector: 'app-docdiff-case-detail',
  standalone: true,
  imports: [CommonModule, RouterLink],
  templateUrl: './case-detail.component.html'
})
export class CaseDetailComponent implements OnInit {
  caseId = '';
  case: CaseDetail<RawDocumentDiffResult> | null = null;
  diffResult: DocumentDiffResult | null = null;
  loading = false;
  error = '';

  originalFile: File | null = null;
  returnedFile: File | null = null;
  uploadingOriginal = false;
  uploadingReturned = false;
  uploadError = '';

  analyzing = false;
  analyzeError = '';

  constructor(private route: ActivatedRoute, private docdiff: DocdiffService) {}

  ngOnInit(): void {
    this.caseId = this.route.snapshot.paramMap.get('caseId') ?? '';
    this.loadCase();
  }

  loadCase(): void {
    if (!this.caseId) return;
    this.error = '';
    this.loading = true;
    this.docdiff.getCase(this.caseId).subscribe({
      next: (c) => {
        this.case = c;
        this.diffResult = c.result ? this.toDiffResult(c.result) : null;
        this.loading = false;
      },
      error: (err: HttpErrorResponse) => {
        this.error = err.error?.detail ?? 'failed to load case';
        this.loading = false;
      }
    });
  }

  get canAnalyze(): boolean {
    return this.case?.status === 'ready' || this.case?.status === 'done' || this.case?.status === 'failed';
  }

  onOriginalFile(event: Event): void {
    this.originalFile = (event.target as HTMLInputElement).files?.[0] ?? null;
  }

  onReturnedFile(event: Event): void {
    this.returnedFile = (event.target as HTMLInputElement).files?.[0] ?? null;
  }

  uploadOriginal(): void {
    if (!this.originalFile) return;
    this.uploadError = '';
    this.uploadingOriginal = true;
    this.docdiff.uploadSlot(this.caseId, 'original', this.originalFile).subscribe({
      next: (c) => {
        this.case = c;
        this.originalFile = null;
        this.uploadingOriginal = false;
      },
      error: (err: HttpErrorResponse) => {
        this.uploadError = err.error?.detail ?? 'upload failed';
        this.uploadingOriginal = false;
      }
    });
  }

  uploadReturned(): void {
    if (!this.returnedFile) return;
    this.uploadError = '';
    this.uploadingReturned = true;
    this.docdiff.uploadSlot(this.caseId, 'returned', this.returnedFile).subscribe({
      next: (c) => {
        this.case = c;
        this.returnedFile = null;
        this.uploadingReturned = false;
      },
      error: (err: HttpErrorResponse) => {
        this.uploadError = err.error?.detail ?? 'upload failed';
        this.uploadingReturned = false;
      }
    });
  }

  analyze(): void {
    if (!this.canAnalyze) return;
    this.analyzeError = '';
    this.analyzing = true;

    this.docdiff
      .analyzeCase(this.caseId, (eventType, data) => {
        if (eventType === 'result') {
          this.loadCase(); // pick up the now-persisted status + result
        } else if (eventType === 'error') {
          this.analyzeError = parseSseError(data);
        }
      })
      .catch((err) => {
        this.analyzeError = err instanceof Error ? err.message : 'comparison failed';
      })
      .finally(() => {
        this.analyzing = false;
        if (!this.case || this.case.status === 'analyzing') this.loadCase();
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
  private toDiffResult(res: RawDocumentDiffResult): DocumentDiffResult {
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

    const changes: DiffChange[] = res.changes.map((c, id) => ({ id, type: c.type, before: c.before, after: c.after }));
    const segments: DiffSegment[] = rawSegments.map((s, idx) => ({
      op: s.op,
      text: s.text,
      changeId: changeIdBySegmentIndex.get(idx) ?? null
    }));

    return {
      identical: res.identical,
      similarity: res.similarity,
      summary: res.summary,
      html: res.html,
      missingPages: res.missingPages,
      render: res.render ?? (res.segments ? 'text' : 'html'),
      changes,
      segments
    };
  }

  jumpToChange(id: number): void {
    // text-mode anchors are a real Angular [id] binding (untouched by any
    // sanitizer); html-mode anchors are baked into raw HTML rendered via
    // [innerHTML], where Angular's sanitizer strips `id` but keeps `class`
    // verbatim — so that path is a class instead (see document-reviewer).
    const el = document.getElementById('doc-change-' + id) ?? document.querySelector('.doc-change-' + id);
    if (!el) return;
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    el.classList.add('flash');
    setTimeout(() => el.classList.remove('flash'), 1500);
  }
}
