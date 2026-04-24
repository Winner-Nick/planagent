// Mirror of backend/src/planagent/wechat/constants.py.
//
// The production deployment has exactly two known humans: Peng and Chenchen.
// This map lets the React dashboard render owner columns without a round-trip
// to the backend. When a third human joins (different deployment), replace
// this with a DB- or API-driven roster.

export interface KnownHuman {
  wechatUserId: string;
  displayName: string;
}

export const PENG: KnownHuman = {
  wechatUserId: "o9cq807dznGxf81R2JoVl2pEx_T0@im.wechat",
  displayName: "鹏鹏",
};

export const CHENCHEN: KnownHuman = {
  wechatUserId: "o9cq80ydQIR4ZaYl6vXvDp_4KklQ@im.wechat",
  displayName: "辰辰",
};

export const KNOWN_HUMANS: readonly KnownHuman[] = [PENG, CHENCHEN];

const DISPLAY_NAME_BY_ID: Record<string, string> = Object.fromEntries(
  KNOWN_HUMANS.map((h) => [h.wechatUserId, h.displayName]),
);

// Fallback index — also resolve by display name so fixtures that already
// store "鹏鹏" / "辰辰" as owner_name still map to the right column.
const DISPLAY_NAME_SET = new Set(KNOWN_HUMANS.map((h) => h.displayName));

export function displayNameFor(
  ownerId: string | null | undefined,
  ownerName?: string | null,
): string {
  if (ownerId && DISPLAY_NAME_BY_ID[ownerId]) return DISPLAY_NAME_BY_ID[ownerId];
  if (ownerName && DISPLAY_NAME_SET.has(ownerName)) return ownerName;
  return ownerName ?? ownerId ?? "未知";
}

// Resolve a plan-like object (owner_id + owner_name) to one of the two known
// display names when possible; otherwise return the raw owner_name (or a
// placeholder). Used to bucket plans into owner columns.
export function resolveOwnerKey(plan: {
  owner_id: string;
  owner_name: string;
}): string {
  if (DISPLAY_NAME_BY_ID[plan.owner_id]) {
    return DISPLAY_NAME_BY_ID[plan.owner_id];
  }
  if (DISPLAY_NAME_SET.has(plan.owner_name)) return plan.owner_name;
  return plan.owner_name || plan.owner_id || "未知";
}

export const OWNER_COLUMN_ORDER: readonly string[] = [
  PENG.displayName,
  CHENCHEN.displayName,
];
