import { Injectable } from '@angular/core';
import { KONG_BASE } from '../session.service';
import { CaseService, SlotDef } from '../shared/case.service';

export interface CollateralComparisonRow {
  field: string;
  label: string;
  legal_value: string | null;
  property_value: string | null;
  status: 'match' | 'mismatch' | 'missing';
}

export interface CollateralResult {
  comparison: CollateralComparisonRow[];
  observations: string[];
  summary: { matches: number; mismatches: number; missing: number; fields: number };
}

@Injectable({ providedIn: 'root' })
export class CollateralService extends CaseService<CollateralResult> {
  protected readonly apiBase = `${KONG_BASE}/api/collateral`;
  readonly routeBase = '/collateral';
  readonly label = 'Collateral';
  readonly slots: SlotDef[] = [
    { key: 'legal', label: 'Legal opinion (.docx or .pdf)', accept: '.docx,.pdf' },
    { key: 'property', label: 'Property / title document (.docx or .pdf)', accept: '.docx,.pdf' }
  ];
  override readonly itemNoun = 'pair';
}
