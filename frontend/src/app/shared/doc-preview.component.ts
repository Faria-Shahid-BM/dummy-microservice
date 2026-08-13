import { CommonModule } from '@angular/common';
import { Component, Input, OnDestroy } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { DomSanitizer, SafeResourceUrl } from '@angular/platform-browser';
import * as mammoth from 'mammoth';
import { CaseService } from './case.service';

// Lets a reviewer look at a slot's document before deciding what to compare
// it against — a .pdf renders natively via <iframe>, but a .docx has no
// browser-native renderer, so it's converted to plain HTML client-side with
// mammoth. Good enough fidelity for "is this the right document," not a
// faithful re-render of Word's exact layout.
@Component({
  selector: 'app-doc-preview',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './doc-preview.component.html'
})
export class DocPreviewComponent implements OnDestroy {
  @Input({ required: true }) service!: CaseService<unknown>;
  @Input({ required: true }) caseId!: string;
  @Input({ required: true }) slot!: string;
  @Input({ required: true }) fileName!: string;

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
    this.service.downloadSlot(this.caseId, this.slot).subscribe({
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

  ngOnDestroy(): void {
    if (this.objectUrl) URL.revokeObjectURL(this.objectUrl);
  }
}
