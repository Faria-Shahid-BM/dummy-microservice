import { CommonModule } from '@angular/common';
import { Component, Input } from '@angular/core';

export interface StageDef {
  key: string;
  label: string;
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
      </div>
    </div>
  `
})
export class StageProgressComponent {
  @Input() steps: StageDef[] = [];
  @Input() currentKey: string | null = null;
  @Input() detail: string | null = null;
  @Input() complete = false;

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
