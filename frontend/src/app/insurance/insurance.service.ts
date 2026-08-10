import { Injectable } from '@angular/core';
import { KONG_BASE } from '../session.service';
import { CaseService } from '../shared/case.service';

// review_insurance()'s output has no fixed shape worth typing — rendered
// via app-json-view, same as the old inline panel did with insuranceResult.
@Injectable({ providedIn: 'root' })
export class InsuranceService extends CaseService<unknown> {
  protected readonly apiBase = `${KONG_BASE}/api/insurance`;
  readonly routeBase = '/insurance';
  readonly label = 'Insurance';
}
