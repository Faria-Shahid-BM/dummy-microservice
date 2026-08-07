import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { FormsModule } from '@angular/forms';
import { SessionService } from '../../session.service';
import { Approval, DocgenService } from '../docgen.service';

@Component({
  selector: 'app-approvals-list',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './approvals-list.component.html'
})
export class ApprovalsListComponent implements OnInit {
  approvals: Approval[] = [];
  loading = false;
  error = '';

  stateFilter = 'pending';
  subjectTypeFilter = '';

  commentByApproval: Record<string, string> = {};
  actingOn: Record<string, boolean> = {};

  constructor(private docgen: DocgenService, public session: SessionService) {}

  get profileId(): string | null {
    return this.docgen.activeProfile?.id ?? null;
  }

  get canDecide(): boolean {
    const scopes = this.session.session?.scopes ?? [];
    return scopes.includes('docgen_check') || scopes.includes('admin');
  }

  // Best-effort client-side hint only — `maker` is docgen's display name for
  // the submitter, which for this auto-provisioned user model is just their
  // username. The server enforces maker≠checker regardless (with a clear
  // 403) even if this heuristic is ever wrong, so it's safe to be approximate.
  isOwnSubmission(a: Approval): boolean {
    return a.maker === this.session.session?.username;
  }

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    if (!this.profileId) return;
    this.error = '';
    this.loading = true;
    this.docgen
      .listApprovals(this.profileId, this.stateFilter || undefined, this.subjectTypeFilter || undefined)
      .subscribe({
        next: (res) => {
          this.approvals = res.approvals;
          this.loading = false;
        },
        error: (err: HttpErrorResponse) => {
          this.error = err.error?.detail ?? 'failed to load approvals';
          this.loading = false;
        }
      });
  }

  submit(a: Approval): void {
    this.actingOn[a.id] = true;
    this.error = '';
    this.docgen.submitApproval(a.id).subscribe({
      next: () => {
        this.actingOn[a.id] = false;
        this.load();
      },
      error: (err: HttpErrorResponse) => {
        this.error = err.error?.detail ?? 'failed to submit';
        this.actingOn[a.id] = false;
      }
    });
  }

  decide(a: Approval, approve: boolean): void {
    const comment = this.commentByApproval[a.id] ?? '';
    this.actingOn[a.id] = true;
    this.error = '';
    this.docgen.decideApproval(a.id, approve, comment).subscribe({
      next: () => {
        this.actingOn[a.id] = false;
        delete this.commentByApproval[a.id];
        this.load();
      },
      error: (err: HttpErrorResponse) => {
        this.error = err.error?.detail ?? 'failed to record decision';
        this.actingOn[a.id] = false;
      }
    });
  }
}
