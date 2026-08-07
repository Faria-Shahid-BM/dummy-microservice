import { CommonModule } from '@angular/common';
import { Component, EventEmitter, Input, OnChanges, OnDestroy, Output, SimpleChanges } from '@angular/core';
import { DocgenJob, DocgenService } from '../docgen.service';

const POLL_INTERVAL_MS = 2000;

// Reusable polling status badge for any docgen job (extract/analyze/select/
// fill/template-version-analyze). Polling, not SSE — see the plan: docgen's
// job buffer is process-local and single-worker-only per MIGRATION_PLAN.md,
// so GET /api/jobs/{id} (DB-backed) is the production-correct choice.
@Component({
  selector: 'app-job-status',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="job-status" *ngIf="job as j">
      <span class="job-dot" [ngClass]="'job-' + j.status"></span>
      <span class="job-label">{{ label(j.status) }}</span>
      <span class="error" *ngIf="j.status === 'failed' && j.error">{{ j.error }}</span>
    </div>
  `
})
export class JobStatusComponent implements OnChanges, OnDestroy {
  @Input({ required: true }) jobId!: string;
  @Output() done = new EventEmitter<DocgenJob>();
  @Output() failed = new EventEmitter<DocgenJob>();

  job: DocgenJob | null = null;
  private timer: ReturnType<typeof setInterval> | null = null;

  constructor(private docgen: DocgenService) {}

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['jobId']) {
      this.stopPolling();
      this.job = null;
      this.poll();
      this.timer = setInterval(() => this.poll(), POLL_INTERVAL_MS);
    }
  }

  ngOnDestroy(): void {
    this.stopPolling();
  }

  private stopPolling(): void {
    if (this.timer !== null) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  private poll(): void {
    this.docgen.getJob(this.jobId).subscribe({
      next: (job) => {
        this.job = job;
        if (job.status === 'succeeded') {
          this.stopPolling();
          this.done.emit(job);
        } else if (job.status === 'failed' || job.status === 'cancelled') {
          this.stopPolling();
          this.failed.emit(job);
        }
      },
      error: () => {
        this.stopPolling();
      }
    });
  }

  label(status: string): string {
    switch (status) {
      case 'queued':
        return 'Queued…';
      case 'running':
        return 'Running…';
      case 'succeeded':
        return 'Done';
      case 'failed':
        return 'Failed';
      case 'cancelled':
        return 'Cancelled';
      default:
        return status;
    }
  }
}
