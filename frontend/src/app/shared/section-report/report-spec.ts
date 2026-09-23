// How one reviewer's result is split into tabs. The rendering lives in
// app-section-report; a service supplies only the vocabulary — which keys
// exist, what to call them, and which words are good or bad news.

/** The colour a status word carries. */
export type Tone = 'ok' | 'bad' | 'warn' | 'muted';

/** One counter on a summary tab. */
export interface ReportStat {
  label: string;
  n: number;
}

/** Which field of a list entry is its heading, its chip, and its id. Every
 * other field falls through to the entry's grid, so a key added to the
 * engine's prompt still renders rather than being dropped. */
export interface ItemShape {
  title: string;
  badge?: string;
  id?: string;
}

export interface ReportSpec {
  /** Unwrap this key when the engine wraps the report in one. */
  root?: string;
  /** Tab order and wording. A key the engine returns that isn't listed here
   * still gets a tab, after these, rendered as raw JSON. */
  labels: Record<string, string>;
  /** For array-valued sections. */
  itemShapes?: Record<string, ItemShape>;
  /** Field keys whose value is a status word, rendered as a toned chip
   * instead of plain text. */
  chipFields?: string[];
  /** Extra status words for this reviewer, lowercased. Merged over the
   * shared defaults below. */
  tones?: Record<string, Tone>;
  /** The key whose value is the headline verdict. Its tab leads with that
   * verdict as a chip, plus `summaryStats` as counters beneath. */
  summaryKey?: string;
  summaryStats?: (report: any) => ReportStat[];
}

// Words every reviewer shares. A word that isn't here stays grey rather than
// being guessed at — "N/A" and "Low" are not warnings.
const BASE_TONES: Record<string, Tone> = {
  compliant: 'ok',
  matched: 'ok',
  pass: 'ok',
  'non-compliant': 'bad',
  critical: 'bad',
  high: 'bad',
  fail: 'bad',
  'info not available': 'warn',
  medium: 'warn'
};

export function toneOf(word: string | undefined, spec: ReportSpec | undefined): Tone {
  const key = (word ?? '').trim().toLowerCase();
  return spec?.tones?.[key] ?? BASE_TONES[key] ?? 'muted';
}
