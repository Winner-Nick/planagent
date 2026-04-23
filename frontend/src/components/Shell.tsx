import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import {
  LayoutDashboard,
  MessagesSquare,
  Moon,
  Settings,
  Sun,
} from "lucide-react";

import { cn } from "../lib/utils";
import { Button } from "./ui/button";

const THEME_KEY = "planagent:theme";

function useTheme(): [string, (next: string) => void] {
  const [theme, setTheme] = useState<string>(() => {
    if (typeof window === "undefined") return "light";
    const stored = window.localStorage.getItem(THEME_KEY);
    if (stored === "light" || stored === "dark") return stored;
    return window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  });

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "dark") root.classList.add("dark");
    else root.classList.remove("dark");
    window.localStorage.setItem(THEME_KEY, theme);
  }, [theme]);

  return [theme, setTheme];
}

const navItems = [
  { to: "/", label: "Plans", icon: LayoutDashboard, end: true },
  { to: "/groups", label: "Groups", icon: MessagesSquare, end: false },
  { to: "/settings", label: "Settings", icon: Settings, end: false },
];

export function Shell() {
  const [theme, setTheme] = useTheme();

  return (
    <div className="min-h-screen bg-background text-foreground">
      <div className="flex min-h-screen">
        <aside className="hidden w-64 shrink-0 border-r bg-card/40 px-4 py-6 md:flex md:flex-col">
          <div className="mb-8 flex items-center gap-2 px-2">
            <div className="h-8 w-8 rounded-lg bg-primary" />
            <div>
              <p className="text-sm font-semibold tracking-tight">PlanAgent</p>
              <p className="text-xs text-muted-foreground">Group operations</p>
            </div>
          </div>
          <nav className="flex flex-col gap-1">
            {navItems.map((item) => {
              const Icon = item.icon;
              return (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  className={({ isActive }) =>
                    cn(
                      "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                      isActive
                        ? "bg-accent text-accent-foreground"
                        : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
                    )
                  }
                >
                  <Icon className="h-4 w-4" />
                  {item.label}
                </NavLink>
              );
            })}
          </nav>
          <div className="mt-auto px-3 pt-8 text-xs text-muted-foreground">
            <p>v0.1.0</p>
            <p>PR-C · fixture mode</p>
          </div>
        </aside>

        <div className="flex flex-1 flex-col">
          <header className="sticky top-0 z-20 flex h-14 items-center justify-between border-b bg-background/80 px-6 backdrop-blur">
            <div className="flex items-center gap-3">
              <h1 className="text-sm font-semibold tracking-tight">
                Dashboard
              </h1>
            </div>
            <div className="flex items-center gap-2">
              <Button
                variant="ghost"
                size="icon"
                aria-label="Toggle theme"
                onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
              >
                {theme === "dark" ? (
                  <Sun className="h-4 w-4" />
                ) : (
                  <Moon className="h-4 w-4" />
                )}
              </Button>
            </div>
          </header>
          <main className="flex-1 px-6 py-8">
            <Outlet />
          </main>
        </div>
      </div>
    </div>
  );
}
