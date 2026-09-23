import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { RouterLink } from '@angular/router';
import { forkJoin, of } from 'rxjs';
import { catchError } from 'rxjs/operators';
import { SessionService } from '../../session.service';
import { CaseService, CaseSummary } from '../../shared/case.service';
import { CollateralService } from '../../collateral/collateral.service';
import { ValuationService } from '../../valuation/valuation.service';
import { InsuranceService } from '../../insurance/insurance.service';
import { DocdiffService } from '../../docdiff/docdiff.service';

// Soft, DISPLAY-ONLY monthly allowance. Nothing on the backend enforces this —
// no service rejects a review past this count, and Kong carries no
// rate-limiting plugin (see POC_TO_PRODUCTION.md). It exists so a user has
// some sense of "how much am I using this", not as a real quota. If/when a
// real per-account limit is introduced, source it from there instead of this
// constant.
const MONTHLY_ALLOWANCE = 50;

interface ReviewerUsage {
  key: string;
  label: string;
  short: string;
  route: string;
  total: number;
  thisMonth: number;
  allowance: number;
  remaining: number;
  usedFrac: number;
}

// This account's own reviewers: which four it's entitled to, how much it has
// used this month, and how much of the (soft) monthly allowance is left.
// Only the four uniform CaseService reviewers are covered — docgen and
// policy_qa don't share this shape (docgen's cases are per-profile; policy_qa
// has no case concept at all) and are cheap enough to find via the sidebar,
// so they're left out here rather than bolted on awkwardly.
@Component({
  selector: 'app-user-home',
  standalone: true,
  imports: [CommonModule, RouterLink],
  templateUrl: './user-home.component.html',
  styleUrl: './user-home.component.css'
})
export class UserHomeComponent implements OnInit {
  loading = true;
  error = '';
  reviewers: ReviewerUsage[] = [];

  constructor(
    public session: SessionService,
    private collateral: CollateralService,
    private valuation: ValuationService,
    private insurance: InsuranceService,
    private docdiff: DocdiffService
  ) {}

  ngOnInit(): void {
    const catalog: { key: string; label: string; short: string; route: string; service: CaseService<unknown> }[] = [
      { key: 'docdiff', label: 'Document Reviewer', short: 'DR', route: '/docdiff', service: this.docdiff },
      { key: 'collateral', label: 'Collateral Reviewer', short: 'CR', route: '/collateral', service: this.collateral },
      { key: 'valuation', label: 'Valuation Review', short: 'VR', route: '/valuation', service: this.valuation },
      { key: 'insurance', label: 'Insurance Review', short: 'IR', route: '/insurance', service: this.insurance }
    ].filter((c) => this.session.has(c.key));

    if (!catalog.length) {
      this.loading = false;
      return;
    }

    forkJoin(
      catalog.map((c) =>
        c.service.listCases().pipe(
          catchError((err: HttpErrorResponse) => {
            // One reviewer failing to load (a transient 5xx) shouldn't blank
            // the whole dashboard — it just reports zero for that card.
            console.error(`dashboard: failed to load ${c.key} cases`, err);
            return of({ cases: [] as CaseSummary[] });
          })
        )
      )
    ).subscribe((results) => {
      this.reviewers = catalog.map((c, i) => {
        const cases = results[i].cases;
        const thisMonth = cases.filter((cs) => this.isThisMonth(cs.created_at)).length;
        const remaining = Math.max(0, MONTHLY_ALLOWANCE - thisMonth);
        return {
          key: c.key,
          label: c.label,
          short: c.short,
          route: c.route,
          total: cases.length,
          thisMonth,
          allowance: MONTHLY_ALLOWANCE,
          remaining,
          usedFrac: Math.min(1, thisMonth / MONTHLY_ALLOWANCE)
        };
      });
      this.loading = false;
    });
  }

  private isThisMonth(iso: string): boolean {
    const d = new Date(iso);
    const now = new Date();
    return d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth();
  }

  /** Below this fraction of the allowance left, the chip calls it out. */
  tone(r: ReviewerUsage): 'ok' | 'warn' | 'bad' {
    if (r.remaining === 0) return 'bad';
    if (r.usedFrac >= 0.8) return 'warn';
    return 'ok';
  }

  get hasAnyUsage(): boolean {
    return this.reviewers.some((r) => r.total > 0);
  }
}
