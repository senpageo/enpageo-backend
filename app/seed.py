from geoalchemy2.elements import WKTElement

from .database import Base, SessionLocal, engine
from .models import Location

LOCATIONS = [
    {"name": "Brandenburger Tor", "lat": 52.5163, "lng": 13.3777, "category": "Wahrzeichen"},
    {"name": "Alexanderplatz", "lat": 52.5219, "lng": 13.4132, "category": "Platz"},
    {"name": "Potsdamer Platz", "lat": 52.5096, "lng": 13.3759, "category": "Platz"},
    {"name": "Reichstag", "lat": 52.5186, "lng": 13.3762, "category": "Wahrzeichen"},
    {"name": "Berlin Hauptbahnhof", "lat": 52.5251, "lng": 13.3694, "category": "Verkehr"},
    {"name": "Museumsinsel", "lat": 52.5169, "lng": 13.4020, "category": "Kultur"},
]


def seed():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(Location).count() > 0:
            print("Locations already seeded, skipping.")
            return
        for loc in LOCATIONS:
            point = WKTElement(f"POINT({loc['lng']} {loc['lat']})", srid=4326)
            db.add(Location(name=loc["name"], category=loc["category"], geom=point))
        db.commit()
        print(f"Seeded {len(LOCATIONS)} locations.")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
