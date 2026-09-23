import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { RouterLink } from '@angular/router';
import { forkJoin } from 'rxjs';
import { KONG_BASE } from '../../session.service';

interface ManagedUser {
  username: string;
  scopes: string[];
}

// Mirrors admin/audit/audit.model.ts's AuditEntry — only the fields this
// view aggregates from, so it isn't coupled to that page's full display-node
// shape.
interface AuditRow {
  timestamp: string;
  user_id: string;
  service: string;
}

interface DayBucket {
  label: string;
  count: number;
  frac: number;
}

interface ServiceBucket {
  service: string;
  label: string;
  count: number;
  frac: number;
  color: string;
}

const DAYS_SHOWN = 14;
// How today counts as "recently active" for the stat tile below.
const ACTIVE_WINDOW_DAYS = 7;

// Friendly labels for the `service` values services actually log (see
// case_store.py's service_scope, docgen-service/app/audit.py's SERVICE_NAME,
// policyqa-service/main.py's outbox.enqueue). Anything not listed here still
// renders — humanized — rather than being dropped.
const SERVICE_LABELS: Record<string, string> = {
  docdiff: 'Document Reviewer',
  collateral: 'Collateral Reviewer',
  valuation: 'Valuation Review',
  insurance: 'Insurance Review',
  'docgen-service': 'Document Generation',
  'policyqa-service': 'Policy Q&A'
};

// Fixed hue order for the per-service bars — assigned once, by this list, not
// regenerated per render, so a service's colour never shifts as others come
// and go from the top-N (see dataviz skill's categorical-colour rule). Same
// validated 8-hue set the insurance/valuation report tabs draw their tones
// from in spirit, sized for this app's actual service count.
const SERVICE_COLOR_ORDER = [
  'docdiff',
  'collateral',
  'valuation',
  'insurance',
  'docgen-service',
  'policyqa-service'
];
const CATEGORICAL_HUES = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#4a3aa7', '#008300', '#e34948'];

// The admin landing view: headcounts and an activity overview drawn from the
// audit trail every case-service/docgen/policyqa write already produces (see
// case_store.py's _audit / docgen-service/app/audit.py / policyqa-service's
// outbox). No new backend endpoint — same two calls the Users and Audit
// pages already make, just aggregated here instead of listed row by row.
@Component({
  selector: 'app-admin-home',
  standalone: true,
  imports: [CommonModule, RouterLink],
  templateUrl: './admin-home.component.html',
  styleUrl: './admin-home.component.css'
})
export class AdminHomeComponent implements OnInit {
  // Exposed for the template's day-bar height clamp (Angular templates have
  // no global Math).
  readonly Math = Math;

  loading = true;
  error = '';

  totalUsers = 0;
  totalEvents = 0;
  eventsToday = 0;
  activeUsers = 0;

  days: DayBucket[] = [];
  services: ServiceBucket[] = [];

  constructor(private http: HttpClient) {}

  ngOnInit(): void {
    forkJoin({
      users: this.http.get<ManagedUser[]>(`${KONG_BASE}/api/auth/users`),
      // audit-service's endpoint is "/audit", Kong's route prefix is also
      // "/api/audit" with strip_path — the doubled segment is intentional,
      // matching admin-audit.component.ts.
      entries: this.http.get<AuditRow[]>(`${KONG_BASE}/api/audit/audit`)
    }).subscribe({
      next: ({ users, entries }) => {
        this.totalUsers = users.length;
        this.build(entries);
        this.loading = false;
      },
      error: (err: HttpErrorResponse) => {
        this.error = err.error?.detail ?? 'failed to load dashboard data';
        this.loading = false;
      }
    });
  }

  private build(entries: AuditRow[]): void {
    this.totalEvents = entries.length;

    const today = this.dayKey(new Date());
    const activeSince = Date.now() - ACTIVE_WINDOW_DAYS * 24 * 60 * 60 * 1000;
    const activeUserIds = new Set<string>();
    let todayCount = 0;

    const perDay = new Map<string, number>();
    const perService = new Map<string, number>();

    for (const e of entries) {
      const t = new Date(e.timestamp).getTime();
      if (Number.isNaN(t)) continue;

      const key = this.dayKey(new Date(t));
      perDay.set(key, (perDay.get(key) ?? 0) + 1);
      perService.set(e.service, (perService.get(e.service) ?? 0) + 1);

      if (key === today) todayCount++;
      if (t >= activeSince) activeUserIds.add(e.user_id);
    }

    this.eventsToday = todayCount;
    this.activeUsers = activeUserIds.size;

    // Always the last DAYS_SHOWN calendar days, oldest first, zero-filled —
    // a quiet day should read as a short bar, not vanish from the axis.
    const buckets: { key: string; label: string; count: number }[] = [];
    for (let i = DAYS_SHOWN - 1; i >= 0; i--) {
      const d = new Date();
      d.setDate(d.getDate() - i);
      const key = this.dayKey(d);
      buckets.push({ key, label: d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }), count: perDay.get(key) ?? 0 });
    }
    const maxDay = Math.max(1, ...buckets.map((b) => b.count));
    this.days = buckets.map((b) => ({ label: b.label, count: b.count, frac: b.count / maxDay }));

    const maxService = Math.max(1, ...Array.from(perService.values()));
    this.services = Array.from(perService.entries())
      .sort((a, b) => b[1] - a[1])
      .map(([service, count]) => ({
        service,
        label: SERVICE_LABELS[service] ?? this.humanize(service),
        count,
        frac: count / maxService,
        color: this.colorFor(service)
      }));
  }

  private colorFor(service: string): string {
    const i = SERVICE_COLOR_ORDER.indexOf(service);
    return CATEGORICAL_HUES[i >= 0 ? i : CATEGORICAL_HUES.length - 1];
  }

  private humanize(key: string): string {
    const words = key.replace(/[-_]/g, ' ').trim();
    return words.charAt(0).toUpperCase() + words.slice(1);
  }

  private dayKey(d: Date): string {
    return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
  }
}
