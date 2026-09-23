import { CommonModule } from '@angular/common';
import {
  Component,
  ElementRef,
  EventEmitter,
  HostListener,
  Input,
  Output,
  ViewChild
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { CatalogueModel } from '../../configuration/configuration.service';

/**
 * A model id field: type freely, or pick from the provider's catalogue.
 *
 * A native `<datalist>` was the obvious first answer and the wrong one. Its
 * popup is browser chrome — it can't be styled to match anything, it shows
 * only the raw id, and it renders its own dropdown marker inside the field.
 * The list here is ordinary markup, so a row can carry what actually helps
 * the choice: the id to type, the provider's own name for it, and whether it
 * can read an image.
 *
 * Free text on purpose. The catalogue has no embedding models in it at all
 * and may not describe this deployment's gateway, so it suggests and warns
 * but never constrains — see config-service's catalogue().
 */
@Component({
  selector: 'app-model-picker',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './model-picker.component.html',
  styleUrl: './model-picker.component.css'
})
export class ModelPickerComponent {
  @Input({ required: true }) value = '';
  @Output() valueChange = new EventEmitter<string>();

  @Input() models: CatalogueModel[] = [];
  @Input() placeholder = '';
  @Input() disabled = false;
  @Input() ariaLabel = '';
  /** Unique per instance, so the listbox and its options get stable ids. */
  @Input({ required: true }) pickerId = '';

  @ViewChild('field') field?: ElementRef<HTMLInputElement>;

  open = false;
  /** Which row the keyboard is on; -1 means none. */
  active = -1;

  // 444 models is more DOM than any list needs at once, and more than anyone
  // reads. Filtering usually cuts it to a handful; this caps the rest.
  private static readonly LIMIT = 40;

  constructor(private host: ElementRef<HTMLElement>) {}

  get matches(): CatalogueModel[] {
    const needle = this.value.trim().toLowerCase();
    const pool = needle
      ? this.models.filter(
          (m) => m.id.toLowerCase().includes(needle) || m.name.toLowerCase().includes(needle)
        )
      : this.models;
    return pool.slice(0, ModelPickerComponent.LIMIT);
  }

  /** How many were left out, so a short list never reads as the whole list. */
  get hiddenCount(): number {
    const needle = this.value.trim().toLowerCase();
    const total = needle
      ? this.models.filter(
          (m) => m.id.toLowerCase().includes(needle) || m.name.toLowerCase().includes(needle)
        ).length
      : this.models.length;
    return Math.max(0, total - ModelPickerComponent.LIMIT);
  }

  onInput(next: string): void {
    this.value = next;
    this.valueChange.emit(next);
    this.active = -1;
    if (this.models.length) this.open = true;
  }

  toggle(): void {
    if (this.disabled || !this.models.length) return;
    this.open = !this.open;
    this.active = -1;
    if (this.open) this.field?.nativeElement.focus();
  }

  choose(model: CatalogueModel): void {
    this.value = model.id;
    this.valueChange.emit(model.id);
    this.open = false;
    this.active = -1;
    this.field?.nativeElement.focus();
  }

  onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      // Only swallow Escape when it had something to close, so it still
      // reaches anything outside that also listens for it.
      if (this.open) {
        this.open = false;
        event.stopPropagation();
      }
      return;
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      if (!this.models.length) return;
      event.preventDefault();
      if (!this.open) {
        this.open = true;
        this.active = 0;
        return;
      }
      const count = this.matches.length;
      if (!count) return;
      const step = event.key === 'ArrowDown' ? 1 : -1;
      this.active = (this.active + step + count) % count;
      this.scrollActiveIntoView();
      return;
    }
    if (event.key === 'Enter' && this.open && this.active >= 0) {
      // Only when a row is highlighted — otherwise Enter belongs to the form,
      // and typing an id the catalogue doesn't list must stay possible.
      const model = this.matches[this.active];
      if (model) {
        event.preventDefault();
        this.choose(model);
      }
    }
  }

  private scrollActiveIntoView(): void {
    queueMicrotask(() => {
      const el = this.host.nativeElement.querySelector<HTMLElement>('.picker-option.is-active');
      el?.scrollIntoView({ block: 'nearest' });
    });
  }

  /** Clicking anywhere else closes it, the way every other menu behaves. */
  @HostListener('document:pointerdown', ['$event'])
  onDocumentPointerDown(event: PointerEvent): void {
    if (!this.open) return;
    if (!this.host.nativeElement.contains(event.target as Node)) this.open = false;
  }

  optionId(index: number): string {
    return `${this.pickerId}-option-${index}`;
  }

  trackById = (_: number, model: CatalogueModel) => model.id;
}
