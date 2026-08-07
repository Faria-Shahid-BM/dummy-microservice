import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, tap } from 'rxjs';

export interface LoginResponse {
  token: string;
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
  session: LoginResponse | null = null;

  constructor(private http: HttpClient) {}

  login(username: string, password: string): Observable<LoginResponse> {
    return this.http
      .post<LoginResponse>(`${KONG_BASE}/api/auth/login`, { username, password })
      .pipe(tap((res) => (this.session = res)));
  }

  logout(): void {
    this.session = null;
  }

  get isLoggedIn(): boolean {
    return this.session !== null;
  }

  get isAdmin(): boolean {
    return this.session?.scopes?.includes('admin') ?? false;
  }

  authHeaders(): Record<string, string> {
    return this.session ? { Authorization: `Bearer ${this.session.token}` } : {};
  }
}
