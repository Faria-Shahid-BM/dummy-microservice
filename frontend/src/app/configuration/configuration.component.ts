import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ModelPickerComponent } from '../shared/model-picker/model-picker.component';
import { HttpErrorResponse } from '@angular/common/http';
import {
  CatalogueModel,
  ConfigServiceGroup,
  ConfigSetting,
  ConfigurationService
} from './configuration.service';

/** Per-row edit state, keyed by `${scope}/${key}`. */
interface RowState {
  draft: string;
  saving: boolean;
  error: string;
  /** Briefly true after a successful write, so a save that changes nothing
   * visible still says it happened. */
  saved: boolean;
}

@Component({
  selector: 'app-configuration',
  standalone: true,
  imports: [CommonModule, FormsModule, ModelPickerComponent],
  templateUrl: './configuration.component.html',
  styleUrl: './configuration.component.css'
})
export class ConfigurationComponent implements OnInit {
  groups: ConfigServiceGroup[] = [];
  loading = false;
  error = '';

  /** Suggestions for the input, and what the advisory check matches against.
   * Empty when the provider couldn't be reached — then the field is a plain
   * text box and nothing is flagged. */
  catalogue: CatalogueModel[] = [];
  catalogueAvailable = false;

  private byId = new Map<string, CatalogueModel>();
  private rows = new Map<string, RowState>();

  constructor(private config: ConfigurationService) {}

  ngOnInit(): void {
    this.load();
    this.loadCatalogue();
  }

  /** Best-effort: the page works without it, so a failure is silent rather
   * than an error banner over settings that are perfectly editable. */
  private loadCatalogue(): void {
    this.config.catalogue().subscribe({
      next: (reply) => {
        this.catalogue = reply.models;
        this.catalogueAvailable = reply.available;
        this.byId = new Map(reply.models.map((m) => [m.id.toLowerCase(), m]));
      },
      error: () => undefined
    });
  }

  /**
   * The catalogue entry for a model id, matched case-insensitively.
   *
   * Case matters here: this deployment's default is `google/gemini-2.5-Flash`
   * and the catalogue lists `google/gemini-2.5-flash`. An exact match would
   * flag the shipped default as unknown.
   */
  match(value: string): CatalogueModel | null {
    return this.byId.get(value.trim().toLowerCase()) ?? null;
  }

  /**
   * Why this id looks wrong, or '' when there's nothing to say.
   *
   * Always advisory. The catalogue has no embedding models at all, and
   * LLM_BASE_URL may point at a gateway with its own names, so an unmatched
   * id is "not in the catalogue" — not "does not exist" — and still saves.
   */
  advice(setting: ConfigSetting, value: string): string {
    const id = value.trim();
    if (!id || !this.catalogueAvailable) return '';
    const model = this.match(id);
    if (!model) {
      return 'Not in the provider catalogue. Check the spelling — or ignore this if your gateway provides it.';
    }
    if (setting.key === 'model.vision' && !model.vision) {
      return `${model.name} can't read images, so scanned pages would fail this step.`;
    }
    return '';
  }

  /** The matched model's own description, shown when there's no warning. */
  describe(value: string): string {
    const model = this.match(value);
    if (!model) return '';
    const context = model.context_length
      ? ` · ${Math.round(model.context_length / 1000)}k context`
      : '';
    return `${model.name}${context}`;
  }

  load(): void {
    this.loading = true;
    this.error = '';
    this.config.overview().subscribe({
      next: (reply) => {
        this.groups = reply.services;
        this.rows.clear();
        this.loading = false;
      },
      error: (err: HttpErrorResponse) => {
        this.error = err.error?.detail ?? 'could not load your settings';
        this.loading = false;
      }
    });
  }

  private id(group: ConfigServiceGroup, setting: ConfigSetting): string {
    return `${group.scope}/${setting.key}`;
  }

  /**
   * The row's editing state, created on first use from the value in force.
   *
   * Lazily rather than up front so a reload replaces the server's values
   * wholesale — an edit half-typed against the previous load shouldn't
   * survive into the new one.
   */
  row(group: ConfigServiceGroup, setting: ConfigSetting): RowState {
    const key = this.id(group, setting);
    let state = this.rows.get(key);
    if (!state) {
      state = { draft: setting.value, saving: false, error: '', saved: false };
      this.rows.set(key, state);
    }
    return state;
  }

  /** Nothing to save when the box still holds what's already in force. */
  isDirty(group: ConfigServiceGroup, setting: ConfigSetting): boolean {
    const draft = this.row(group, setting).draft.trim();
    return draft.length > 0 && draft !== setting.value;
  }

  save(group: ConfigServiceGroup, setting: ConfigSetting): void {
    const state = this.row(group, setting);
    const value = state.draft.trim();
    if (!value || state.saving) return;
    state.saving = true;
    state.error = '';
    state.saved = false;
    this.config.setValue(group.scope, setting.key, value).subscribe({
      next: (updated) => this.apply(group, setting, updated, state),
      error: (err: HttpErrorResponse) => {
        state.error = err.error?.detail ?? 'could not save this model';
        state.saving = false;
      }
    });
  }

  reset(group: ConfigServiceGroup, setting: ConfigSetting): void {
    const state = this.row(group, setting);
    if (state.saving) return;
    state.saving = true;
    state.error = '';
    state.saved = false;
    this.config.reset(group.scope, setting.key).subscribe({
      next: (updated) => this.apply(group, setting, updated, state),
      error: (err: HttpErrorResponse) => {
        state.error = err.error?.detail ?? 'could not reset this model';
        state.saving = false;
      }
    });
  }

  /**
   * Fold the server's reply back into the row it came from.
   *
   * The reply is the authority on what's now in force — not the string that
   * was typed — so a value the server trimmed or normalised shows as what
   * will actually run.
   */
  private apply(
    group: ConfigServiceGroup,
    setting: ConfigSetting,
    updated: ConfigSetting,
    state: RowState
  ): void {
    setting.value = updated.value;
    setting.is_overridden = updated.is_overridden;
    setting.updated_at = updated.updated_at;
    state.draft = updated.value;
    state.saving = false;
    state.saved = true;
    group.override_count = group.settings.filter((s) => s.is_overridden).length;
  }

  /** The picker owns the text; this stores it and clears the row's last
   * outcome, so a stale "saved" tick can't sit under a changed value. */
  onDraft(group: ConfigServiceGroup, setting: ConfigSetting, value: string): void {
    const state = this.row(group, setting);
    state.draft = value;
    state.saved = false;
    state.error = '';
  }

  trackByScope = (_: number, group: ConfigServiceGroup) => group.scope;
  trackByKey = (_: number, setting: ConfigSetting) => setting.key;
}
