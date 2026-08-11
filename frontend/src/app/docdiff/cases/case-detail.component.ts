import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { CaseDetail } from '../../shared/case.service';
import { CasePairsComponent } from '../../shared/case-pairs.component';
import { PairRun } from '../../shared/pair-run';
import { DiffChange, DiffSegment, DocdiffService, DocumentDiffResult, RawDocumentDiffResult } from '../docdiff.service';

@Component({
  selector: 'app-docdiff-case-detail',
  standalone: true,
  imports: [CommonModule, RouterLink, CasePairsComponent],
  templateUrl: './case-detail.component.html'
})
export class CaseDetailComponent implements OnInit {
  caseId = '';
  case: CaseDetail<RawDocumentDiffResult> | null = null;
  // engines/document_diff.py's shape -> the richer one the redline view needs,
  // memoized per raw result so the template can call diffFor() freely (change
  // detection runs it often, the conversion is not free).
  private readonly diffCache = new WeakMap<object, DocumentDiffResult>();
  loading = false;
  error = '';


  analyzing = false;
  analyzeError = '';
  /** Which pair is running, how far along, and which tab is on screen. */
  readonly run = new PairRun();

  constructor(private route: ActivatedRoute, public docdiff: DocdiffService) {}

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
        this.loading = false;
      },
      error: (err: HttpErrorResponse) => {
        this.error = err.error?.detail ?? 'failed to load case';
        this.loading = false;
      }
    });
  }


  /** Pairs with no result yet — what the main button will run. */
  get pendingPairs(): number {
    return (this.case?.pairs ?? []).filter((p) => p.result == null).length;
  }

  /** Says what pressing it will actually do, so "Compare" never means a re-run. */
  get analyzeLabel(): string {
    if (this.analyzing) return 'Comparing…';
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
    // One tab per pair on the case; the server analyzes them in the same order.
    this.run.start(this.case.pairs.length);

    this.docdiff
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
  diffFor(raw: RawDocumentDiffResult): DocumentDiffResult {
    let converted = this.diffCache.get(raw as object);
    if (!converted) {
      converted = this.toDiffResult(raw);
      this.diffCache.set(raw as object, converted);
    }
    return converted;
  }

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
