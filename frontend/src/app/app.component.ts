import { CommonModule } from '@angular/common';
import { Component } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';

interface LoginResponse {
  token: string;
  username: string;
  role: string;
  services: string[];
}

interface LogEntry {
  time: string;
  label: string;
  status: number | string;
  body: string;
  kind: 'success' | 'unauthorized' | 'forbidden' | 'error';
}

interface AuditEntry {
  user_id: string;
  service: string;
  action: string;
  resource: string | null;
  timestamp: string;
}

const KONG_BASE = 'http://localhost';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './app.component.html',
  styleUrl: './app.component.css'
})
export class AppComponent {
  username = '';
  password = '';
  loginError = '';
  loggingIn = false;

  session: LoginResponse | null = null;
  log: LogEntry[] = [];

  auditEntries: AuditEntry[] = [];
  auditError = '';
  loadingAudit = false;

  constructor(private http: HttpClient) {}

  login(): void {
    this.loginError = '';
    this.loggingIn = true;
    this.http.post<LoginResponse>(`${KONG_BASE}/api/auth/login`, {
      username: this.username,
      password: this.password
    }).subscribe({
      next: (res) => {
        this.session = res;
        this.password = '';
        this.loggingIn = false;
      },
      error: (err: HttpErrorResponse) => {
        this.loginError = err.error?.detail ?? 'login failed';
        this.loggingIn = false;
      }
    });
  }

  logout(): void {
    this.session = null;
    this.username = '';
    this.auditEntries = [];
    this.auditError = '';
  }

  loadAudit(): void {
    if (!this.session) return;
    this.auditError = '';
    this.loadingAudit = true;
    this.http.get<AuditEntry[]>(`${KONG_BASE}/api/audit`, {
      headers: { Authorization: `Bearer ${this.session.token}` }
    }).subscribe({
      next: (entries) => {
        this.auditEntries = entries;
        this.loadingAudit = false;
      },
      error: (err: HttpErrorResponse) => {
        this.auditError = err.error?.detail ?? 'failed to load audit log';
        this.loadingAudit = false;
      }
    });
  }

  call(path: string, label: string): void {
    const headers: Record<string, string> = {};
    if (this.session) headers['Authorization'] = `Bearer ${this.session.token}`;

    this.http.get(`${KONG_BASE}${path}`, { headers, observe: 'response' }).subscribe({
      next: (res) => this.pushLog(label, res.status, JSON.stringify(res.body)),
      error: (err: HttpErrorResponse) =>
        this.pushLog(label, err.status, JSON.stringify(err.error))
    });
  }

  clearLog(): void {
    this.log = [];
  }

  private pushLog(label: string, status: number | string, body: string): void {
    let kind: LogEntry['kind'] = 'error';
    if (status === 200) kind = 'success';
    else if (status === 401) kind = 'unauthorized';
    else if (status === 403) kind = 'forbidden';

    this.log.unshift({
      time: new Date().toLocaleTimeString(),
      label,
      status,
      body,
      kind
    });
  }
}
