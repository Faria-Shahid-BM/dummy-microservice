import { CommonModule } from '@angular/common';
import { Component } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { HttpErrorResponse } from '@angular/common/http';
import { ActivatedRoute, Router } from '@angular/router';
import { SessionService } from '../session.service';

@Component({
  selector: 'app-login',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './login.component.html'
})
export class LoginComponent {
  username = '';
  password = '';
  loginError = '';
  loggingIn = false;

  constructor(
    private session: SessionService,
    private router: Router,
    route: ActivatedRoute
  ) {
    // Set by the auth-expiry interceptor when a request was rejected because the
    // token had expired — otherwise the redirect looks like an unexplained logout.
    if (route.snapshot.queryParamMap.get('expired')) {
      this.loginError = 'Your session has expired. Please sign in again.';
    }
  }

  login(): void {
    this.loginError = '';
    this.loggingIn = true;
    this.session.login(this.username, this.password).subscribe({
      next: () => {
        this.loggingIn = false;
        this.router.navigate(['/dashboard']);
      },
      error: (err: HttpErrorResponse) => {
        this.loginError = err.error?.detail ?? 'login failed';
        this.loggingIn = false;
      }
    });
  }
}
