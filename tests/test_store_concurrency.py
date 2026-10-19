"""Several gateway processes on one database -- one per Claude window.

Measured with four windows against Browser Use: every acquire in one window
failed with "database is locked". This drives the read-then-write acquire
pattern from four processes at once and requires that none of them fails.
(It is a guard, not a reproduction: the original failure needed the timing of
four real gateways starting together and did not reproduce in this harness.)
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import cbmcp

WORKER = textwrap.dedent(
    """
    import asyncio, sys
    from pathlib import Path
    from cbmcp.store import Store, new_lease_token

    async def main(db, me):
        store = Store(Path(db))
        await store.open()
        for i in range(60):
            # read with fetchone-style calls, then write: the acquire pattern
            await store.usage_for_day()
            await store.reserved_total()
            await store.get_lease(f"lse_{me}_{i - 1}")
            await store.insert_lease(
                lease_id=f"lse_{me}_{i}", token=new_lease_token(), provider="fake",
                state="creating", owner=None, task_label=None, idempotency_key=None,
                scope=[], network_mode="direct", expires_at=None, instance_id=me)
            await store.update_lease(f"lse_{me}_{i}", state="active")
            await store.heartbeat(me, pid=1, host="h")
            await store.audit("lease.acquire", lease_id=f"lse_{me}_{i}", outcome="ok")
        await store.close()

    asyncio.run(main(sys.argv[1], sys.argv[2]))
    """
)


def test_four_gateway_processes_never_hit_database_is_locked(tmp_path: Path):
    db = tmp_path / "shared.sqlite3"
    src = str(Path(cbmcp.__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": src}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", WORKER, str(db), f"w{n}"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for n in range(4)
    ]
    failures = []
    for proc in procs:
        _, err = proc.communicate(timeout=180)
        if proc.returncode != 0:
            failures.append(err.strip().splitlines()[-1] if err.strip() else f"rc={proc.returncode}")
    assert not failures, failures
