"""In-memory read-through / write-through cache over the ``chat_members`` collection.

Per-(chat, user) **affinity** (0–1) and **mode** (``auto``/``quiet``/``chatty``) drive the
group ambient gate. Reading them on every non-addressed group message would add a DB
round-trip to the hot path, so this cache serves warm entries from memory and falls back to
a single DB read (plus default creation) only on a miss. Writes go through to
``chat_members`` and hydrate the cache from the committed document. A per-member
lock serializes updates within this process without blocking unrelated members.

Lives in its own module (rather than ``group_gate.py``) so the affinity store and the
ambient funnel can evolve independently without edit collisions.

Bounded state: like ``UserState`` eviction in ``user_task_manager``, the cache is pruned by
idle time (see :meth:`AffinityCache.prune`) so it cannot grow without limit across many
distinct chats/members.

DM guard: affinity has no meaning in private chats (Requirement 4.8). Callers MUST NOT
invoke this cache for DMs — the DM path never consults or creates ``chat_members``. This
class deliberately holds no special private-chat logic; correctness depends on the caller
routing only group/supergroup speakers here.
"""

import asyncio
import math
import time
from weakref import WeakValueDictionary

from loguru import logger

from app.config import config
from app.database import models


class AffinityCache:
    """Read-through / write-through in-memory cache over ``chat_members``.

    The cache is keyed by ``(chat_id, user_id)`` and each entry holds the member's current
    ``affinity`` and ``mode`` plus a ``last_access`` timestamp used for idle eviction.
    """

    def __init__(self):
        # (chat_id, user_id) -> {"affinity": float, "mode": str, "last_access": float}
        self._cache: dict[tuple[int, int], dict] = {}
        # Strong references held by callers/waiters keep a lock alive while in
        # use; unused locks disappear without an unbounded second member map.
        self._locks: WeakValueDictionary[tuple[int, int], asyncio.Lock] = (
            WeakValueDictionary()
        )

    def _lock_for(self, key: tuple[int, int]) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def _store(self, key: tuple[int, int], doc: dict) -> dict:
        try:
            affinity = float(doc.get("affinity", config.AFFINITY_DEFAULT))
        except TypeError, ValueError, OverflowError:
            affinity = config.AFFINITY_DEFAULT
        if not math.isfinite(affinity):
            affinity = config.AFFINITY_DEFAULT
        mode = doc.get("mode", "auto")
        if mode not in ("auto", "quiet", "chatty"):
            mode = "auto"
        self._cache[key] = {
            "affinity": max(0.0, min(1.0, affinity)),
            "mode": mode,
            "last_access": time.time(),
        }
        return {"affinity": self._cache[key]["affinity"], "mode": mode}

    async def get(self, db, chat_id: int, user_id: int) -> dict:
        """Return ``{"affinity", "mode"}`` for the member, serving from cache when warm.

        On a cache miss this performs exactly one DB read via ``models.get_chat_member``.
        If the member record does not yet exist it is created with defaults
        (``affinity=config.AFFINITY_DEFAULT``, ``mode="auto"``) and persisted with a single
        ``upsert_chat_member`` so subsequent reads of cold members are stable. The result
        is cached, so a subsequent ``get`` for the same member does NOT hit the DB.
        """
        async with self._lock_for((chat_id, user_id)):
            return await self._get(db, chat_id, user_id)

    async def _get(self, db, chat_id: int, user_id: int) -> dict:
        """Read while the caller holds this member's lock."""
        key = (chat_id, user_id)
        entry = self._cache.get(key)
        if entry is not None:
            entry["last_access"] = time.time()
            return {"affinity": entry["affinity"], "mode": entry["mode"]}

        # Cache miss: one DB read.
        doc = await models.get_chat_member(db, chat_id, user_id)
        if doc is None:
            # Miss-create: persist defaults once so the record exists going forward.
            doc = await models.upsert_chat_member(db, chat_id, user_id)

        return self._store(key, doc)

    async def bump(self, db, chat_id: int, user_id: int, delta: float) -> float:
        """Adjust the member's affinity by ``delta``, clamp to [0, 1], write through.

        Reads the current value (via :meth:`get`, so cold members are created first),
        computes the clamped new affinity, persists it through ``upsert_chat_member``,
        updates the cache, and returns the new value.
        """
        if not math.isfinite(delta):
            raise ValueError("affinity delta must be finite")
        key = (chat_id, user_id)
        async with self._lock_for(key):
            current = await self._get(db, chat_id, user_id)
            new_affinity = max(0.0, min(1.0, current["affinity"] + delta))
            doc = await models.upsert_chat_member(
                db, chat_id, user_id, affinity=new_affinity
            )
            self._store(key, doc)

        logger.debug(
            "affinity bump {}:{} delta={:+.3f} -> {:.3f}",
            chat_id,
            user_id,
            delta,
            new_affinity,
        )
        return new_affinity

    async def set_mode(self, db, chat_id: int, user_id: int, mode: str) -> None:
        """Set the member's ``mode``, write it through, and update the cache."""
        key = (chat_id, user_id)
        async with self._lock_for(key):
            doc = await models.upsert_chat_member(db, chat_id, user_id, mode=mode)
            self._store(key, doc)

        logger.debug("affinity set_mode {}:{} -> {}", chat_id, user_id, mode)

    def prune(self, now: float, max_idle: float) -> int:
        """Evict cache entries idle longer than ``max_idle`` seconds; return count pruned.

        Mirrors the idle-eviction policy of ``UserState`` so the cache stays bounded across
        unbounded distinct chats/members. Eviction is harmless for correctness: a pruned
        member is simply re-read from the DB on its next ``get``.
        """
        stale = [
            key
            for key, entry in self._cache.items()
            if now - entry["last_access"] > max_idle and key not in self._locks
        ]
        for key in stale:
            del self._cache[key]
        return len(stale)


# Module-level singleton, mirroring the other service singletons.
affinity_cache = AffinityCache()
