import { ReportSpec, ReportStat } from '../../shared/section-report/report-spec';

// How the insurance reviewer's result is split into tabs. Keys are
// engines/prompts/insurance_analysis.md's STEP 6 output schema; the labels
// are what a reviewer would call them. The rendering is app-section-report's.
export const INSURANCE_REPORT: ReportSpec = {
  root: 'insurance_report',
  labels: {
    overall_assessment: 'Summary',
    policy_type_determination: 'Policy type',
    key_details: 'Key details',
    financials_and_limits: 'Financials',
    risk_analysis: 'Risk analysis',
    policy_compliance: 'Compliance',
    key_issues: 'Key issues',
    data_gaps: 'Data gaps',
    recommendations: 'Recommendations'
  },
  itemShapes: {
    key_issues: { title: 'title', badge: 'severity', id: 'issue_id' },
    data_gaps: { title: 'missing_information' },
    recommendations: { title: 'action', badge: 'priority' }
  },
  chipFields: ['compatibility_status'],
  summaryKey: 'overall_assessment',
  summaryStats: (report: any): ReportStat[] => {
    const statuses = Object.values(report.policy_compliance ?? {}).map((e: any) =>
      (e?.status ?? '').trim().toLowerCase()
    );
    const tally = (want: string) => statuses.filter((s) => s === want).length;
    const len = (k: string) => (Array.isArray(report[k]) ? report[k].length : 0);
    return [
      { label: 'compliant', n: tally('compliant') },
      { label: 'non-compliant', n: tally('non-compliant') },
      { label: 'info not available', n: tally('info not available') },
      { label: 'key issues', n: len('key_issues') },
      { label: 'data gaps', n: len('data_gaps') },
      { label: 'recommendations', n: len('recommendations') }
    ];
  }
};
