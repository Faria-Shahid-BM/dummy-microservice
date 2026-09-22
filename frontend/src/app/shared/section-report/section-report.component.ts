import { CommonModule } from '@angular/common';
import { Component, Input } from '@angular/core';
import { ClampTextComponent } from '../clamp-text/clamp-text.component';
import { JsonViewComponent } from '../../json-view/json-view.component';
import { ItemShape, ReportSpec, ReportStat, Tone, toneOf } from './report-spec';

/** One label/value pair in a section's field grid. */
export interface ReportField {
  label: string;
  value: unknown;
  /** Render the value as a toned status chip. */
  chip?: boolean;
}

/** One entry of an array-valued section (an issue, a gap, a recommendation). */
export interface ReportItem {
  id?: string;
  title: string;
  badge?: string;
  fields: ReportField[];
}

export interface ComplianceRow {
  requirement: string;
  status: string;
  issues: string;
}

type SectionKind = 'summary' | 'text' | 'fields' | 'compliance' | 'items' | 'raw';

export interface ReportSection {
  key: string;
  label: string;
  kind: SectionKind;
  /** Shown on the tab, for sections that are a list of things. */
  count?: number;
  verdict?: string;
  stats?: ReportStat[];
  text?: string;
  fields?: ReportField[];
  rows?: ComplianceRow[];
  items?: ReportItem[];
  raw?: unknown;
}

// A free-text value past this many characters is clamped with a "show more"
// rather than pushing the rest of the grid down the page.
const CLAMP_OVER = 180;

// A reviewer's result as one tab per section of the report.
//
// It replaced app-json-view for insurance and valuation: that renders any
// JSON as nested key/value tables, which for these payloads meant one deep
// table where the section name was just another left-hand cell and the whole
// report was a single scroll. Both shapes ARE fixed (each engine's prompt
// pins them), so they're worth laying out properly.
//
// Only the vocabulary differs between reviewers, so that is all a caller
// passes — see ReportSpec. Anything the engine grows later still appears,
// via the 'raw' fallback, rather than silently vanishing.
@Component({
  selector: 'app-section-report',
  standalone: true,
  imports: [CommonModule, ClampTextComponent, JsonViewComponent],
  templateUrl: './section-report.component.html',
  styleUrl: './section-report.component.css'
})
export class SectionReportComponent {
  sections: ReportSection[] = [];
  active = 0;

  private _spec?: ReportSpec;
  private _result: unknown;

  @Input({ required: true }) set spec(value: ReportSpec) {
    this._spec = value;
    this.rebuild();
  }

  @Input() set result(value: unknown) {
    this._result = value;
    this.rebuild();
  }

  get current(): ReportSection | undefined {
    return this.sections[this.active];
  }

  tone(word: string | undefined): Tone {
    return toneOf(word, this._spec);
  }

  /** How to render one value in a field grid. */
  kindOf(value: unknown): 'empty' | 'list' | 'long' | 'text' | 'raw' {
    if (value === null || value === undefined || value === '') return 'empty';
    if (Array.isArray(value)) {
      if (!value.length) return 'empty';
      return value.every((v) => v === null || typeof v !== 'object') ? 'list' : 'raw';
    }
    if (typeof value === 'object') return 'raw';
    return String(value).length > CLAMP_OVER ? 'long' : 'text';
  }

  private rebuild(): void {
    this.sections = this.build(this._result);
    this.active = 0;
  }

  private build(value: unknown): ReportSection[] {
    const spec = this._spec;
    if (!spec) return [];
    // Tolerate the inner object on its own as well as the wrapped form, so a
    // result stored in either shape still renders.
    const report = (spec.root ? (value as any)?.[spec.root] : undefined) ?? value;
    if (!report || typeof report !== 'object' || Array.isArray(report)) return [];

    const out: ReportSection[] = [];
    const known = new Set<string>();
    for (const key of Object.keys(spec.labels)) {
      if (!(key in report)) continue;
      known.add(key);
      out.push(
        key === spec.summaryKey
          ? this.summary(key, spec.labels[key], report, spec)
          : this.section(key, spec.labels[key], report[key], spec)
      );
    }
    for (const [key, val] of Object.entries(report)) {
      if (known.has(key)) continue;
      out.push({ key, label: this.humanize(key), kind: 'raw', raw: val });
    }
    return out;
  }

