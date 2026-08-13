import { CommonModule } from '@angular/common';
import { AfterContentInit, Component, ContentChild, ContentChildren, Directive, Input, QueryList, TemplateRef } from '@angular/core';

// A column, declared by the caller as content INSIDE <app-data-table>:
//   <ng-template appColumn header="Name" let-row>{{ row.name }}</ng-template>
// Content children (unlike a component's own @ViewChild into a template
// nested under its own *ngIf) are guaranteed resolved by the time the table
// itself renders, however deeply the whole <app-data-table> is nested in
// the caller's conditionals — Angular projects and initializes content
// before the child component's first render, no extra change-detection
// cycle required. A @ViewChild-based version of this component had exactly
// that one-cycle-behind gap and rendered custom cells blank.
@Directive({ selector: 'ng-template[appColumn]', standalone: true })
export class TableColumnDirective<T = unknown> {
  @Input() header = '';
  @Input() cellClass = '';
  /** Optional explicit column width (e.g. '50%'), for a table whose columns
   * are too lopsided in content length for the browser's own auto-layout
   * guess to look reasonable. */
  @Input() width?: string;
  constructor(public template: TemplateRef<{ $implicit: T }>) {}
}

// A full-width row rendered right after a matching row, when `isExpanded`
// says so:
//   <ng-template appRowDetail let-row>...</ng-template>
@Directive({ selector: 'ng-template[appRowDetail]', standalone: true })
export class RowDetailDirective<T = unknown> {
  constructor(public template: TemplateRef<{ $implicit: T }>) {}
}

// The one `<table class="audit-table">` shell, reused everywhere a page
// renders "rows of records" — case lists, template lists, generated
// documents, selected documents. Callers own each column's content
// (including status badges, links, or action buttons) via a projected
// `appColumn` template; this component owns only the table markup, the
// empty state, row styling, and the optional expandable detail row.
@Component({
  selector: 'app-data-table',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './data-table.component.html'
})
export class DataTableComponent<T> implements AfterContentInit {
  @Input({ required: true }) rows: T[] = [];
  @Input() empty = 'No records yet.';
  /** CSS class(es) for a row, e.g. flagging one that needs attention. */
  @Input() rowClass?: (row: T) => string;
  @Input() isExpanded?: (row: T) => boolean;

  @ContentChildren(TableColumnDirective) private columnDefs!: QueryList<TableColumnDirective<T>>;
  @ContentChild(RowDetailDirective) rowDetail?: RowDetailDirective<T>;

  columns: TableColumnDirective<T>[] = [];

  ngAfterContentInit(): void {
    this.columns = this.columnDefs.toArray();
    this.columnDefs.changes.subscribe(() => (this.columns = this.columnDefs.toArray()));
  }
}
