import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { KONG_BASE } from '../../session.service';
import { attachmentUrl, AuditAttachment, AuditEntry } from './audit.model';

@Component({
  selector: 'app-admin-audit-detail',
  standalone: true,
  imports: [CommonModule, RouterLink],
  templateUrl: './admin-audit-detail.component.html'
})
export class AdminAuditDetailComponent implements OnInit {
  entry: AuditEntry | null = null;
  loading = false;
  error = '';

  constructor(private route: ActivatedRoute, private http: HttpClient) {}

  ngOnInit(): void {
    const entryId = this.route.snapshot.paramMap.get('entryId') ?? '';
    this.error = '';
    this.loading = true;
    // Same "doubled path" quirk as the list page: /api/audit/audit/{id}.
    this.http.get<AuditEntry>(`${KONG_BASE}/api/audit/audit/${entryId}`).subscribe({
      next: (res) => {
        this.entry = res;
        this.loading = false;
      },
      error: (err: HttpErrorResponse) => {
        this.error = err.error?.detail ?? 'failed to load audit entry';
        this.loading = false;
      }
    });
  }

  attachmentHref(att: AuditAttachment): string | null {
    return att.attachment_id ? attachmentUrl(att.attachment_id, att.filename) : null;
  }

  hasAnyDetail(): boolean {
    return !!this.entry && (this.entry.attachments.length > 0 || this.entry.sections.length > 0);
  }
}