  /** The landing tab, when a reviewer has a headline verdict. On its own the
   * verdict is one word, so the counts that back it up sit under it — which
   * is why this is built from the whole report, not from its own key. */
  private summary(key: string, label: string, report: any, spec: ReportSpec): ReportSection {
    return {
      key,
      label,
      kind: 'summary',
      verdict: report[key] == null ? '' : String(report[key]),
      stats: spec.summaryStats ? spec.summaryStats(report) : []
    };
  }

  private section(key: string, label: string, value: any, spec: ReportSpec): ReportSection {
    if (Array.isArray(value)) {
      const items = value.map((entry, i) => this.item(entry, spec.itemShapes?.[key], i, spec));
      return { key, label, kind: 'items', items, count: items.length };
    }

    if (value && typeof value === 'object') {
      // A map of name -> {status, ...} is a checklist, not a field grid: it
      // reads far better as rows than as one label/value pair per entry.
      // Inferred rather than configured — the shape says it plainly.
      if (this.isChecklist(value)) {
        const rows: ComplianceRow[] = Object.entries(value).map(([requirement, entry]: [string, any]) => ({
          requirement: this.humanize(requirement),
          status: entry?.status ?? '',
          issues: (entry?.issue_ids ?? []).join(', ')
        }));
        return { key, label, kind: 'compliance', rows, count: rows.length };
      }
      return { key, label, kind: 'fields', fields: this.fields(value, spec) };
    }

    // A lone scalar section is prose (a valuator's comments) or a verdict.
    if (value !== null && value !== undefined && typeof value !== 'object') {
      return { key, label, kind: 'text', text: String(value) };
    }

    return { key, label, kind: 'raw', raw: value };
  }

  /** Every value is an object carrying a `status` — i.e. requirement -> result. */
  private isChecklist(value: Record<string, any>): boolean {
    const entries = Object.values(value);
    return (
      entries.length > 0 &&
      entries.every((e) => !!e && typeof e === 'object' && !Array.isArray(e) && 'status' in e)
    );
  }

  private item(entry: any, shape: ItemShape | undefined, i: number, spec: ReportSpec): ReportItem {
    if (!entry || typeof entry !== 'object') {
      return { title: String(entry ?? `Item ${i + 1}`), fields: [] };
    }
    const used = new Set([shape?.title, shape?.badge, shape?.id].filter(Boolean) as string[]);
    const rest = Object.fromEntries(Object.entries(entry).filter(([k]) => !used.has(k)));
    return {
      id: shape?.id ? entry[shape.id] : undefined,
      title: (shape?.title ? entry[shape.title] : undefined) || `Item ${i + 1}`,
      badge: shape?.badge ? entry[shape.badge] : undefined,
      fields: this.fields(rest, spec)
    };
  }

  private fields(obj: Record<string, unknown>, spec: ReportSpec): ReportField[] {
    const chips = spec.chipFields ?? [];
    return Object.entries(obj ?? {}).map(([key, value]) => ({
      label: this.humanize(key),
      value,
      // Only a short scalar reads as a chip; a sentence in a pill does not.
      chip: chips.includes(key) && value != null && typeof value !== 'object'
    }));
  }

  /** insurer_name -> "Insurer name". An already-spaced key is left alone, so a
   * requirement the model named in prose isn't mangled. */
  private humanize(key: string): string {
    const words = key.replace(/_/g, ' ').trim();
    return words.charAt(0).toUpperCase() + words.slice(1);
  }
}
