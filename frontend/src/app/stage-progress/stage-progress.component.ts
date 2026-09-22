import { CommonModule } from '@angular/common';
import { Component, Input } from '@angular/core';

import { TokenUsage } from '../sse.util';

export interface StageDef {
  key: string;
  label: string;
}

// Sub-second steps are the deterministic ones; showing "0s" for those would
// read as "not measured" rather than "instant".
function formatMs(ms: number): string {
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  const minutes = Math.floor(ms / 60000);
  return `${minutes}m ${Math.round((ms % 60000) / 1000)}s`;
}

// A vertical step checklist for the LLM review pipelines (collateral,
// valuation, insurance). Each engine emits its stages strictly in order via
// an SSE "event" payload like {"stage": "extract_fields", ...} — receiving a
// given stage's event means every earlier stage in `steps` is implicitly
// done, so the whole checklist can be driven off just `currentKey`.
@Component({
  selector: 'app-stage-progress',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="stage-progress">
      <div
        class="stage-step"
        *ngFor="let step of steps; let i = index"
        [class.done]="isDone(i)"
        [class.active]="isActive(i)"
      >
        <span class="stage-dot">
          <ng-container *ngIf="isDone(i)">&#10003;</ng-container>
        </span>
        <span class="stage-label">{{ step.label }}</span>
        <span class="stage-detail" *ngIf="isActive(i) && detail">{{ detail }}</span>
        <span class="stage-cost" *ngIf="tokensFor(step.key) as tokens">
          <span class="stage-time" *ngIf="timeFor(step.key) as elapsed">{{ elapsed }}</span>
          <span class="stage-tokens">{{ tokens }}</span>
        </span>
      </div>
      <div class="stage-total" *ngIf="usageTotal && usageTotal.total > 0">
        <span class="stage-label">Total</span>
        <span class="stage-cost">
          <span class="stage-time" *ngIf="totalTime() as elapsed">{{ elapsed }}</span>
          <span class="stage-tokens">{{ usageTotal.total | number }} tokens</span>
        </span>
      </div>
    </div>
  `
})
export class StageProgressComponent {
  @Input() steps: StageDef[] = [];
  @Input() currentKey: string | null = null;
  @Input() detail: string | null = null;
  @Input() complete = false;
  @Input() usageByStep: Record<string, TokenUsage> = {};
  @Input() usageTotal: TokenUsage | null = null;
  @Input() msByStep: Record<string, number> = {};

  /** Null until the step reports, so an unreached step shows nothing rather
   *  than a zero it hasn't earned. */
  tokensFor(key: string): string | null {
    const spent = this.usageByStep[key];
    if (!spent) return null;
    return spent.total === 0
      ? 'no LLM call'
      : `${spent.total.toLocaleString()} tokens`;
  }

  timeFor(key: string): string | null {
    const ms = this.msByStep[key];
    return ms === undefined ? null : formatMs(ms);
  }

  totalTime(): string | null {
    const values = Object.values(this.msByStep);
    if (!values.length) return null;
    return formatMs(values.reduce((sum, ms) => sum + ms, 0));
  }

  private currentIndex(): number {
    if (this.complete) return this.steps.length;
    if (!this.currentKey) return -1;
    return this.steps.findIndex((s) => s.key === this.currentKey);
  }

  isDone(i: number): boolean {
    return this.complete || i < this.currentIndex();
  }

  isActive(i: number): boolean {
    return !this.complete && i === this.currentIndex();
  }
}
