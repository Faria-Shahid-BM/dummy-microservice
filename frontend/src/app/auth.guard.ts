import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { map } from 'rxjs';
import { SessionService } from './session.service';

export const authGuard: CanActivateFn = () => {
  const session = inject(SessionService);
  const router = inject(Router);
  if (session.isLoggedIn) return true;
  // No in-memory session — e.g. a fresh page load. The JWT itself lives in
  // an httpOnly cookie the browser already sent, so ask the server who it
  // belongs to before assuming the user needs to log in again.
  return session.restore().pipe(
    map((info) => {
      if (info) return true;
      router.navigate(['/login']);
      return false;
    })
  );
};

export const adminGuard: CanActivateFn = () => {
  const session = inject(SessionService);
  const router = inject(Router);
  if (session.isAdmin) return true;
  router.navigate(['/dashboard']);
  return false;
};

// Route-level gate for a routed service (docgen, collateral, ...) — holding
// its scope (or 'admin') gets you in, otherwise back to the dashboard.
function scopeGuard(scope: string): CanActivateFn {
  return () => {
    const session = inject(SessionService);
    const router = inject(Router);
    const scopes = session.session?.scopes ?? [];
    if (scopes.includes(scope) || scopes.includes('admin')) return true;
    router.navigate(['/dashboard']);
    return false;
  };
}

export const docgenGuard = scopeGuard('docgen');
export const collateralGuard = scopeGuard('collateral');
export const valuationGuard = scopeGuard('valuation');
export const insuranceGuard = scopeGuard('insurance');
export const docdiffGuard = scopeGuard('docdiff');
