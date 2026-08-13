import uuid

from pydantic import BaseModel


class LeagueOption(BaseModel):
    slug: str
    name: str


class SportResponse(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    model_type: str
    league_count: int
    # The sport's own leagues, so a client can offer them as filters without a second call.
    #
    # ONLY POPULATED WHEN league_count <= LEAGUE_PICKER_MAX. This is data-driven rather than a
    # per-sport rule in the client: basketball's NBA/WNBA and tennis's ATP/WTA are competitions
    # a user thinks of separately and would filter by, while football's 18 leagues would turn a
    # dropdown into a scrolling list of everything. Football is already grouped by league inside
    # the feed itself, which is the right affordance at that count.
    leagues: list[LeagueOption] = []
