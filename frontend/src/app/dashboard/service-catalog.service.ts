import { Injectable } from '@angular/core';
import { BehaviorSubject } from 'rxjs';
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

  constructor(private session: SessionService) {}

  get selected(): ServiceMeta | null {
    return this.selectedSubject.value;
  }

  get entitledServices(): ServiceMeta[] {
    const granted = this.session.session?.scopes ?? [];
    return SERVICE_CATALOG.filter((s) => granted.includes(s.key));
  }

  select(meta: ServiceMeta): void {
    this.selectedSubject.next(meta);
  }
}
