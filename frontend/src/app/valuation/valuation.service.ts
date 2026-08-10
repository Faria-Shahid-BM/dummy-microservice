import { Injectable } from '@angular/core';
import { KONG_BASE } from '../session.service';
import { CaseService } from '../shared/case.service';

// review_valuation()'s output has no fixed shape worth typing — rendered
// via app-json-view, same as the old inline panel did with valuationResult.
@Injectable({ providedIn: 'root' })
export class ValuationService extends CaseService<unknown> {
  protected readonly apiBase = `${KONG_BASE}/api/valuation`;
  readonly routeBase = '/valuation';
  readonly label = 'Valuation';
}
