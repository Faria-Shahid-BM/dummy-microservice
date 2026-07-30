import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, tap } from 'rxjs';

export interface LoginResponse {
  token: string;
  username: string;
  role: string;
  services: string[];
}

// Empty = same-origin: the app calls /api/... relative to whatever host
// served it, and nginx reverse-proxies /api/* to Kong (see frontend/nginx.conf
// and DEPLOYMENT.md §4b). For local dev against `ng serve`, override to
// 'http://localhost' where Kong is published directly.
export const KONG_BASE = '';
export const KNOWN_SERVICES = ['document-reviewer', 'collateral-reviewer'];

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
    return this.session?.role === 'admin';
  }

  authHeaders(): Record<string, string> {
    return this.session ? { Authorization: `Bearer ${this.session.token}` } : {};
  }
}
