import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, catchError, of, tap } from 'rxjs';

export interface SessionInfo {
  username: string;
  scopes: string[];
}

// Empty = same-origin: the app calls /api/... relative to whatever host
// served it, and nginx reverse-proxies /api/* to Kong (see frontend/nginx.conf
// and DEPLOYMENT.md §4b). For local dev against `ng serve`, override to
// 'http://localhost' where Kong is published directly.
export const KONG_BASE = '';
// Matches the scopes issued by auth-service (see auth-service/main.py seed
// users) and required by each backend service via security.py's
// require_scope(). "admin" is just another scope, not a separate role.
export const KNOWN_SERVICES = [
  'collateral',
  'docdiff',
  'valuation',
  'insurance',
  'policy_qa',
  'docgen',
  'docgen_check',
  'admin'
];

@Injectable({ providedIn: 'root' })
export class SessionService {
  // The JWT itself never reaches this class — auth-service sets it as an
  // httpOnly cookie (see auth-service/main.py's /login), so JS can't read it
  // even if the page were compromised by XSS. Kong bridges that cookie into
  // an Authorization header for the upstream services (see kong.yml's
  // pre-function plugin). This is only the non-sensitive identity the UI
  // needs to decide what to show.
  session: SessionInfo | null = null;

  constructor(private http: HttpClient) {}

  login(username: string, password: string): Observable<SessionInfo> {
    return this.http
      .post<SessionInfo>(`${KONG_BASE}/api/auth/login`, { username, password })
      .pipe(tap((res) => (this.session = res)));
  }

  // A page refresh clears `session` above (in-memory only) but not the
  // cookie — authGuard calls this on the first navigation after a fresh
  // load to ask the server who the cookie belongs to, instead of assuming
  // the user needs to log in again.
  restore(): Observable<SessionInfo | null> {
    return this.http.get<SessionInfo>(`${KONG_BASE}/api/auth/me`).pipe(
      tap((res) => (this.session = res)),
      catchError(() => {
        this.session = null;
        return of(null);
      })
    );
  }

  logout(): void {
    this.session = null;
    this.http.post(`${KONG_BASE}/api/auth/logout`, {}).subscribe();
  }

  get isLoggedIn(): boolean {
    return this.session !== null;
  }

  get isAdmin(): boolean {
    return this.session?.scopes?.includes('admin') ?? false;
  }
}
