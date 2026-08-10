import { CommonModule } from '@angular/common';
import { Component } from '@angular/core';
import { Router, RouterLink, RouterOutlet } from '@angular/router';
import { SessionService } from '../session.service';
import { ServiceCatalogService, ServiceMeta } from '../dashboard/service-catalog.service';
import { SidebarToggleService } from './sidebar-toggle.service';

// Mounted once for the whole /dashboard <-> /docgen area (see app.routes.ts)
// so the services sidebar stays put while those routes swap the content
// next to it, instead of disappearing on every navigation.
@Component({
  selector: 'app-shell',
  standalone: true,
  imports: [CommonModule, RouterLink, RouterOutlet],
  templateUrl: './app-shell.component.html'
})
export class AppShellComponent {
  constructor(
    public session: SessionService,
    public catalog: ServiceCatalogService,
    public sidebarToggle: SidebarToggleService,
    private router: Router
  ) {}

  // Non-route services render as an inline panel on the dashboard itself,
  // so picking one from the sidebar also has to get you back there.
  selectKind(meta: ServiceMeta): void {
    this.catalog.select(meta);
    this.router.navigateByUrl('/dashboard');
  }
}
