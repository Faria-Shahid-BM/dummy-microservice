import { Component } from '@angular/core';
import { Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { SessionService } from '../session.service';

@Component({
  selector: 'app-admin-shell',
  standalone: true,
  imports: [RouterLink, RouterLinkActive, RouterOutlet],
  templateUrl: './admin-shell.component.html'
})
export class AdminShellComponent {
  constructor(public session: SessionService, private router: Router) {}

  logout(): void {
    this.session.logout();
    this.router.navigate(['/login']);
  }
}
