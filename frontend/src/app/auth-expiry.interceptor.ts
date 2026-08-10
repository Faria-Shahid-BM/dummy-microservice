import { HttpErrorResponse, HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';
import { Router } from '@angular/router';
import { catchError, throwError } from 'rxjs';
import { SessionService } from './session.service';

// Kong rejects expired tokens with 401. Without this, an expired session leaves
// the user on a dashboard where every request fails with no explanation — so
// clear the session and send them back to sign in.
//
// The login request is excluded: it returns 401 for a wrong password, which the
// login form reports itself and which is not an expired session.
export const authExpiryInterceptor: HttpInterceptorFn = (req, next) => {
  const session = inject(SessionService);
  const router = inject(Router);

  return next(req).pipe(
    catchError((err: unknown) => {
      const isLoginRequest = req.url.includes('/api/auth/login');
      if (
        err instanceof HttpErrorResponse &&
        err.status === 401 &&
        !isLoginRequest &&
        session.isLoggedIn
      ) {
        session.logout();
        router.navigate(['/login'], { queryParams: { expired: '1' } });
      }
      return throwError(() => err);
    })
  );
};
