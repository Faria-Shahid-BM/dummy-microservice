import { CommonModule } from '@angular/common';
import { Component, EventEmitter, Input, Output } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { marked } from 'marked';

// Renders LLM-produced markdown (case text, credit analysis) as formatted
// text by default. Editing needs the raw markdown back, so the caller
// switches [editing] on (its own "Edit"/"Preview" button, next to the
// section's other actions) to swap in the plain textarea instead of trying
// to edit rendered HTML.
@Component({
  selector: 'app-markdown-field',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './markdown-field.component.html',
  styleUrl: './markdown-field.component.css'
})
export class MarkdownFieldComponent {
  @Input() value = '';
  @Input() rows = 8;
  @Input() disabled = false;
  @Input() editing = false;
  @Output() valueChange = new EventEmitter<string>();

  get renderedHtml(): string {
    if (!this.value.trim()) return '<p class="empty">No content.</p>';
    return marked.parse(this.value, { async: false });
  }

  onModelChange(next: string): void {
    this.value = next;
    this.valueChange.emit(next);
  }
}
