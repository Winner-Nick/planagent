"""Small, pure utility helpers with no cross-package deps.

Kept deliberately tiny: each module here must be unit-testable without
touching the DB, HTTP, or the LLM client. Anything with I/O belongs in
its feature package (agent, scheduler, wechat, ...).
"""
