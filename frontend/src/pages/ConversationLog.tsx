import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ChevronDown, ChevronRight, ChevronLeft, Wrench } from "lucide-react";

import { api } from "../api/client";
import type {
  ConversationTurn,
  GroupContext,
  ToolCall,
} from "../api/types";
import { Badge } from "../components/ui/badge";
import { Card, CardContent } from "../components/ui/card";
import { cn } from "../lib/utils";
import { formatDate } from "../lib/format";

export function ConversationLog() {
  const { id } = useParams<{ id: string }>();
  const [group, setGroup] = useState<GroupContext | null>(null);
  const [turns, setTurns] = useState<ConversationTurn[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!id) return;
    let active = true;
    Promise.all([api.groups.get(id), api.groups.conversations(id)])
      .then(([g, c]) => {
        if (!active) return;
        setGroup(g);
        setTurns(c);
      })
      .catch((err) => {
        if (active) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      active = false;
    };
  }, [id]);

  useEffect(() => {
    if (turns && turns.length > 0 && bottomRef.current) {
      bottomRef.current.scrollIntoView({ block: "end" });
    }
  }, [turns]);

  return (
    <div className="space-y-6">
      <Link
        to="/groups"
        className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
      >
        <ChevronLeft className="h-3 w-3" /> Back to groups
      </Link>

      <div>
        <h2 className="text-3xl font-semibold tracking-tight">
          {group?.name ?? "Conversation"}
        </h2>
        {group && (
          <p className="text-sm text-muted-foreground">
            {group.member_count} members · last seen{" "}
            {formatDate(group.last_seen_at)}
          </p>
        )}
      </div>

      {error && (
        <Card>
          <CardContent className="py-6 text-sm text-destructive">
            {error}
          </CardContent>
        </Card>
      )}

      <Card>
        <CardContent
          data-testid="conversation-scroll"
          className="h-[calc(100vh-280px)] space-y-3 overflow-y-auto p-6"
        >
          {turns === null && (
            <div className="space-y-3">
              {[0, 1, 2].map((i) => (
                <div
                  key={i}
                  className="h-12 animate-pulse rounded-md bg-muted/40"
                />
              ))}
            </div>
          )}
          {turns?.map((turn) => <TurnBubble key={turn.id} turn={turn} />)}
          <div ref={bottomRef} />
        </CardContent>
      </Card>
    </div>
  );
}

function TurnBubble({ turn }: { turn: ConversationTurn }) {
  if (turn.role === "user") {
    return (
      <div data-testid="bubble-user" className="flex justify-end">
        <div className="max-w-[75%] rounded-2xl rounded-br-sm bg-primary px-4 py-2.5 text-primary-foreground">
          {turn.sender_name && (
            <p className="text-[10px] font-semibold uppercase tracking-wide opacity-80">
              {turn.sender_name}
            </p>
          )}
          <p className="text-sm">{turn.content}</p>
          <p className="mt-1 text-[10px] opacity-70">
            {formatDate(turn.created_at)}
          </p>
        </div>
      </div>
    );
  }

  if (turn.role === "tool") {
    return (
      <div data-testid="bubble-tool" className="flex justify-start">
        <div className="max-w-[75%] rounded-md border border-dashed bg-muted/40 px-3 py-2 text-xs font-mono">
          <p className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
            tool result · {turn.sender_name ?? turn.tool_call_id}
          </p>
          <pre className="whitespace-pre-wrap break-all text-xs">
            {turn.content}
          </pre>
        </div>
      </div>
    );
  }

  return (
    <div
      data-testid="bubble-assistant"
      className={cn("flex justify-start", turn.role === "system" && "opacity-70")}
    >
      <div className="max-w-[75%] space-y-2">
        <div className="rounded-2xl rounded-bl-sm border bg-card px-4 py-2.5">
          <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
            {turn.role === "system" ? "System" : "Assistant"}
          </p>
          {turn.content && <p className="mt-0.5 text-sm">{turn.content}</p>}
          <p className="mt-1 text-[10px] text-muted-foreground">
            {formatDate(turn.created_at)}
          </p>
        </div>
        {turn.tool_calls.map((call) => (
          <ToolCallCard key={call.id} call={call} />
        ))}
      </div>
    </div>
  );
}

function ToolCallCard({ call }: { call: ToolCall }) {
  const [open, setOpen] = useState(false);
  return (
    <div
      data-testid={`tool-call-${call.id}`}
      className="rounded-md border bg-muted/30"
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-xs font-medium"
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown className="h-3 w-3" />
        ) : (
          <ChevronRight className="h-3 w-3" />
        )}
        <Wrench className="h-3 w-3 text-muted-foreground" />
        <span className="font-mono">{call.name}</span>
        <Badge variant="outline" className="ml-auto text-[10px]">
          tool
        </Badge>
      </button>
      {open && (
        <pre className="border-t bg-background/60 px-3 py-2 text-xs font-mono overflow-x-auto">
          {JSON.stringify(call.arguments, null, 2)}
        </pre>
      )}
    </div>
  );
}
