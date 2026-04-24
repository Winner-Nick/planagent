// Types mirror the PlanAgent backend schemas. All identifiers are string,
// all timestamps are ISO-8601 strings, and status fields use union literals.

// Known plan statuses. Backend may add more lanes (e.g. PR-I introduces
// `overdue`); the board widens this union eagerly and renders any future
// unknown status under an "其他" (other) bucket, so the UI never crashes
// when a new server enum lands before the client is redeployed.
export type PlanStatus =
  | "draft"
  | "active"
  | "overdue"
  | "completed"
  | "paused";

export type PlanPriority = "low" | "medium" | "high";

export type PlanRecurrence =
  | { kind: "none" }
  | { kind: "daily"; time: string }
  | { kind: "weekly"; day_of_week: number; time: string }
  | { kind: "monthly"; day_of_month: number; time: string };

export interface Plan {
  id: string;
  title: string;
  description: string;
  status: PlanStatus;
  priority: PlanPriority;
  owner_id: string;
  owner_name: string;
  group_id: string | null;
  start_at: string | null;
  due_at: string | null;
  recurrence: PlanRecurrence;
  tags: string[];
  created_at: string;
  updated_at: string;
}

export type ReminderStatus = "scheduled" | "sent" | "skipped" | "failed";

export interface Reminder {
  id: string;
  plan_id: string;
  fire_at: string;
  status: ReminderStatus;
  channel: "wechat" | "email" | "web";
  message: string;
  created_at: string;
  sent_at: string | null;
}

export interface GroupMember {
  id: string;
  display_name: string;
  role: "admin" | "member";
}

export interface GroupContext {
  id: string;
  name: string;
  platform: "wechat" | "telegram" | "web";
  external_id: string;
  last_seen_at: string;
  member_count: number;
  members: GroupMember[];
  active_plan_count: number;
}

export type ConversationRole = "user" | "assistant" | "system" | "tool";

export interface ToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export interface ConversationTurn {
  id: string;
  group_id: string;
  role: ConversationRole;
  sender_name: string | null;
  content: string;
  tool_calls: ToolCall[];
  tool_call_id: string | null;
  created_at: string;
}

export interface PlanFilter {
  status?: PlanStatus;
  owner_id?: string;
  group_id?: string;
}

export type PlanCreate = Omit<Plan, "id" | "created_at" | "updated_at">;
export type PlanUpdate = Partial<PlanCreate>;
