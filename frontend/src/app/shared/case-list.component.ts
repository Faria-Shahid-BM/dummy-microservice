import { CommonModule } from '@angular/common';
import { Component, Inject, OnInit } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { DataTableComponent, TableColumnDirective } from './data-table.component';
import { CASE_SERVICE, CaseService, CaseSummary } from './case.service';

// Shared by every stateless review service (collateral/valuation/insurance/
// docdiff) — the list page is identical across all of them, so this is the
// one copy. Bound to a concrete CaseService via the route's `providers`
// (see app.routes.ts); it never imports a specific service itself.
@Component({
  selector: 'app-case-list',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink, DataTableComponent, TableColumnDirective],
  templateUrl: './case-list.component.html'
})
export class CaseListComponent implements OnInit {
  cases: CaseSummary[] = [];
  loading = false;
  error = '';

  showAddCase = false;
  newCaseName = '';
  creating = false;
  createError = '';

  constructor(@Inject(CASE_SERVICE) public caseService: CaseService, private router: Router) {}

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    this.error = '';
    this.loading = true;
    this.caseService.listCases().subscribe({
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
    if (!this.newCaseName.trim()) return;
    this.createError = '';
    this.creating = true;
    this.caseService.createCase(this.newCaseName.trim()).subscribe({
      next: (created) => {
        this.creating = false;
        this.newCaseName = '';
        this.showAddCase = false;
        this.router.navigate([this.caseService.routeBase, 'cases', created.id]);
      },
      error: (err: HttpErrorResponse) => {
        this.createError = err.error?.detail ?? 'failed to create case';
        this.creating = false;
      }
    });
  }
}
