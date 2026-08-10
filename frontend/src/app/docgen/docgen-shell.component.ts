import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { DocgenService } from './docgen.service';

@Component({
  selector: 'app-docgen-shell',
  standalone: true,
  imports: [CommonModule, RouterLink, RouterLinkActive, RouterOutlet],
  templateUrl: './docgen-shell.component.html'
})
export class DocgenShellComponent implements OnInit {
  loadingProfile = true;
  profileError = '';

  constructor(public docgen: DocgenService) {}

  ngOnInit(): void {
    this.docgen.listProfiles().subscribe({
      next: (res) => {
        this.docgen.activeProfile = res.profiles.find((p) => !p.is_default) ?? res.profiles[0] ?? null;
        this.loadingProfile = false;
        if (!this.docgen.activeProfile) {
          this.profileError = 'No profile is available for this account.';
        }
      },
      error: () => {
        this.loadingProfile = false;
        this.profileError = 'Failed to load your profile.';
      }
    });
  }
}
