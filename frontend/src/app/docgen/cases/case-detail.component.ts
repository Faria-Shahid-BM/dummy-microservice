import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, RouterLink } from '@angular/router';
import {
  DocgenCaseDetail,
  DocgenService,
  DocumentProvenance,
  FillResponse,
  GeneratedDocument
} from '../docgen.service';
import { JobStatusComponent } from '../job-status/job-status.component';
import { JsonViewComponent } from '../../json-view/json-view.component';

@Component({
  selector: 'app-case-detail',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink, JobStatusComponent, JsonViewComponent],
  templateUrl: './case-detail.component.html'
})
export class CaseDetailComponent implements OnInit {
  caseId = '';
  case: DocgenCaseDetail | null = null;
  loading = false;
  error = '';

  inputFile: File | null = null;
  uploadingInput = false;
  inputError = '';

  extractJobId: string | null = null;
  extractError = '';

  caseText = '';
  caseTextLoaded = false;
  caseTextError = '';
  savingCaseText = false;

  analyzeJobId: string | null = null;
  analyzeError = '';

  analysisText = '';
  analysisLoaded = false;
  analysisError = '';
  savingAnalysis = false;

  selectJobId: string | null = null;
  selectError = '';

  selectedText = '';
  selectedLoaded = false;
  selectedError = '';
  savingSelected = false;

  filling = false;
  fillResult: FillResponse | null = null;
  fillError = '';

  documents: GeneratedDocument[] = [];
  documentsError = '';
  loadingDocuments = false;
  provenanceByDoc: Record<string, DocumentProvenance> = {};

  constructor(private route: ActivatedRoute, private docgen: DocgenService) {}

  get profileId(): string | null {
    return this.docgen.activeProfile?.id ?? null;
  }

  get readOnly(): boolean {
    return this.docgen.activeProfile?.is_default ?? false;
  }

  ngOnInit(): void {
    this.caseId = this.route.snapshot.paramMap.get('caseId') ?? '';
    this.loadCase();
  }

  loadCase(): void {
    if (!this.profileId || !this.caseId) return;
    this.error = '';
    this.loading = true;
    this.docgen.getCase(this.profileId, this.caseId).subscribe({
      next: (c) => {
        this.case = c;
        this.loading = false;
        if (c.has_case_text && !this.caseTextLoaded) this.loadCaseText();
        if (c.has_analysis && !this.analysisLoaded) this.loadAnalysis();
        if (c.has_selected && !this.selectedLoaded) this.loadSelected();
        if (c.generated_count > 0) this.loadDocuments();
      },
      error: (err: HttpErrorResponse) => {
        this.error = err.error?.detail ?? 'failed to load case';
        this.loading = false;
      }
    });
  }

  // --- input ---

  onInputFile(event: Event): void {
    this.inputFile = (event.target as HTMLInputElement).files?.[0] ?? null;
  }

  uploadInput(): void {
    if (!this.profileId || !this.inputFile) return;
    this.inputError = '';
    this.uploadingInput = true;
    this.docgen.uploadCaseInput(this.profileId, this.caseId, this.inputFile).subscribe({
      next: () => {
        this.uploadingInput = false;
        this.inputFile = null;
        this.loadCase();
      },
      error: (err: HttpErrorResponse) => {
        this.inputError = err.error?.detail ?? 'upload failed';
        this.uploadingInput = false;
      }
    });
  }

  // --- extract ---

  runExtract(): void {
    if (!this.profileId) return;
    this.extractError = '';
    this.docgen.runExtract(this.profileId, this.caseId).subscribe({
      next: (job) => (this.extractJobId = job.id),
      error: (err: HttpErrorResponse) => {
        this.extractError = err.error?.detail ?? 'failed to start extraction';
      }
    });
  }

  onExtractDone(): void {
    this.extractJobId = null;
    this.loadCase();
  }

  onExtractFailed(): void {
    this.extractError = 'Extraction failed — see the job status above.';
  }

  loadCaseText(): void {
    if (!this.profileId) return;
    this.caseTextError = '';
    this.docgen.getCaseText(this.profileId, this.caseId).subscribe({
      next: (res) => {
        this.caseText = res.content;
        this.caseTextLoaded = true;
      },
      error: (err: HttpErrorResponse) => {
        this.caseTextError = err.error?.detail ?? 'failed to load case text';
      }
    });
  }

  saveCaseText(): void {
    if (!this.profileId) return;
    this.savingCaseText = true;
    this.caseTextError = '';
    this.docgen.putCaseText(this.profileId, this.caseId, this.caseText).subscribe({
      next: () => (this.savingCaseText = false),
      error: (err: HttpErrorResponse) => {
        this.caseTextError = err.error?.detail ?? 'failed to save';
        this.savingCaseText = false;
      }
    });
  }

  // --- analyze (optional step) ---

  runAnalyze(): void {
    if (!this.profileId) return;
    this.analyzeError = '';
    this.docgen.runAnalyze(this.profileId, this.caseId).subscribe({
      next: (job) => (this.analyzeJobId = job.id),
      error: (err: HttpErrorResponse) => {
        this.analyzeError = err.error?.detail ?? 'failed to start analysis';
      }
    });
  }

  onAnalyzeDone(): void {
    this.analyzeJobId = null;
    this.loadCase();
  }

