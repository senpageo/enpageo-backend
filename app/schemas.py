from typing import Optional

from pydantic import BaseModel


class LocationOut(BaseModel):
    id: int
    name: str
    category: Optional[str] = None
    description: Optional[str] = None
    latitude: float
    longitude: float

    class Config:
        from_attributes = True
