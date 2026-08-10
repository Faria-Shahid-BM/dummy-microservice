import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { Router } from '@angular/router';
import { SessionService } from '../session.service';
import { DocgenNotification, DocgenService } from '../docgen/docgen.service';
import { SidebarToggleService } from '../shell/sidebar-toggle.service';

// App-wide top bar: profile circle + logout, the notifications bell, and the
// sidebar collapse toggle. Mounted once in app.component.html (not
// per-shell) so identity/logout and notifications aren't duplicated across
// the dashboard/admin/docgen shells — the toggle itself only does anything
// on /dashboard and /docgen (see AppShellComponent), hence showSidebarToggle.
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

  constructor(
    public session: SessionService,
    public sidebarToggle: SidebarToggleService,
    private docgen: DocgenService,
    private router: Router
  ) {}

  get showSidebarToggle(): boolean {
    const url = this.router.url;
    return url.startsWith('/dashboard') || url.startsWith('/docgen');
  }

  // Notifications currently only exist behind docgen-service — hide the
  // bell entirely for accounts that can't reach it rather than show an
  // always-empty control.
  get hasDocgenAccess(): boolean {
    const scopes = this.session.session?.scopes ?? [];
    return scopes.includes('docgen') || scopes.includes('admin');
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
