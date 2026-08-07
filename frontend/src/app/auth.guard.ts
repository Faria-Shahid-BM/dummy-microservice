import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { SessionService } from './session.service';

export const authGuard: CanActivateFn = () => {
  const session = inject(SessionService);
  const router = inject(Router);
  if (session.isLoggedIn) return true;
  router.navigate(['/login']);
  return false;
};

export const adminGuard: CanActivateFn = () => {
  const session = inject(SessionService);
  const router = inject(Router);
  if (session.isAdmin) return true;
  router.navigate(['/dashboard']);
  return false;
};

export const docgenGuard: CanActivateFn = () => {
  const session = inject(SessionService);
  const router = inject(Router);
  const scopes = session.session?.scopes ?? [];
  if (scopes.includes('docgen') || scopes.includes('admin')) return true;
  router.navigate(['/dashboard']);
  return false;
};
