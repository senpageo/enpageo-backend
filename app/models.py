from geoalchemy2 import Geometry
from sqlalchemy import Column, Integer, String, Text

from .database import Base


class Location(Base):
    __tablename__ = "locations"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    category = Column(String(100), nullable=True)
    description = Column(Text, nullable=True)
    geom = Column(Geometry(geometry_type="POINT", srid=4326), nullable=False)
