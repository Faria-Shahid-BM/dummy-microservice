import { KONG_BASE } from '../../session.service';

export interface AuditAttachment {
  filename: string;
  attachment_id: string | null;
}

// Mirrors audit-service's _to_display_node() (audit-service/main.py) —
// the backend has already decided what's noise and what's worth a card;
// this is just a type-tagged tree to render, not raw JSON to interpret.
export type DisplayNode =
  | { type: 'text'; value: string }
  | { type: 'fields'; rows: { label: string; value: DisplayNode }[] }
  | { type: 'table'; columns: string[]; rows: DisplayNode[][] }
  | { type: 'list'; items: DisplayNode[] };

export interface AuditSection {
  title: string;
  content: DisplayNode;
}

export interface AuditEntry {
  id: number;
  timestamp: string;
  user_id: string;
  service: string;
  action: string;
  resource: string | null;
  attachments: AuditAttachment[];
  sections: AuditSection[];
}

export function attachmentUrl(attachmentId: string, filename: string): string {
  // audit-service's endpoint lives at "/audit/attachments/..." (not root),
  // and Kong's route prefix is also "/api/audit" with strip_path — so the
  // externally reachable path ends up with the segment doubled, same as the
  // list endpoint (see admin-audit.component.ts). See README.md.
  return `${KONG_BASE}/api/audit/audit/attachments/${attachmentId}?filename=${encodeURIComponent(filename)}`;
}
