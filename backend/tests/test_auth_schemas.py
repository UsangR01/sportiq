"""RegisterRequest.guest_session_id regression test.

pydantic-core's strict UUID validator rejects a JSON string outright — it demands an actual
UUID instance, which no JSON body can ever supply. Every real client sending a guest_session_id
to migrate guest filter state into a new account (TDD §2.1) hit a 422 until this was found and
fixed with a per-field strict=False override (app/auth/schemas.py) — discovered live once the
mobile app first exercised this path with a real id instead of null.
"""

import uuid

from app.auth.schemas import RegisterRequest


def test_guest_session_id_accepts_a_real_uuid_string_from_json():
    body = RegisterRequest.model_validate_json(
        '{"email": "a@b.com", "password": "x", '
        '"guest_session_id": "c2873bda-ffd4-48b0-9acd-49be502cb912"}'
    )
    assert body.guest_session_id == uuid.UUID("c2873bda-ffd4-48b0-9acd-49be502cb912")


def test_guest_session_id_still_defaults_to_none():
    body = RegisterRequest.model_validate_json('{"email": "a@b.com", "password": "x"}')
    assert body.guest_session_id is None
