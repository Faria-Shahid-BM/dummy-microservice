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
  templateUrl: './stage-progress.component.html',
  styleUrl: './stage-progress.component.css'
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
