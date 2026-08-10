import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { RouterLink } from '@angular/router';
import { KONG_BASE } from '../../session.service';
import { AuditEntry } from './audit.model';

@Component({
  selector: 'app-admin-audit',
  standalone: true,
  imports: [CommonModule, RouterLink],
  templateUrl: './admin-audit.component.html'
})
export class AdminAuditComponent implements OnInit {
  // audit-service returns the full log as a plain array with no pagination
  // or auth of its own (see kong.yml — the /api/audit GET route is the one
  // place that still enforces jwt+exp at the edge). Pagination below is done
  // client-side over the full fetched list.
  allEntries: AuditEntry[] = [];
  page = 1;
  pageSize = 20;
  error = '';
  loading = false;

  constructor(private http: HttpClient) {}

  ngOnInit(): void {
    this.load();
  }

  get total(): number {
    return this.allEntries.length;
  }

  get totalPages(): number {
    return Math.max(1, Math.ceil(this.total / this.pageSize));
  }

  get entries(): AuditEntry[] {
    const start = (this.page - 1) * this.pageSize;
    return this.allEntries.slice(start, start + this.pageSize);
  }

  load(): void {
    this.error = '';
    this.loading = true;
    // audit-service defines its endpoint at "/audit" (not root), and Kong's
    // route prefix is also "/api/audit" with strip_path — so the externally
    // reachable path ends up with the segment doubled. See README.md.
    this.http
      .get<AuditEntry[]>(`${KONG_BASE}/api/audit/audit`)
      .subscribe({
        next: (res) => {
          // Newest first, matching the paginated API's previous ordering.
          this.allEntries = [...res].reverse();
          this.page = 1;
          this.loading = false;
        },
        error: (err: HttpErrorResponse) => {
          this.error = err.error?.detail ?? 'failed to load audit log';
          this.loading = false;
        }
      });
  }

  prevPage(): void {
    if (this.page > 1) {
      this.page--;
    }
  }

  nextPage(): void {
    if (this.page < this.totalPages) {
      this.page++;
    }
  }
}
