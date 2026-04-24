# PlanAgent

An LLM-native plan manager. Lives in your WeChat group, extracts plans from free-form chat, asks follow-up questions until plans are well-defined, and nudges you on schedule.

## Architecture (one-line)

WeChat ClawBot ⇄ FastAPI gateway ⇄ DeepSeek tool-using agent ⇄ SQLite plans ⇄ React dashboard.

Every natural-language decision — what's a plan, what's missing, when to nudge, what to say — is delegated to the LLM. The backend provides tools and persistence, not heuristics.

## Quickstart

```bash
# 1. Install
cd backend && pip install -e '.[dev]'
cd ../frontend && npm install

# 2. Configure
cp .env.example .env   # fill DEEPSEEK_API_KEY

# 3. Scan WeChat bot (one-time)
python -m planagent.wechat.login

# 4. Run
uvicorn planagent.main:app --reload
cd frontend && npm run dev
```

## Live dev

The React dashboard talks to the live FastAPI bridge by default (no fixtures).
Run three terminals:

```bash
# Terminal 1 — FastAPI (serves /api/v1, enables CORS for :5173)
cd backend && uvicorn planagent.main:app --reload

# Terminal 2 — WeChat bridge (writes plans into the shared SQLite DB)
cd backend && python -m planagent.wechat.bridge

# Terminal 3 — React dashboard, reads VITE_API_BASE from .env.development
cd frontend && npm run dev
```

Then open http://localhost:5173. The Plans board groups cards by owner
(鹏鹏 / 辰辰) and shows each plan's next reminder fire time.

Switches:

- `VITE_API_BASE` — backend origin (default `http://localhost:8000`).
- `VITE_USE_FIXTURES=1` — serve offline fixture data instead of hitting the
  backend. Use this for screenshots, tests, or demoing without the bot
  running. Any other value (including unset) means live mode.

When live mode cannot reach the backend the UI renders an inline
「后端未连接」 banner instead of crashing.

## Stack

- Backend: Python 3.11, FastAPI, SQLAlchemy, APScheduler
- LLM: DeepSeek via OpenAI-compatible SDK
- Frontend: React + Vite + TypeScript + Tailwind + shadcn/ui
- Messaging: WeChat ClawBot (iLink Bot API, official)
- Tests: pytest, real API calls only (no mocks)

## Running the bridge (production)

The bridge is the long-running process that drives WeChat I/O, the
scheduler, and agent turns. For day-to-day dev you can just do:

```bash
cd backend
python -m planagent.wechat.bridge --scheduler-interval-s 300
```

On startup the bridge writes its PID to `/tmp/planagent-bridge.pid` and
removes it on clean shutdown. `SIGTERM` triggers a graceful shutdown
bounded by ~5 s.

### Health check

```bash
python -m planagent.wechat.bridge --health-check
```

Prints a JSON summary (uptime, sessions, last inbound/outbound per
session, open pending outbounds, scheduler liveness). Exit 0 iff the
bridge is alive, 1 otherwise — suitable for a systemd `ExecStartPre`,
k8s liveness probe, or cron-based watchdog.

### systemd

A unit template lives at [`deploy/planagent-bridge.service`](deploy/planagent-bridge.service).
It is NOT installed automatically. Edit the `__REPLACE_WITH_*__` markers
(user, group, repo path), then:

```bash
sudo cp deploy/planagent-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now planagent-bridge
journalctl -u planagent-bridge -f | jq .
```

### Structured logs

Every significant bridge event prints one JSON line to stderr, tagged
with `event` + a handful of contextual fields. The contract:

| event                      | fields                                                               |
| -------------------------- | -------------------------------------------------------------------- |
| `inbound_received`         | `session_name`, `wechat_user_id`, `text_preview`, `context_token`    |
| `outbound_sent`            | `session_name`, `target_user_id`, `text_preview`, `client_id`        |
| `reminder_fired`           | `plan_id`, `owner`, `fire_at`, `message_preview`                     |
| `wakeup_decision`          | `session_name`, `should_ping`, `reason`                              |
| `pending_outbound_flushed` | `pending_id`, `target_user_id`                                       |
| `handler_failed`           | `session_name`, `error`, `exc_type`                                  |

Use `jq` to filter:

```bash
journalctl -u planagent-bridge -f | jq -r 'select(.event=="inbound_received")'
```

### Log rotation

Two options — pick one:

1. **logrotate** (if you write logs to a file via `systemd`/`tee`):

   ```
   /var/log/planagent/bridge.log {
       daily
       rotate 14
       compress
       missingok
       notifempty
       copytruncate
   }
   ```

2. **Python-side rotation**: call
   `planagent.logutil.setup_json_logging(enable_file_rotation=True)`
   (already invoked inside `main()` when you pass `PLANAGENT_LOG_TO_FILE=1`
   — see the systemd unit). Rolls `~/.planagent/logs/bridge.log` at
   midnight via `TimedRotatingFileHandler`, keeping 14 days of backups.

## Status

Under active construction — see the open PRs for current progress.
