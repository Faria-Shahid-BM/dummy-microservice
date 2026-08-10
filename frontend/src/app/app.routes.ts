import { Routes } from '@angular/router';
import { adminGuard, authGuard, collateralGuard, docdiffGuard, docgenGuard, insuranceGuard, valuationGuard } from './auth.guard';
import { CASE_SERVICE } from './shared/case.service';
import { CollateralService } from './collateral/collateral.service';
import { ValuationService } from './valuation/valuation.service';
import { InsuranceService } from './insurance/insurance.service';
import { DocdiffService } from './docdiff/docdiff.service';

export const routes: Routes = [
  {
    path: 'login',
    loadComponent: () => import('./login/login.component').then((m) => m.LoginComponent)
  },
  {
    // Empty path so it wraps /dashboard and every routed service:
    // AppShellComponent mounts the services sidebar once and keeps it on
    // screen while its <router-outlet> swaps between them, instead of each
    // route bringing its own full-page shell and replacing the sidebar.
    path: '',
    loadComponent: () => import('./shell/app-shell.component').then((m) => m.AppShellComponent),
    canActivate: [authGuard],
    children: [
      { path: '', redirectTo: 'dashboard', pathMatch: 'full' },
      {
        path: 'dashboard',
        loadComponent: () => import('./dashboard/dashboard.component').then((m) => m.DashboardComponent)
      },
      {
        path: 'docgen',
        loadComponent: () => import('./docgen/docgen-shell.component').then((m) => m.DocgenShellComponent),
        canActivate: [docgenGuard],
        children: [
          { path: '', redirectTo: 'cases', pathMatch: 'full' },
          {
            path: 'cases',
            loadComponent: () => import('./docgen/cases/case-list.component').then((m) => m.CaseListComponent)
          },
          {
            path: 'cases/:caseId',
            loadComponent: () =>
              import('./docgen/cases/case-detail.component').then((m) => m.CaseDetailComponent)
          },
          {
            path: 'templates',
            loadComponent: () =>
              import('./docgen/templates/template-list.component').then((m) => m.TemplateListComponent)
          },
          {
            path: 'templates/:tid',
            loadComponent: () =>
              import('./docgen/templates/template-detail.component').then((m) => m.TemplateDetailComponent)
          },
          {
            path: 'approvals',
            loadComponent: () =>
              import('./docgen/approvals/approvals-list.component').then((m) => m.ApprovalsListComponent)
          }
        ]
      },
      // The four stateless review services (collateral/valuation/insurance/
      // docdiff) all share the identical Cases shape (case_store.py) — same
      // list page (shared/case-list.component, bound per-route via
      // CASE_SERVICE), only the detail page differs (upload slot count,
      // result rendering). Component-less parent: no wrapping shell needed,
      // so children render straight into AppShellComponent's
      // <router-outlet> and the sidebar stays visible, same as docgen.
      {
        path: 'collateral',
        canActivate: [collateralGuard],
        children: [
          { path: '', redirectTo: 'cases', pathMatch: 'full' },
          {
            path: 'cases',
            providers: [{ provide: CASE_SERVICE, useExisting: CollateralService }],
            loadComponent: () => import('./shared/case-list.component').then((m) => m.CaseListComponent)
          },
          {
            path: 'cases/:caseId',
            loadComponent: () =>
              import('./collateral/cases/case-detail.component').then((m) => m.CaseDetailComponent)
          }
        ]
      },
      {
        path: 'valuation',
        canActivate: [valuationGuard],
        children: [
          { path: '', redirectTo: 'cases', pathMatch: 'full' },
          {
            path: 'cases',
            providers: [{ provide: CASE_SERVICE, useExisting: ValuationService }],
            loadComponent: () => import('./shared/case-list.component').then((m) => m.CaseListComponent)
          },
          {
            path: 'cases/:caseId',
            loadComponent: () =>
              import('./valuation/cases/case-detail.component').then((m) => m.CaseDetailComponent)
          }
        ]
      },
      {
        path: 'insurance',
        canActivate: [insuranceGuard],
        children: [
          { path: '', redirectTo: 'cases', pathMatch: 'full' },
          {
            path: 'cases',
            providers: [{ provide: CASE_SERVICE, useExisting: InsuranceService }],
            loadComponent: () => import('./shared/case-list.component').then((m) => m.CaseListComponent)
          },
          {
            path: 'cases/:caseId',
            loadComponent: () =>
              import('./insurance/cases/case-detail.component').then((m) => m.CaseDetailComponent)
          }
        ]
      },
      {
        path: 'docdiff',
        canActivate: [docdiffGuard],
        children: [
          { path: '', redirectTo: 'cases', pathMatch: 'full' },
          {
            path: 'cases',
            providers: [{ provide: CASE_SERVICE, useExisting: DocdiffService }],
            loadComponent: () => import('./shared/case-list.component').then((m) => m.CaseListComponent)
          },
          {
            path: 'cases/:caseId',
            loadComponent: () =>
              import('./docdiff/cases/case-detail.component').then((m) => m.CaseDetailComponent)
          }
        ]
      }
    ]
  },
  {
    path: 'admin',
    loadComponent: () => import('./admin/admin-shell.component').then((m) => m.AdminShellComponent),
    canActivate: [authGuard, adminGuard],
    children: [
      { path: '', redirectTo: 'audit', pathMatch: 'full' },
      {
        path: 'audit',
        loadComponent: () => import('./admin/audit/admin-audit.component').then((m) => m.AdminAuditComponent)
      },
      {
        path: 'audit/:entryId',
        loadComponent: () =>
          import('./admin/audit/admin-audit-detail.component').then((m) => m.AdminAuditDetailComponent)
      },
      {
        path: 'users',
        loadComponent: () => import('./admin/users/admin-users.component').then((m) => m.AdminUsersComponent)
      },
      {
        path: 'users/new',
        loadComponent: () =>
          import('./admin/add-user/admin-add-user.component').then((m) => m.AdminAddUserComponent)
      }
    ]
  },
  { path: '**', redirectTo: 'dashboard' }
];
