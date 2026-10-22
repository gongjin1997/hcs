from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class IdsIn(BaseModel):
    ids: list[int]


class NoteIn(BaseModel):
    note: str | None = None


class Timestamped(ORMModel):
    created_at: datetime | None = None
