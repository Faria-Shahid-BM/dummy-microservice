import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { DocgenCase, DocgenService } from '../docgen.service';

@Component({
  selector: 'app-case-list',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink],
  templateUrl: './case-list.component.html'
})
export class CaseListComponent implements OnInit {
  cases: DocgenCase[] = [];
  loading = false;
  error = '';

  newCaseName = '';
  creating = false;

  constructor(private docgen: DocgenService, private router: Router) {}

  ngOnInit(): void {
    this.load();
  }

  private get profileId(): string | null {
    return this.docgen.activeProfile?.id ?? null;
  }

  get readOnly(): boolean {
    return this.docgen.activeProfile?.is_default ?? false;
  }

  load(): void {
    if (!this.profileId) return;
    this.error = '';
    this.loading = true;
    this.docgen.listCases(this.profileId).subscribe({
      next: (res) => {
        this.cases = res.cases;
        this.loading = false;
      },
      error: (err: HttpErrorResponse) => {
        this.error = err.error?.detail ?? 'failed to load cases';
        this.loading = false;
      }
    });
  }

  createCase(): void {
    if (!this.profileId || !this.newCaseName.trim()) return;
    this.error = '';
    this.creating = true;
    this.docgen.createCase(this.profileId, this.newCaseName.trim()).subscribe({
      next: (created) => {
        this.creating = false;
        this.newCaseName = '';
        this.router.navigate(['/docgen/cases', created.id]);
      },
      error: (err: HttpErrorResponse) => {
        this.error = err.error?.detail ?? 'failed to create case';
        this.creating = false;
      }
    });
  }
}
