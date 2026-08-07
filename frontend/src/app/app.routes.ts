import { Routes } from '@angular/router';
import { adminGuard, authGuard, docgenGuard } from './auth.guard';

export const routes: Routes = [
  {
    path: 'login',
    loadComponent: () => import('./login/login.component').then((m) => m.LoginComponent)
  },
  {
    path: 'dashboard',
    loadComponent: () => import('./dashboard/dashboard.component').then((m) => m.DashboardComponent),
    canActivate: [authGuard]
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
  {
    path: 'docgen',
    loadComponent: () => import('./docgen/docgen-shell.component').then((m) => m.DocgenShellComponent),
    canActivate: [authGuard, docgenGuard],
    children: [
      { path: '', redirectTo: 'cases', pathMatch: 'full' },
      {
        path: 'cases',
        loadComponent: () => import('./docgen/cases/case-list.component').then((m) => m.CaseListComponent)
      },
      {
        path: 'cases/:caseId',
        loadComponent: () => import('./docgen/cases/case-detail.component').then((m) => m.CaseDetailComponent)
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
  { path: '', redirectTo: 'dashboard', pathMatch: 'full' },
  { path: '**', redirectTo: 'dashboard' }
];
