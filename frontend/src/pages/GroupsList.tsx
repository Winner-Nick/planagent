import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import type { GroupContext } from "../api/types";
import { Badge } from "../components/ui/badge";
import { Card, CardContent } from "../components/ui/card";
import { formatDate } from "../lib/format";

export function GroupsList() {
  const [groups, setGroups] = useState<GroupContext[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    api.groups
      .list()
      .then((g) => {
        if (active) setGroups(g);
      })
      .catch((err) => {
        if (active) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      active = false;
    };
  }, []);

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-3xl font-semibold tracking-tight">Groups</h2>
        <p className="text-sm text-muted-foreground">
          Every chat the agent is connected to.
        </p>
      </div>

      {error && (
        <Card>
          <CardContent className="py-6 text-sm text-destructive">
            {error}
          </CardContent>
        </Card>
      )}

      <Card>
        <CardContent className="p-0">
          <table className="w-full text-sm">
            <thead className="border-b text-xs uppercase tracking-wide text-muted-foreground">
              <tr>
                <th className="px-4 py-3 text-left font-medium">Group</th>
                <th className="px-4 py-3 text-left font-medium">Platform</th>
                <th className="px-4 py-3 text-left font-medium">Members</th>
                <th className="px-4 py-3 text-left font-medium">
                  Active plans
                </th>
                <th className="px-4 py-3 text-left font-medium">Last seen</th>
              </tr>
            </thead>
            <tbody>
              {groups === null && (
                <tr>
                  <td colSpan={5} className="px-4 py-6">
                    <div className="h-6 animate-pulse rounded bg-muted/40" />
                  </td>
                </tr>
              )}
              {groups?.map((g) => (
                <tr
                  key={g.id}
                  className="border-b last:border-b-0 transition-colors hover:bg-accent/40"
                >
                  <td className="px-4 py-3">
                    <Link
                      to={`/groups/${g.id}`}
                      className="font-medium hover:underline"
                    >
                      {g.name}
                    </Link>
                    <p className="text-xs text-muted-foreground">
                      {g.external_id}
                    </p>
                  </td>
                  <td className="px-4 py-3">
                    <Badge variant="outline" className="capitalize">
                      {g.platform}
                    </Badge>
                  </td>
                  <td className="px-4 py-3 text-muted-foreground">
                    {g.member_count}
                  </td>
                  <td className="px-4 py-3 text-muted-foreground">
                    {g.active_plan_count}
                  </td>
                  <td className="px-4 py-3 text-muted-foreground">
                    {formatDate(g.last_seen_at)}
                  </td>
                </tr>
              ))}
              {groups !== null && groups.length === 0 && (
                <tr>
                  <td
                    colSpan={5}
                    className="px-4 py-8 text-center text-sm text-muted-foreground"
                  >
                    No groups yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </CardContent>
      </Card>
    </div>
  );
}
