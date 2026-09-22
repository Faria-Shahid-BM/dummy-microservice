import { CommonModule } from '@angular/common';
import { Component, EventEmitter, Input, OnChanges, Output } from '@angular/core';

/**
 * Where a value sits in a document's text. Structurally compatible with the
 * engine's evidence span (see collateral.service.ts) but declared here so this
 * shared component doesn't depend on any one reviewer's result shape.
 */
export interface EvidenceHighlight {
  start: number;
  end: number;
  found_by: string;
  confidence: number;
}

/** Everything the panel shows about one extracted value. */
export interface EvidenceView {
  /** Which document, e.g. 'Legal opinion'. */
  docLabel: string;
  /** The uploaded file's name, so it's unambiguous which file this is. */
  fileName: string;
  /** Which field, e.g. 'Property address'. */
  fieldLabel: string;
  /** What the model reported as the value. */
  value: string;
  /** Where it was located, or null if it couldn't be found in the document. */
  span: EvidenceHighlight | null;
  /** Only set when the document's pages are actually knowable. */
  page: number | null;
  /** The document text; null while loading or on failure. */
  text: string | null;
  loading: boolean;
  error: string;
  /** The text this citation points into was never stored — a review from
   * before citations were recorded. Re-running the pair produces it, so the
   * panel offers that instead of showing a dead-end error. */
  stale: boolean;
}

// Anchor id for the highlighted run — a real [id] on an Angular-rendered
// element, so it survives (unlike ids inside [innerHTML], which the sanitizer
// strips — see docdiff's jumpToChange for that other case).
const HIT_ID = 'evidence-hit';

/**
 * A docked panel showing the document text an extracted value came from, with
 * that value highlighted and scrolled to.
 *
 * Deliberately NOT a modal: a reviewer checks a field per document — eighteen
 * of them on a collateral case — and a dialog that has to be dismissed between
 * each one turns a scan into a chore. This stays open at the side and re-aims
 * as each value is clicked.
 *
 * Purely presentational: the parent owns which value is open, and fetching the
 * text belongs to the service that knows the endpoint. All this does is slice
 * the text around the span, say how solid the match was, and scroll to it.
 */
@Component({
  selector: 'app-evidence-panel',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './evidence-panel.component.html'
})
export class EvidencePanelComponent implements OnChanges {
  @Input() view: EvidenceView | null = null;
  @Output() closed = new EventEmitter<void>();
  /** Asked for from the stale-citation notice; the parent owns analysis. */
  @Output() rerun = new EventEmitter<void>();

  /** The text split around the highlight; `hit` empty means nothing to mark. */
  before = '';
  hit = '';
  after = '';

  verdictText = '';
  verdictTone: 'ok' | 'warn' = 'ok';

  ngOnChanges(): void {
    const view = this.view;
    const text = view?.text ?? '';
    this.before = '';
    this.hit = '';
    this.after = '';
    if (!view) return;

    if (!view.span) {
      this.before = text;
      this.say('This value does not appear in the document text.', 'warn');
      return;
    }

    // The offsets and the text arrive from two different requests, so a pair
    // re-analyzed in between could leave them describing different documents.
    // Show the text unhighlighted rather than marking an arbitrary run.
    const { start, end } = view.span;
    if (start < 0 || end > text.length || end <= start) {
      this.before = text;
      this.say('The stored location does not fit this document — review this pair again.', 'warn');
      return;
    }

    this.before = text.slice(0, start);
    this.hit = text.slice(start, end);
    this.after = text.slice(end);
    this.describe(view.span);
    // The <mark> only exists once this change has rendered.
    setTimeout(() => this.reveal(), 0);
  }

  private describe(span: EvidenceHighlight): void {
    switch (span.found_by) {
      case 'exact':
        return this.say('Found in the document, word for word.', 'ok');
      case 'whitespace':
        return this.say('Found in the document — only spacing or capitals differ.', 'ok');
      case 'core':
        return this.say('Highlighted is the part of the document this value was taken from.', 'ok');
      case 'approximate':
        return this.say(
          `Only part of this value appears in the document — about ${Math.round(
            span.confidence * 100
          )}% of it.`,
          'warn'
        );
      default:
        return this.say('Found in the document.', 'ok');
    }
  }

  private say(text: string, tone: 'ok' | 'warn'): void {
    this.verdictText = text;
    this.verdictTone = tone;
  }

  // Same treatment docdiff uses for "View in document": centre it, then flash
  // it, so the eye lands on the right run in a wall of text.
  private reveal(): void {
    const element = document.getElementById(HIT_ID);
    if (!element) return;
    element.scrollIntoView({ behavior: 'smooth', block: 'center' });
    element.classList.add('flash');
    setTimeout(() => element.classList.remove('flash'), 1500);
  }
}
