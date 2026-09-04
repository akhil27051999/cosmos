"""
Redis-backed DISTRIBUTED token bucket (rate limiter).

The in-memory TokenBucket only limits one process. Run several gateway/worker
replicas and each would allow the full rate — so the real limit is N× too high.
This adapter keeps the bucket in Redis and refills/consumes it with a **Lua
script**, which Redis runs atomically. Atomicity is the whole point: without it,
two replicas could each read "1 token left" and both consume it (a race). One
Lua script = one indivisible check-and-decrement, shared by every replica.
"""

from __future__ import annotations

import time

from redis.asyncio import Redis

# KEYS[1]=bucket key; ARGV: rate/sec, capacity, now_ms, requested. Returns 1/0.
_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = capacity; ts = now end
local elapsed = math.max(0, now - ts) / 1000.0
tokens = math.min(capacity, tokens + elapsed * rate)
local allowed = 0
if tokens >= requested then tokens = tokens - requested; allowed = 1 end
redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', key, math.ceil(capacity / rate * 1000) + 1000)
return allowed
"""


class RedisTokenBucket:
    def __init__(self, redis: Redis, key: str, capacity: float, refill_per_sec: float) -> None:
        self._redis = redis
        self._key = key
        self._capacity = capacity
        self._rate = refill_per_sec

    async def try_acquire(self, tokens: int = 1) -> bool:
        now_ms = int(time.time() * 1000)
        allowed = await self._redis.eval(
            _LUA, 1, self._key, self._rate, self._capacity, now_ms, tokens
        )
        return int(allowed) == 1
