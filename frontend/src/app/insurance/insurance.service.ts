import { Injectable } from '@angular/core';
import { Observable } from 'rxjs';
import { KONG_BASE } from '../session.service';
import { CaseService, SlotDef } from '../shared/case.service';

// GET /api/insurance/policy — the bank policy this account's cases are graded
// against. has_own_policy false means the engine's bundled policy.txt is used.
export interface BankPolicyStatus {
  has_own_policy: boolean;
  file_name: string | null;
  chars: number | null;
  uploaded_at: string | null;
}

// review_insurance()'s output has no fixed shape worth typing — rendered
// via app-json-view, same as the old inline panel did with insuranceResult.
@Injectable({ providedIn: 'root' })
export class InsuranceService extends CaseService<unknown> {
  protected readonly apiBase = `${KONG_BASE}/api/insurance`;
  readonly routeBase = '/insurance';
  readonly label = 'Insurance';
  readonly slots: SlotDef[] = [
    { key: 'policy', label: 'Insurance policy (.docx or .pdf)', accept: '.docx,.pdf' }
  ];
  override readonly itemNoun = 'policy';

  // The bank policy is account-level standing configuration, not case input —
  // it lives beside /cases rather than as an upload slot on one (see
  // insurance-service/main.py), so these sit outside the shared CaseService API.

  getBankPolicy(): Observable<BankPolicyStatus> {
    return this.http.get<BankPolicyStatus>(`${this.apiBase}/policy`);
  }

  uploadBankPolicy(file: File): Observable<BankPolicyStatus> {
    const form = new FormData();
    form.append('file', file);
    return this.http.post<BankPolicyStatus>(`${this.apiBase}/policy`, form);
  }

  deleteBankPolicy(): Observable<BankPolicyStatus> {
    return this.http.delete<BankPolicyStatus>(`${this.apiBase}/policy`);
  }
}
