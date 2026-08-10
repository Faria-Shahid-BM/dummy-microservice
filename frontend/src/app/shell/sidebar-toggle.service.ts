import { Injectable } from '@angular/core';

// Shared between TopbarComponent (the hamburger button lives in the
// app-wide top bar, mounted once) and AppShellComponent (the sidebar it
// collapses, mounted only on /dashboard and /docgen) — the two aren't in a
// parent/child relationship, so this is their only line of communication.
@Injectable({ providedIn: 'root' })
export class SidebarToggleService {
  collapsed = false;

  toggle(): void {
    this.collapsed = !this.collapsed;
  }
}