  onAnalyzeFailed(): void {
    this.analyzeError = 'Analysis failed — see the job status above.';
  }

  loadAnalysis(): void {
    if (!this.profileId) return;
    this.analysisError = '';
    this.docgen.getAnalysis(this.profileId, this.caseId).subscribe({
      next: (res) => {
        this.analysisText = res.content;
        this.analysisLoaded = true;
      },
      error: (err: HttpErrorResponse) => {
        this.analysisError = err.error?.detail ?? 'failed to load analysis';
      }
    });
  }

  saveAnalysis(): void {
    if (!this.profileId) return;
    this.savingAnalysis = true;
    this.analysisError = '';
    this.docgen.putAnalysis(this.profileId, this.caseId, this.analysisText).subscribe({
      next: () => (this.savingAnalysis = false),
      error: (err: HttpErrorResponse) => {
        this.analysisError = err.error?.detail ?? 'failed to save';
        this.savingAnalysis = false;
      }
    });
  }

  // --- select ---

  runSelect(): void {
    if (!this.profileId) return;
    this.selectError = '';
    this.docgen.runSelect(this.profileId, this.caseId).subscribe({
      next: (job) => (this.selectJobId = job.id),
      error: (err: HttpErrorResponse) => {
        this.selectError = err.error?.detail ?? 'failed to start selection';
      }
    });
  }

  onSelectDone(): void {
    this.selectJobId = null;
    this.loadCase();
  }

  onSelectFailed(): void {
    this.selectError = 'Selection failed — see the job status above.';
  }

  loadSelected(): void {
    if (!this.profileId) return;
    this.selectedError = '';
    this.docgen.getSelected(this.profileId, this.caseId).subscribe({
      next: (res) => {
        this.selectedText = res.content;
        this.selectedLoaded = true;
      },
      error: (err: HttpErrorResponse) => {
        this.selectedError = err.error?.detail ?? 'failed to load selection';
      }
    });
  }

  saveSelected(): void {
    if (!this.profileId) return;
    this.savingSelected = true;
    this.selectedError = '';
    this.docgen.putSelected(this.profileId, this.caseId, this.selectedText).subscribe({
      next: () => (this.savingSelected = false),
      error: (err: HttpErrorResponse) => {
        this.selectedError = err.error?.detail ?? 'failed to save';
        this.savingSelected = false;
      }
    });
  }

  // --- fill ---

  runFill(): void {
    if (!this.profileId) return;
    this.fillError = '';
    this.filling = true;
    this.docgen.runFill(this.profileId, this.caseId).subscribe({
      next: (res) => {
        this.fillResult = res;
        this.filling = false;
      },
      error: (err: HttpErrorResponse) => {
        this.fillError = err.error?.detail ?? 'failed to start fill';
        this.filling = false;
      }
    });
  }

  onFillJobSettled(): void {
    // Any one job in the fill batch finishing (success or failure) is a
    // fine moment to refresh — harmless to over-refresh the list/case flags.
    this.loadDocuments();
    this.loadCase();
  }

  // --- documents ---

  loadDocuments(): void {
    if (!this.profileId) return;
    this.documentsError = '';
    this.loadingDocuments = true;
    this.docgen.listDocuments(this.profileId, this.caseId).subscribe({
      next: (res) => {
        this.documents = res.documents;
        this.loadingDocuments = false;
      },
      error: (err: HttpErrorResponse) => {
        this.documentsError = err.error?.detail ?? 'failed to load documents';
        this.loadingDocuments = false;
      }
    });
  }

  submitDocument(doc: GeneratedDocument): void {
    if (!this.profileId) return;
    this.documentsError = '';
    this.docgen.submitDocument(this.profileId, this.caseId, doc.id).subscribe({
      next: () => this.loadDocuments(),
      error: (err: HttpErrorResponse) => {
        this.documentsError = err.error?.detail ?? 'failed to submit';
      }
    });
  }

  async downloadDocument(doc: GeneratedDocument): Promise<void> {
    if (!this.profileId) return;
    this.documentsError = '';
    try {
      await this.docgen.downloadFile(
        this.docgen.documentDownloadUrl(this.profileId, this.caseId, doc.id),
        doc.file_name
      );
    } catch (err) {
      this.documentsError = err instanceof Error ? err.message : 'download failed';
    }
  }

  async downloadAll(): Promise<void> {
    if (!this.profileId) return;
    this.documentsError = '';
    try {
      await this.docgen.downloadFile(this.docgen.downloadAllUrl(this.profileId, this.caseId), 'documents.zip');
    } catch (err) {
      this.documentsError = err instanceof Error ? err.message : 'download failed';
    }
  }

  toggleProvenance(doc: GeneratedDocument): void {
    if (this.provenanceByDoc[doc.id]) {
      delete this.provenanceByDoc[doc.id];
      return;
    }
    if (!this.profileId) return;
    this.documentsError = '';
    this.docgen.getProvenance(this.profileId, this.caseId, doc.id).subscribe({
      next: (prov) => (this.provenanceByDoc[doc.id] = prov),
      error: (err: HttpErrorResponse) => {
        this.documentsError = err.error?.detail ?? 'failed to load provenance';
      }
    });
  }
}
