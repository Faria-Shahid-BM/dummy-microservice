import { CommonModule } from '@angular/common';
import { Component } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Router } from '@angular/router';
import { KNOWN_SERVICES, KONG_BASE, SessionService } from '../../session.service';

@Component({
  selector: 'app-admin-add-user',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './admin-add-user.component.html'
})
export class AdminAddUserComponent {
  knownServices = KNOWN_SERVICES;

  username = '';
  password = '';
  role = 'viewer';
  services: string[] = [];
  error = '';
  creating = false;

  constructor(private http: HttpClient, private session: SessionService, private router: Router) {}

  toggleService(service: string): void {
    const i = this.services.indexOf(service);
    if (i === -1) this.services.push(service);
    else this.services.splice(i, 1);
  }

  create(): void {
    this.error = '';
    this.creating = true;
    this.http
      .post(
        `${KONG_BASE}/api/auth/users`,
        { username: this.username, password: this.password, role: this.role, services: this.services },
        { headers: this.session.authHeaders() }
      )
      .subscribe({
        next: () => {
          this.creating = false;
          this.router.navigate(['/admin/users']);
        },
        error: (err: HttpErrorResponse) => {
          this.error = err.error?.detail ?? 'failed to create user';
          this.creating = false;
        }
      });
  }
}
