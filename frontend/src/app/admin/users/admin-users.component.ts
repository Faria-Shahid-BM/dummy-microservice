import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { RouterLink } from '@angular/router';
import { KNOWN_SERVICES, KONG_BASE, SessionService } from '../../session.service';

interface ManagedUser {
  username: string;
  scopes: string[];
}

@Component({
  selector: 'app-admin-users',
  standalone: true,
  imports: [CommonModule, RouterLink],
  templateUrl: './admin-users.component.html'
})
export class AdminUsersComponent implements OnInit {
  knownServices = KNOWN_SERVICES;
  users: ManagedUser[] = [];
  error = '';
  loading = false;

  constructor(private http: HttpClient, private session: SessionService) {}

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    this.error = '';
    this.loading = true;
    this.http.get<ManagedUser[]>(`${KONG_BASE}/api/auth/users`, { headers: this.session.authHeaders() }).subscribe({
      next: (users) => {
        this.users = users;
        this.loading = false;
      },
      error: (err: HttpErrorResponse) => {
        this.error = err.error?.detail ?? 'failed to load users';
        this.loading = false;
      }
    });
  }

  toggleUserService(user: ManagedUser, scope: string): void {
    let scopes = user.scopes.includes(scope)
      ? user.scopes.filter((s) => s !== scope)
      : [...user.scopes, scope];

    // "docgen_check" is the checker role *within* docgen, not access to it, so
    // the two travel together: auth-service adds "docgen" whenever
    // "docgen_check" is granted, and removing docgen access here must take the
    // checker role with it — otherwise the server would just re-add docgen and
    // the checkbox would look stuck.
    if (scope === 'docgen' && !scopes.includes('docgen')) {
      scopes = scopes.filter((s) => s !== 'docgen_check');
    }

    this.http
      .put<ManagedUser>(`${KONG_BASE}/api/auth/users/${user.username}/scopes`, { scopes }, { headers: this.session.authHeaders() })
      .subscribe({
        next: (updated) => {
          user.scopes = updated.scopes;
        },
        error: (err: HttpErrorResponse) => {
          this.error = err.error?.detail ?? 'failed to update scopes';
        }
      });
  }
}
