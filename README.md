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

## Stack

- Backend: Python 3.11, FastAPI, SQLAlchemy, APScheduler
- LLM: DeepSeek via OpenAI-compatible SDK
- Frontend: React + Vite + TypeScript + Tailwind + shadcn/ui
- Messaging: WeChat ClawBot (iLink Bot API, official)
- Tests: pytest, real API calls only (no mocks)

## Status

Under active construction — see the open PRs for current progress.
