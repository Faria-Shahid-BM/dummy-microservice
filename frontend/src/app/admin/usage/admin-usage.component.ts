import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { forkJoin } from 'rxjs';
import { KONG_BASE } from '../../session.service';

interface UsageRow {
  user_id: string;
  service: string;
  runs: number;
  prompt: number;
  completion: number;
  total: number;
  last_run: string | null;
  models: string[];
}

interface ManagedUser {
  username: string;
  scopes: string[];
}

interface ConfigOverview {
  /** scope -> role -> model, from the deployment environment. */
  defaults: Record<string, Record<string, string>>;
  /** user -> scope -> role -> model, only where a user chose something. */
  overrides: Record<string, Record<string, Record<string, string>>>;
}

/** One reviewer's resolved models for one user, and whether they chose them. */
interface ModelRow {
  service: string;
  role: string;
  model: string;
  overridden: boolean;
}

interface UserModels {
  username: string;
  rows: ModelRow[];
}

@Component({
  selector: 'app-admin-usage',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './admin-usage.component.html',
  styleUrl: './admin-usage.component.css'
})
export class AdminUsageComponent implements OnInit {
  usage: UsageRow[] = [];
  userModels: UserModels[] = [];
  error = '';
  loading = false;

  constructor(private http: HttpClient) {}

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    this.error = '';
    this.loading = true;
    // Three sources because no single service owns the answer: audit-service
    // knows what was spent, config-service knows what is configured, and
    // auth-service is the only list of users who exist at all. A user who has
    // never run anything appears via the third but not the first.
    forkJoin({
      usage: this.http.get<{ by_user_service: UsageRow[] }>(
        `${KONG_BASE}/api/audit/audit/usage`
      ),
      config: this.http.get<ConfigOverview>(`${KONG_BASE}/api/config/admin/overview`),
      users: this.http.get<ManagedUser[]>(`${KONG_BASE}/api/auth/users`)
    }).subscribe({
      next: ({ usage, config, users }) => {
        this.usage = usage.by_user_service ?? [];
        this.userModels = this.resolveModels(config, users);
        this.loading = false;
      },
      error: (err: HttpErrorResponse) => {
        this.error = err.error?.detail ?? 'failed to load usage';
        this.loading = false;
      }
    });
  }

  /**
   * A user's effective model per reviewer: their override where they set one,
   * the deployment default otherwise. Only reviewers they're entitled to —
   * a setting for a service they can't reach would just be noise.
   */
  private resolveModels(config: ConfigOverview, users: ManagedUser[]): UserModels[] {
    return users.map((user) => {
      const chosen = config.overrides[user.username] ?? {};
      const rows: ModelRow[] = [];
      for (const [service, roles] of Object.entries(config.defaults)) {
        if (!user.scopes.includes(service)) continue;
        for (const [role, fallback] of Object.entries(roles)) {
          const override = chosen[service]?.[role];
          rows.push({
            service,
            role,
            model: override || fallback,
            overridden: !!override
          });
        }
      }
      return { username: user.username, rows };
    });
  }

  /** Which users' model lists are expanded. Collapsed by default: the list is
   *  long and mostly defaults, so the interesting rows drown in it. */
  private expanded = new Set<string>();

  isExpanded(username: string): boolean {
    return this.expanded.has(username);
  }

  toggle(username: string): void {
    if (!this.expanded.delete(username)) this.expanded.add(username);
  }

  /** Reviewers a user has actually chosen a model for — worth seeing without
   *  expanding, since that is why their results may differ from everyone's. */
  overriddenCount(user: UserModels): number {
    return user.rows.filter((r) => r.overridden).length;
  }

  /**
   * Reviewers label themselves inconsistently in the audit trail — the
   * case-based ones by JWT scope ("collateral"), policy Q&A by service name
   * ("policyqa-service"). Normalised here rather than by rewriting what is
   * already recorded, since the audit trail is meant to be append-only.
   */
  serviceLabel(service: string): string {
    return service.replace(/-service$/, '');
  }

  get totalTokens(): number {
    return this.usage.reduce((sum, row) => sum + row.total, 0);
  }

  get hasUsage(): boolean {
    return this.usage.length > 0;
  }
}
