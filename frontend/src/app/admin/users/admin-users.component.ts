import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { RouterLink } from '@angular/router';
import { KNOWN_SERVICES, KONG_BASE, SessionService } from '../../session.service';

interface ManagedUser {
  username: string;
  role: string;
  services: string[];
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

  toggleUserService(user: ManagedUser, service: string): void {
    const services = user.services.includes(service)
      ? user.services.filter((s) => s !== service)
      : [...user.services, service];

    this.http
      .put<ManagedUser>(`${KONG_BASE}/api/auth/users/${user.username}/services`, { services }, { headers: this.session.authHeaders() })
      .subscribe({
        next: (updated) => {
          user.services = updated.services;
        },
        error: (err: HttpErrorResponse) => {
          this.error = err.error?.detail ?? 'failed to update services';
        }
      });
  }
}
