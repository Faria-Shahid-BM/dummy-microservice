import { CommonModule } from '@angular/common';
import { Component, Input } from '@angular/core';

// Recursively renders an arbitrary JSON-ish value (object/array/scalar) as
// nested sections. Used for backend results that don't have a fixed shape
// worth a bespoke table yet (valuation, insurance).
@Component({
  selector: 'app-json-view',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './json-view.component.html',
  styleUrl: './json-view.component.css'
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
