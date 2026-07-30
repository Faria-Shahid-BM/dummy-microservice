import { Routes } from '@angular/router';
import { adminGuard, authGuard } from './auth.guard';

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
  { path: '', redirectTo: 'dashboard', pathMatch: 'full' },
  { path: '**', redirectTo: 'dashboard' }
];
