import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";

export function Settings() {
  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-3xl font-semibold tracking-tight">Settings</h2>
        <p className="text-sm text-muted-foreground">
          Configuration lives here once the backend exposes it.
        </p>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>Coming soon</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          Reminder channels, API tokens, and timezone preferences will land in a
          follow-up PR.
        </CardContent>
      </Card>
    </div>
  );
}
