import { CommonModule } from '@angular/common';
import { Component } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Router, RouterLink } from '@angular/router';
import { SessionService, KONG_BASE } from '../session.service';

type ServiceKind = 'diff' | 'collateral';

interface ServiceMeta {
  key: string;
  label: string;
  kind: ServiceKind;
  path: string;
}

const SERVICE_CATALOG: ServiceMeta[] = [
  { key: 'document-reviewer', label: 'Document Reviewer', kind: 'diff', path: '/api/documents' },
  { key: 'collateral-reviewer', label: 'Collateral Reviewer', kind: 'collateral', path: '/api/collateral' }
];

interface DiffChange {
  id: number;
  type: string;
  before: string;
  after: string;
  possibleMissingSection?: boolean;
}

interface DiffSegment {
  op: 'equal' | 'delete' | 'insert';
  text: string;
  changeId: number | null;
}

interface DocumentDiffResult {
  render: 'html' | 'text';
  identical: boolean;
  similarity: number;
  summary: { insertions: number; deletions: number; replacements: number; changes: number };
  changes: DiffChange[];
  segments?: DiffSegment[];
  html?: string;
  missingPages?: number[];
}

interface CollateralComparisonRow {
  field: string;
  label: string;
  legal_value: string | null;
  property_value: string | null;
  status: 'match' | 'mismatch' | 'missing';
}

interface CollateralResult {
  comparison: CollateralComparisonRow[];
  observations: string[];
  summary: { matches: number; mismatches: number; missing: number; fields: number };
}

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [CommonModule, RouterLink],
  templateUrl: './dashboard.component.html'
})
export class DashboardComponent {
  selected: ServiceMeta | null = null;

  originalFile: File | null = null;
  returnedFile: File | null = null;
  diffResult: DocumentDiffResult | null = null;
  diffError = '';
  comparingDiff = false;

  legalFile: File | null = null;
  propertyFile: File | null = null;
  collateralResult: CollateralResult | null = null;
  collateralError = '';
  comparingCollateral = false;

  constructor(public session: SessionService, private http: HttpClient, private router: Router) {}

  get entitledServices(): ServiceMeta[] {
    const granted = this.session.session?.services ?? [];
    return SERVICE_CATALOG.filter((s) => granted.includes(s.key));
  }

  select(meta: ServiceMeta): void {
    this.selected = meta;
    this.diffResult = null;
    this.diffError = '';
    this.collateralResult = null;
    this.collateralError = '';
  }

  logout(): void {
    this.session.logout();
    this.router.navigate(['/login']);
  }

  // --- document-reviewer ---

  onOriginalFile(event: Event): void {
    this.originalFile = (event.target as HTMLInputElement).files?.[0] ?? null;
  }

  onReturnedFile(event: Event): void {
    this.returnedFile = (event.target as HTMLInputElement).files?.[0] ?? null;
  }

  compareDocuments(): void {
    if (!this.originalFile || !this.returnedFile) return;
    this.diffError = '';
    this.comparingDiff = true;
    const form = new FormData();
    form.append('original', this.originalFile);
    form.append('returned', this.returnedFile);
    this.http
      .post<DocumentDiffResult>(`${KONG_BASE}/api/documents/compare`, form, { headers: this.session.authHeaders() })
      .subscribe({
        next: (res) => {
          this.diffResult = res;
          this.comparingDiff = false;
        },
        error: (err: HttpErrorResponse) => {
          this.diffError = err.error?.detail ?? 'comparison failed';
          this.comparingDiff = false;
        }
      });
  }

  jumpToChange(id: number): void {
    // text-mode anchors are a real Angular [id] binding (untouched by any
    // sanitizer); html-mode anchors are baked into raw HTML rendered via
    // [innerHTML], where Angular's sanitizer strips `id` but keeps `class`
    // verbatim — so that path is a class instead (see document-reviewer).
    const el =
      document.getElementById('doc-change-' + id) ?? document.querySelector('.doc-change-' + id);
    if (!el) return;
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    el.classList.add('flash');
    setTimeout(() => el.classList.remove('flash'), 1500);
  }

  // --- collateral-reviewer ---

  onLegalFile(event: Event): void {
    this.legalFile = (event.target as HTMLInputElement).files?.[0] ?? null;
  }

  onPropertyFile(event: Event): void {
    this.propertyFile = (event.target as HTMLInputElement).files?.[0] ?? null;
  }

  compareCollateral(): void {
    if (!this.legalFile || !this.propertyFile) return;
    this.collateralError = '';
    this.comparingCollateral = true;
    const form = new FormData();
    form.append('legal', this.legalFile);
    form.append('property', this.propertyFile);
    this.http
      .post<CollateralResult>(`${KONG_BASE}/api/collateral/compare`, form, { headers: this.session.authHeaders() })
      .subscribe({
        next: (res) => {
          this.collateralResult = res;
          this.comparingCollateral = false;
        },
        error: (err: HttpErrorResponse) => {
          this.collateralError = err.error?.detail ?? 'comparison failed';
          this.comparingCollateral = false;
        }
      });
  }
}
