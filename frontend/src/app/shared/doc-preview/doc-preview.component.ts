import { CommonModule } from '@angular/common';
import { Component, Input, OnChanges, OnDestroy, SimpleChanges } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { DomSanitizer, SafeResourceUrl } from '@angular/platform-browser';
import * as mammoth from 'mammoth';
import { CaseService } from '../case.service';

// Lets a reviewer look at a slot's document before deciding what to compare
// it against — a .pdf renders natively via <iframe>, but a .docx has no
// browser-native renderer, so it's converted to plain HTML client-side with
// mammoth. Good enough fidelity for "is this the right document," not a
// faithful re-render of Word's exact layout.
@Component({
  selector: 'app-doc-preview',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './doc-preview.component.html',
  styleUrl: './doc-preview.component.css'
})
export class DocPreviewComponent implements OnChanges, OnDestroy {
  @Input({ required: true }) service!: CaseService<unknown>;
  @Input({ required: true }) caseId!: string;
  @Input({ required: true }) slot!: string;
  @Input({ required: true }) fileName!: string;
  /** Which pair's copy of the slot. Without it every preview fetched pair 0's
   * file, so pair 2+ showed the wrong document or failed to render. */
  @Input() pairIndex = 0;

  open = false;
  loading = false;
  error = '';
  html: string | null = null;
  pdfUrl: SafeResourceUrl | null = null;

  private objectUrl: string | null = null;

  constructor(private sanitizer: DomSanitizer) {}

  get isPdf(): boolean {
    return this.fileName.toLowerCase().endsWith('.pdf');
  }

  /**
   * Drop the cached render when this instance is pointed at a different
   * document. The cache below is keyed to nothing, so a slot whose file
   * changes while the component stays alive — a docdiff `original` that
   * Document Generator regenerated — would keep showing the old one.
   */
  ngOnChanges(changes: SimpleChanges): void {
    const moved = ['caseId', 'slot', 'fileName', 'pairIndex'].some(
      (k) => changes[k] && !changes[k].firstChange
    );
    if (moved) this.reset();
  }

  private reset(): void {
    this.revokeObjectUrl();
    this.html = null;
    this.pdfUrl = null;
    this.error = '';
    this.loading = false;
  }

  toggle(): void {
    if (this.open) {
      this.open = false;
      return;
    }
    this.open = true;
    // Fetch once per instance — the slot's file doesn't change under an open panel.
    if (this.html !== null || this.pdfUrl !== null || this.loading) return;
    this.load();
  }

  private load(): void {
    this.error = '';
    this.loading = true;
    this.service.downloadSlot(this.caseId, this.slot, this.pairIndex).subscribe({
      next: (blob) => this.render(blob),
      error: (err: HttpErrorResponse) => {
        this.error = err.error?.detail ?? 'could not load the document';
        this.loading = false;
      }
    });
  }

  private async render(blob: Blob): Promise<void> {
    try {
      if (this.isPdf) {
        this.objectUrl = URL.createObjectURL(blob);
        this.pdfUrl = this.sanitizer.bypassSecurityTrustResourceUrl(this.objectUrl);
      } else {
        const buffer = await blob.arrayBuffer();
        const result = await mammoth.convertToHtml({ arrayBuffer: buffer });
        this.html = result.value;
      }
    } catch {
      this.error = 'could not render this document';
    } finally {
      this.loading = false;
    }
  }

  private revokeObjectUrl(): void {
    if (this.objectUrl) URL.revokeObjectURL(this.objectUrl);
    this.objectUrl = null;
  }

  ngOnDestroy(): void {
    this.revokeObjectUrl();
  }
}
