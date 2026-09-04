# invincible/agent/__init__.py
"""Local agent package (Phase 10).

Two modules, one split: ``sandbox`` decides what the agent may touch
on the user's machine (its walls are HOME-relative - the server's
repo-relative write denylist matches nothing outside the server repo
and cannot be reused here), and ``runner`` is the polling loop plus
the local executors that reuse tool_executor's exact run/write code
so behavior is byte-identical wherever the work happens.
"""
