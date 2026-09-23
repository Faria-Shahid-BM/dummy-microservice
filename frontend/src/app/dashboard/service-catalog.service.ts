import { Injectable } from '@angular/core';
import { Router, NavigationEnd } from '@angular/router';
import { BehaviorSubject } from 'rxjs';
import { filter } from 'rxjs/operators';
import { SessionService } from '../session.service';

export type ServiceKind = 'diff' | 'collateral' | 'valuation' | 'insurance' | 'policyqa' | 'docgen';

export interface ServiceMeta {
  key: string;
  label: string;
  // 1-2 letter badge shown on the collapsed sidebar rail in place of the
  // full label (see app-shell.component.html) — set explicitly rather than
  // derived from `label`, so it stays stable and collision-free (e.g.
  // "Document Reviewer" and "Document Generation" would otherwise both
  // reduce to "D").
  short: string;
  kind: ServiceKind;
  path: string;
  // When set, the sidebar renders a router link to this route instead of an
  // inline panel — docgen is a whole multi-page mini-app (cases, templates,
  // approvals), not a single-result panel like the others.
  route?: string;
}

const SERVICE_CATALOG: ServiceMeta[] = [
  {
    key: 'docdiff',
    label: 'Document Reviewer',
    short: 'DR',
    kind: 'diff',
    path: '/api/docdiff',
    route: '/docdiff'
  },
  {
    key: 'collateral',
    label: 'Collateral Reviewer',
    short: 'CR',
    kind: 'collateral',
    path: '/api/collateral',
    route: '/collateral'
  },
  {
    key: 'valuation',
    label: 'Valuation Review',
    short: 'VR',
    kind: 'valuation',
    path: '/api/valuation',
    route: '/valuation'
  },
  {
    key: 'insurance',
    label: 'Insurance Review',
    short: 'IR',
    kind: 'insurance',
    path: '/api/insurance',
    route: '/insurance'
  },
  { key: 'policy_qa', label: 'Policy Q&A', short: 'PQ', kind: 'policyqa', path: '/api/policyqa' },
  {
    key: 'docgen',
    label: 'Document Generation',
    short: 'DG',
    kind: 'docgen',
    path: '/api/profiles',
    route: '/docgen'
  }
];

// Sidebar is mounted once (in AppShellComponent) and persists across the
// /dashboard <-> /docgen navigation, so the "which inline panel is picked"
// state can't live on DashboardComponent anymore — it would be lost/reset
// every time the sidebar navigates away and back. This service is the
// shared home for that selection.
@Injectable({ providedIn: 'root' })
export class ServiceCatalogService {
  private readonly selectedSubject = new BehaviorSubject<ServiceMeta | null>(null);
  readonly selected$ = this.selectedSubject.asObservable();

  constructor(private session: SessionService, router: Router) {
    // "Selected" means "which inline panel dashboard.component.html should
    // show" — meaningful only ON /dashboard (only Policy Q&A still renders
    // that way; every other service is now a routed page — see
    // app-shell.component.html). Leaving /dashboard for a routed service
    // used to leave the old selection sitting here with nothing to clear it,
    // so returning to /dashboard later (e.g. the topbar brand link) found
    // `selected` still pointing at whatever was clicked last and rendered
    // neither the landing view (selected must be null) nor that service's
    // panel (only policyqa has one) — the page just went blank. Clearing on
    // every navigation away from /dashboard is what AppShellComponent's
    // selectKind() (the one caller left that sets this) effectively assumed
    // was already happening.
    router.events.pipe(filter((e): e is NavigationEnd => e instanceof NavigationEnd)).subscribe((e) => {
      if (!e.urlAfterRedirects.startsWith('/dashboard')) {
        this.selectedSubject.next(null);
      }
    });
  }

  get selected(): ServiceMeta | null {
    return this.selectedSubject.value;
  }

  get entitledServices(): ServiceMeta[] {
    const granted = this.session.session?.scopes ?? [];
    return SERVICE_CATALOG.filter((s) =>
      // docgen is entitled by either of its scopes — a checker holds
      // "docgen_check", which on its own must still show the entry.
      s.key === 'docgen' ? this.session.canUseDocgen : granted.includes(s.key)
    );
  }

  select(meta: ServiceMeta): void {
    this.selectedSubject.next(meta);
  }
}
