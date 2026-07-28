import json
import uuid

from redis.asyncio import Redis

GUEST_SESSION_TTL_SECONDS = 24 * 60 * 60


def guest_session_key(guest_session_id: uuid.UUID) -> str:
    return f"guest_session:{guest_session_id}"


async def create_guest_session(redis: Redis) -> uuid.UUID:
    guest_session_id = uuid.uuid4()
    await redis.set(
        guest_session_key(guest_session_id), json.dumps({}), ex=GUEST_SESSION_TTL_SECONDS
    )
    return guest_session_id


async def get_guest_session(redis: Redis, guest_session_id: uuid.UUID) -> dict | None:
    raw = await redis.get(guest_session_key(guest_session_id))
    return json.loads(raw) if raw is not None else None


async def set_guest_session(redis: Redis, guest_session_id: uuid.UUID, state: dict) -> None:
    await redis.set(
        guest_session_key(guest_session_id), json.dumps(state), ex=GUEST_SESSION_TTL_SECONDS
    )
