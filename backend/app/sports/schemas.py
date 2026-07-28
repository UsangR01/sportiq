import uuid

from pydantic import BaseModel


class SportResponse(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    model_type: str
    league_count: int
