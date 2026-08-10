import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { Router } from '@angular/router';
import { SessionService } from '../session.service';
import { DocgenNotification, DocgenService } from '../docgen/docgen.service';

// App-wide top bar: profile circle + logout, and the notifications bell.
// Mounted once in app.component.html (not per-shell) so identity/logout and
// notifications aren't duplicated across the dashboard/admin/docgen shells.
@Component({
  selector: 'app-topbar',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './topbar.component.html'
})
export class TopbarComponent implements OnInit {
  showNotifications = false;
  showProfile = false;

  notifications: DocgenNotification[] = [];
  unreadCount = 0;

  constructor(public session: SessionService, private docgen: DocgenService, private router: Router) {}

  // Notifications currently only exist behind docgen-service — hide the
  // bell entirely for accounts that can't reach it rather than show an
  // always-empty control. Checkers need it most: it's how they learn that
  // something was submitted for their approval.
  get hasDocgenAccess(): boolean {
    return this.session.canUseDocgen;
  }

  get initials(): string {
    const name = this.session.session?.username ?? '';
    return name.slice(0, 2).toUpperCase();
  }

  ngOnInit(): void {
    if (this.hasDocgenAccess) {
      this.loadNotifications();
    }
  }

  loadNotifications(): void {
    this.docgen.listNotifications().subscribe({
      next: (res) => {
        this.notifications = res.notifications;
        this.unreadCount = res.unread_count;
      },
      error: () => {
        /* non-critical — notifications are a nice-to-have, don't surface an error */
      }
    });
  }

  toggleNotifications(): void {
    this.showNotifications = !this.showNotifications;
    this.showProfile = false;
  }

  toggleProfile(): void {
    this.showProfile = !this.showProfile;
    this.showNotifications = false;
  }

  markAllRead(): void {
    this.docgen.markNotificationsRead().subscribe({
      next: () => this.loadNotifications(),
      error: () => {
        /* non-critical */
      }
    });
  }

  logout(): void {
    this.session.logout();
    this.router.navigate(['/login']);
  }
}
