import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { DataTableComponent, TableColumnDirective } from '../../shared/data-table.component';
import { DocgenCase, DocgenService } from '../docgen.service';

@Component({
  selector: 'app-case-list',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink, DataTableComponent, TableColumnDirective],
  templateUrl: './case-list.component.html'
})
export class CaseListComponent implements OnInit {
  cases: DocgenCase[] = [];
  loading = false;
  error = '';

  showAddCase = false;
  newCaseName = '';
  creating = false;
  createError = '';

  constructor(private docgen: DocgenService, private router: Router) {}

  private get profileId(): string | null {
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

  toggleAddCase(): void {
    this.showAddCase = !this.showAddCase;
    this.createError = '';
  }

  createCase(): void {
    if (!this.profileId || !this.newCaseName.trim()) return;
    this.createError = '';
    this.creating = true;
    this.docgen.createCase(this.profileId, this.newCaseName.trim()).subscribe({
      next: (created) => {
        this.creating = false;
        this.newCaseName = '';
        this.showAddCase = false;
        this.router.navigate(['/docgen/cases', created.id]);
      },
      error: (err: HttpErrorResponse) => {
        this.createError = err.error?.detail ?? 'failed to create case';
        this.creating = false;
      }
    });
  }
}
