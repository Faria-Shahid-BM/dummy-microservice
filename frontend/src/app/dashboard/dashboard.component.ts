import { CommonModule } from '@angular/common';
import { Component } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { FormsModule } from '@angular/forms';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { KONG_BASE } from '../session.service';
import { ServiceCatalogService, ServiceMeta } from './service-catalog.service';
import { consumeSse, parseSseError } from '../sse.util';

interface PolicyQaStatus {
  has_own_index: boolean;
  bundled_available: boolean;
}

interface PolicyQaMessage {
  role: 'user' | 'assistant';
  content: string;
  sources?: string[];
  streaming?: boolean;
}

// docdiff/collateral/valuation/insurance are all routed services now (each
// with its own persisted Cases list — see collateral/, valuation/,
// insurance/, docdiff/); policy_qa is the only one left as an inline
// dashboard panel, since it's a standing per-account index + chat, not a
// per-upload case.
@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './dashboard.component.html'
})
export class DashboardComponent {
  policyStatus: PolicyQaStatus | null = null;
  policyMessages: PolicyQaMessage[] = [];
  policyQuery = '';
  policyIngestFile: File | null = null;
  policyBusy = false;
  policyError = '';
  policyChatError = '';

  constructor(private http: HttpClient, public catalog: ServiceCatalogService) {
    // Selection lives in ServiceCatalogService (see app-shell.component.ts)
    // since the sidebar that sets it is mounted outside this component.
    this.catalog.selected$.pipe(takeUntilDestroyed()).subscribe((meta) => this.onSelectionChanged(meta));
  }

  private onSelectionChanged(meta: ServiceMeta | null): void {
    this.policyError = '';
    this.policyChatError = '';

    if (meta?.kind === 'policyqa') {
      this.loadPolicyStatus();
    }
  }

  // --- policyqa-service ---

  loadPolicyStatus(): void {
    this.http.get<PolicyQaStatus>(`${KONG_BASE}/api/policyqa/status`).subscribe({
      next: (res) => (this.policyStatus = res),
      error: (err: HttpErrorResponse) => {
        this.policyError = err.error?.detail ?? 'failed to load status';
      }
    });
  }

  onPolicyIngestFile(event: Event): void {
    this.policyIngestFile = (event.target as HTMLInputElement).files?.[0] ?? null;
  }

  ingestPolicy(): void {
    if (!this.policyIngestFile) return;
    this.policyError = '';
    this.policyBusy = true;
    const form = new FormData();
    form.append('file', this.policyIngestFile);
    this.http.post(`${KONG_BASE}/api/policyqa/ingest`, form).subscribe({
      next: () => {
        this.policyIngestFile = null;
        this.policyBusy = false;
        this.loadPolicyStatus();
      },
      error: (err: HttpErrorResponse) => {
        this.policyError = err.error?.detail ?? 'ingest failed';
        this.policyBusy = false;
      }
    });
  }

  deletePolicyIndex(): void {
    this.policyError = '';
    this.http.delete(`${KONG_BASE}/api/policyqa/index`).subscribe({
      next: () => this.loadPolicyStatus(),
      error: (err: HttpErrorResponse) => {
        this.policyError = err.error?.detail ?? 'delete failed';
      }
    });
  }

  async sendPolicyQuery(): Promise<void> {
    const query = this.policyQuery.trim();
    if (!query || this.policyBusy) return;
    this.policyChatError = '';
    this.policyBusy = true;

    const history = this.policyMessages.map((m) => ({ role: m.role, content: m.content }));
    this.policyMessages.push({ role: 'user', content: query });
    this.policyQuery = '';

    // Streaming placeholder: shows the typing-dots indicator until the first
    // token arrives, then grows in place as content events stream in.
    const assistantMsg: PolicyQaMessage = { role: 'assistant', content: '', streaming: true };
    this.policyMessages.push(assistantMsg);

    try {
      await consumeSse(`${KONG_BASE}/api/policyqa/chat/stream`, { query, history }, (eventType, data) => {
        if (eventType === 'content') {
          try {
            assistantMsg.content += JSON.parse(data) as string;
          } catch {
            /* malformed chunk — skip it rather than corrupt the message */
          }
        } else if (eventType === 'result') {
          try {
            const result = JSON.parse(data) as { answer: string; sources: string[] };
            assistantMsg.content = result.answer;
            assistantMsg.sources = result.sources;
          } catch {
            /* keep the accumulated content as-is */
          }
        } else if (eventType === 'error') {
          this.policyChatError = parseSseError(data);
        }
      });
    } catch (err) {
      this.policyChatError = err instanceof Error ? err.message : 'chat failed';
      if (!assistantMsg.content) {
        const idx = this.policyMessages.indexOf(assistantMsg);
        if (idx !== -1) this.policyMessages.splice(idx, 1);
      }
    } finally {
      assistantMsg.streaming = false;
      this.policyBusy = false;
    }
  }
}
