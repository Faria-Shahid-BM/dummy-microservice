import { CommonModule } from '@angular/common';
import { Component, OnDestroy, OnInit } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { CaseDetail } from '../../shared/case.service';
import { CasePairsComponent } from '../../shared/case-pairs/case-pairs.component';
import { PairRun } from '../../shared/pair-run';
import { CaseWatch } from '../../shared/case-watch';
import { DiffChange, DiffSegment, DocdiffService, DocumentDiffResult, RawDocumentDiffResult } from '../docdiff.service';

@Component({
  selector: 'app-docdiff-case-detail',
  standalone: true,
  imports: [CommonModule, RouterLink, CasePairsComponent],
  templateUrl: './case-detail.component.html'
})
export class CaseDetailComponent implements OnInit, OnDestroy {
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
  /** Follows a review still running on the server when this page isn't the one streaming it. */
  private readonly watch = new CaseWatch();

  constructor(private route: ActivatedRoute, public docdiff: DocdiffService) {}

  ngOnInit(): void {
    this.caseId = this.route.snapshot.paramMap.get('caseId') ?? '';
    this.loadCase();
  }

  ngOnDestroy(): void {
    this.watch.stop();
  }

  /** A review is running that this page didn't start (we left and came back). */
  get serverBusy(): boolean {
    return this.case?.status === 'analyzing' && !this.analyzing;
  }

  loadCase(): void {
    if (!this.caseId) return;
    this.error = '';
    this.loading = true;
    this.docdiff.getCase(this.caseId).subscribe({
      next: (c) => {
        this.case = c;
        this.loading = false;
        this.watch.sync(c.status, this.analyzing, () => this.loadCase());
        // A run this page didn't start has no stream here, so the stage
        // checklist comes from the server's own snapshot instead of frames.
        if (this.serverBusy) this.watch.followProgress(this.docdiff, this.caseId, this.run, c.pairs?.length ?? 1);
        else if (!this.analyzing) this.run.stopAdopted();
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
    if (this.analyzing || this.serverBusy) return 'Comparing…';
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
    // A re-run replaces whichever pairs are in scope — clear their old
    // result/error from the UI now rather than leaving it on screen (looking
    // current) until the new one streams in. Mirrors the server's own
    // "pending" selection: a pair with no result yet (including one that only
    // has a stored error from a failed attempt) is already about to run.
    for (const pair of this.case.pairs) {
      if (scope === 'all' || scope === pair.index || (scope === 'pending' && pair.result == null)) {
        pair.result = null;
        pair.error = null;
      }
    }

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
  /**
   * How to describe the images left out of the comparison, or '' for none.
   *
   * The generated original has no images and the signed copy usually has one,
   * so the returned count alone reads oddly ("1 image" — where?). Naming the
   * side keeps it unambiguous. Returns '' rather than null so the template's
   * `*ngIf="... as images"` treats "no images" as nothing to say. Results
   * stored before `media` existed have none and are silent too.
   */
  imagesSetAside(diff: DocumentDiffResult): string {
    const media = diff.media;
    if (!media || media.compared) return '';
    const plural = (n: number) => `${n} image${n > 1 ? 's' : ''}`;
    if (media.returned && media.original) {
      return `${plural(media.returned)} in the returned document and ${media.original} in the original`;
    }
    if (media.returned) return `${plural(media.returned)} in the returned document`;
    if (media.original) return `${plural(media.original)} in the original document`;
    return '';
  }

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

    const changes: DiffChange[] = res.changes.map((c, id) => ({
      id,
      type: c.type,
      before: c.before,
      after: c.after,
      possibleMissingSection: c.possibleMissingSection
    }));
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
      media: res.media,
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
