import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { DocgenService, TemplateDetail, TemplateVersion } from '../docgen.service';
import { JobStatusComponent } from '../job-status/job-status.component';

@Component({
  selector: 'app-template-detail',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink, JobStatusComponent],
  templateUrl: './template-detail.component.html'
})
export class TemplateDetailComponent implements OnInit {
  templateId = '';
  template: TemplateDetail | null = null;
  loading = false;
  error = '';

  newVersionFile: File | null = null;
  newVersionNote = '';
  uploadingVersion = false;

  analyzeJobIdByVersion: Record<string, string> = {};
  analyzeErrorByVersion: Record<string, string> = {};

  descriptorByVersion: Record<string, string> = {};
  descriptorErrorByVersion: Record<string, string> = {};
  savingDescriptorByVersion: Record<string, boolean> = {};

  constructor(private route: ActivatedRoute, private docgen: DocgenService) {}

  get profileId(): string | null {
    return this.docgen.activeProfile?.id ?? null;
  }

  get readOnly(): boolean {
    return this.docgen.activeProfile?.is_default ?? false;
  }

  ngOnInit(): void {
    this.templateId = this.route.snapshot.paramMap.get('tid') ?? '';
    this.load();
  }

  load(): void {
    if (!this.profileId || !this.templateId) return;
    this.error = '';
    this.loading = true;
    this.docgen.getTemplate(this.profileId, this.templateId).subscribe({
      next: (t) => {
        this.template = t;
        this.loading = false;
      },
      error: (err: HttpErrorResponse) => {
        this.error = err.error?.detail ?? 'failed to load template';
        this.loading = false;
      }
    });
  }

  onVersionFile(event: Event): void {
    this.newVersionFile = (event.target as HTMLInputElement).files?.[0] ?? null;
  }

  uploadVersion(): void {
    if (!this.profileId || !this.newVersionFile) return;
    this.error = '';
    this.uploadingVersion = true;
    this.docgen
      .uploadTemplateVersion(this.profileId, this.templateId, this.newVersionFile, this.newVersionNote)
      .subscribe({
        next: () => {
          this.uploadingVersion = false;
          this.newVersionFile = null;
          this.newVersionNote = '';
          this.load();
        },
        error: (err: HttpErrorResponse) => {
          this.error = err.error?.detail ?? 'failed to upload version';
          this.uploadingVersion = false;
        }
      });
  }

  analyzeVersion(v: TemplateVersion): void {
    if (!this.profileId) return;
    delete this.analyzeErrorByVersion[v.id];
    this.docgen.analyzeTemplateVersion(this.profileId, this.templateId, v.id).subscribe({
      next: (job) => (this.analyzeJobIdByVersion[v.id] = job.id),
      error: (err: HttpErrorResponse) => {
        this.analyzeErrorByVersion[v.id] = err.error?.detail ?? 'failed to start analysis';
      }
    });
  }

  onAnalyzeVersionDone(v: TemplateVersion): void {
    delete this.analyzeJobIdByVersion[v.id];
    this.load();
  }

  onAnalyzeVersionFailed(v: TemplateVersion): void {
    this.analyzeErrorByVersion[v.id] = 'Analysis failed — see the job status above.';
  }

  toggleDescriptor(v: TemplateVersion): void {
    if (this.descriptorByVersion[v.id] !== undefined) {
      delete this.descriptorByVersion[v.id];
      return;
    }
    if (!this.profileId) return;
    delete this.descriptorErrorByVersion[v.id];
    this.docgen.getDescriptor(this.profileId, this.templateId, v.id).subscribe({
      next: (res) => (this.descriptorByVersion[v.id] = res.descriptor),
      error: (err: HttpErrorResponse) => {
        this.descriptorErrorByVersion[v.id] = err.error?.detail ?? 'failed to load descriptor';
      }
    });
  }

  saveDescriptor(v: TemplateVersion): void {
    if (!this.profileId) return;
    const content = this.descriptorByVersion[v.id];
    if (content === undefined) return;
    this.savingDescriptorByVersion[v.id] = true;
    delete this.descriptorErrorByVersion[v.id];
    this.docgen.putDescriptor(this.profileId, this.templateId, v.id, content).subscribe({
      next: () => (this.savingDescriptorByVersion[v.id] = false),
      error: (err: HttpErrorResponse) => {
        this.descriptorErrorByVersion[v.id] = err.error?.detail ?? 'failed to save';
        this.savingDescriptorByVersion[v.id] = false;
      }
    });
  }

  async downloadVersion(v: TemplateVersion): Promise<void> {
    if (!this.profileId) return;
    this.error = '';
    try {
      await this.docgen.downloadFile(
        this.docgen.templateVersionFileUrl(this.profileId, this.templateId, v.id),
        v.file_name
      );
    } catch (err) {
      this.error = err instanceof Error ? err.message : 'download failed';
    }
  }
}
