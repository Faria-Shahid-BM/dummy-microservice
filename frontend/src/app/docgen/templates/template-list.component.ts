import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { DocgenService, DocgenTemplate } from '../docgen.service';

@Component({
  selector: 'app-template-list',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink],
  templateUrl: './template-list.component.html'
})
export class TemplateListComponent implements OnInit {
  templates: DocgenTemplate[] = [];
  loading = false;
  error = '';

  newFile: File | null = null;
  newName = '';
  newLanguage: 'en' | 'ar' | 'bilingual' = 'en';
  newNote = '';
  creating = false;

  constructor(private docgen: DocgenService) {}

  get profileId(): string | null {
    return this.docgen.activeProfile?.id ?? null;
  }

  get readOnly(): boolean {
    return this.docgen.activeProfile?.is_default ?? false;
  }

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    if (!this.profileId) return;
    this.error = '';
    this.loading = true;
    this.docgen.listTemplates(this.profileId).subscribe({
      next: (res) => {
        this.templates = res.templates;
        this.loading = false;
      },
      error: (err: HttpErrorResponse) => {
        this.error = err.error?.detail ?? 'failed to load templates';
        this.loading = false;
      }
    });
  }

  onFile(event: Event): void {
    this.newFile = (event.target as HTMLInputElement).files?.[0] ?? null;
  }

  create(): void {
    if (!this.profileId || !this.newFile) return;
    this.error = '';
    this.creating = true;
    this.docgen
      .createTemplate(this.profileId, this.newFile, this.newName, this.newLanguage, this.newNote)
      .subscribe({
        next: () => {
          this.creating = false;
          this.newFile = null;
          this.newName = '';
          this.newNote = '';
          this.load();
        },
        error: (err: HttpErrorResponse) => {
          this.error = err.error?.detail ?? 'failed to create template';
          this.creating = false;
        }
      });
  }
}
