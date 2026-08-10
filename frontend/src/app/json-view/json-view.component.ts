import { CommonModule } from '@angular/common';
import { Component, Input } from '@angular/core';

// Recursively renders an arbitrary JSON-ish value (object/array/scalar) as
// nested sections. Used for backend results that don't have a fixed shape
// worth a bespoke table yet (valuation, insurance).
@Component({
  selector: 'app-json-view',
  standalone: true,
  imports: [CommonModule],
  template: `
    <table class="json-table" *ngIf="kind === 'object'">
      <tr *ngFor="let entry of entries">
        <th>{{ entry[0] }}</th>
        <td><app-json-view [value]="entry[1]"></app-json-view></td>
      </tr>
    </table>

    <ul class="json-list" *ngIf="kind === 'array'">
      <li *ngFor="let item of value">
        <app-json-view [value]="item"></app-json-view>
      </li>
    </ul>

    <span class="empty" *ngIf="kind === 'empty'">&mdash;</span>

    <span *ngIf="kind === 'scalar'">{{ value }}</span>
  `
})
export class JsonViewComponent {
  @Input() value: any;

  get kind(): 'object' | 'array' | 'empty' | 'scalar' {
    if (this.value === null || this.value === undefined || this.value === '') return 'empty';
    if (Array.isArray(this.value)) return this.value.length ? 'array' : 'empty';
    if (typeof this.value === 'object') return Object.keys(this.value).length ? 'object' : 'empty';
    return 'scalar';
  }

  get entries(): [string, any][] {
    return Object.entries(this.value ?? {});
  }
}
