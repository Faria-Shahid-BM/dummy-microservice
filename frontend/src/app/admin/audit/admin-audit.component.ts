import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { KONG_BASE, SessionService } from '../../session.service';

interface AuditEntry {
  user_id: string;
  service: string;
  action: string;
  resource: string | null;
  timestamp: string;
}

interface AuditPage {
  items: AuditEntry[];
  total: number;
}

@Component({
  selector: 'app-admin-audit',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './admin-audit.component.html'
})
export class AdminAuditComponent implements OnInit {
  entries: AuditEntry[] = [];
  total = 0;
  page = 1;
  pageSize = 20;
  error = '';
  loading = false;

  constructor(private http: HttpClient, private session: SessionService) {}

  ngOnInit(): void {
    this.load();
  }

  get totalPages(): number {
    return Math.max(1, Math.ceil(this.total / this.pageSize));
  }

  load(): void {
    this.error = '';
    this.loading = true;
    const offset = (this.page - 1) * this.pageSize;
    this.http
      .get<AuditPage>(`${KONG_BASE}/api/audit`, {
        headers: this.session.authHeaders(),
        params: { limit: this.pageSize, offset }
      })
      .subscribe({
        next: (res) => {
          this.entries = res.items;
          this.total = res.total;
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
      this.load();
    }
  }

  nextPage(): void {
    if (this.page < this.totalPages) {
      this.page++;
      this.load();
    }
  }
}
