import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';
import { Observable } from 'rxjs';
import { KONG_BASE } from '../session.service';

/** One configurable setting, as config-service reports it. */
export interface ConfigSetting {
  key: string;
  label: string;
  /** model | prompt | number — only "model" exists today. */
  kind: string;
  description: string;
  /** What the deployment ships with; what "reset" goes back to. */
  default: string;
  /** In force for this user: their override if set, else the default. */
  value: string;
  is_overridden: boolean;
  updated_at: string | null;
}

/** One reviewer's settings. Only the ones this user is entitled to come back. */
export interface ConfigServiceGroup {
  scope: string;
  label: string;
  description: string;
  settings: ConfigSetting[];
  override_count: number;
}

/** One model the configured provider advertises. */
export interface CatalogueModel {
  id: string;
  name: string;
  context_length: number | null;
  /** Can read an image — the capability the vision/OCR role needs. */
  vision: boolean;
}

/** `available: false` means suggest nothing and warn about nothing. */
export interface ModelCatalogue {
  available: boolean;
  source: string;
  error: string;
  models: CatalogueModel[];
}

// Settings live in config-service, not in each reviewer (see
// config-service/main.py) — one place to read and change what every service
// runs with, rather than one screen per service.
@Injectable({ providedIn: 'root' })
export class ConfigurationService {
  private readonly base = `${KONG_BASE}/api/config`;

  constructor(private http: HttpClient) {}

  overview(): Observable<{ services: ConfigServiceGroup[] }> {
    return this.http.get<{ services: ConfigServiceGroup[] }>(this.base);
  }

  /** Suggestions and an advisory spelling check — never a whitelist; see
   * config-service's catalogue() for why it can't be one. */
  catalogue(): Observable<ModelCatalogue> {
    return this.http.get<ModelCatalogue>(`${this.base}/catalogue`);
  }

  setValue(scope: string, key: string, value: string): Observable<ConfigSetting> {
    return this.http.put<ConfigSetting>(`${this.base}/${scope}/${key}`, { value });
  }

  /** Deleting the override IS "reset to default" — nothing stores a copy of
   * the default, so the row goes back to following the deployment. */
  reset(scope: string, key: string): Observable<ConfigSetting> {
    return this.http.delete<ConfigSetting>(`${this.base}/${scope}/${key}`);
  }
}
