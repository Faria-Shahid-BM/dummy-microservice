import { ReportSpec } from '../../shared/section-report/report-spec';

// How the valuation reviewer's result is split into tabs. Keys are what
// engines/valuation.py assembles; the labels are what a reviewer would call
// them. The rendering is app-section-report's.
//
// No summary tab: unlike insurance, valuation returns no headline verdict to
// lead with, and inventing one would mean deciding here what counts as a
// pass — a judgement that belongs in the engine, not the view.
export const VALUATION_REPORT: ReportSpec = {
  labels: {
    extracted_fields: 'Extracted fields',
    panel_review: 'Panel review',
    policy_review: 'Policy review',
    cushion_calculation: 'Cushion',
    lending_limit: 'Lending limit',
    valuator_comments: 'Valuator comments'
  },
  chipFields: ['limit_status', 'panel_status'],
  tones: {
    'within limit': 'ok',
    'exceeds limit': 'bad',
    'over limit': 'bad',
    unmatched: 'bad',
    'not matched': 'bad',
    'no match': 'bad'
  }
};
