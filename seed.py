"""Demo data so the app is useful the moment it starts."""
import db
import markets

ITEMS = [
    # name, category, unit, qty, par level
    ("Rice", "Grains", "lb", 40, 200),
    ("Pasta", "Grains", "box", 145, 150),
    ("Flour", "Grains", "lb", 90, 80),
    ("Cereal", "Grains", "box", 12, 60),
    ("Beans", "Protein", "lb", 30, 150),
    ("Canned Tuna", "Protein", "can", 88, 120),
    ("Peanut Butter", "Protein", "jar", 9, 75),
    ("Chicken", "Protein", "lb", 55, 50),
    ("Canned Corn", "Canned", "can", 210, 200),
    ("Canned Tomatoes", "Canned", "can", 34, 150),
    ("Milk", "Dairy", "gallon", 6, 40),
    ("Eggs", "Dairy", "dozen", 18, 60),
    ("Apples", "Produce", "lb", 25, 80),
    ("Potatoes", "Produce", "lb", 110, 100),
    ("Onions", "Produce", "lb", 20, 60),
    ("Carrots", "Produce", "lb", 44, 50),
    ("Bread", "Bakery", "loaf", 15, 70),
    ("Cooking Oil", "Pantry", "bottle", 11, 40),
    ("Sugar", "Pantry", "lb", 62, 50),
    ("Diapers", "Household", "each", 80, 300),
    ("Soap", "Household", "bar", 140, 120),
    ("Baby Formula", "Household", "can", 3, 25),
]

MARKETS = [
    ("Corner Grocery", 0.8),
    ("Aldi", 2.4),
    ("Walmart Supercenter", 4.1),
    ("Safeway", 5.3),
    ("WinCo Foods", 7.6),
    ("Restaurant Depot", 9.2),
    ("Costco Business Center", 12.5),
    ("Whole Foods Market", 3.7),
]


def seed_if_empty():
    with db.session() as conn:
        if conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]:
            return False
        conn.executemany(
            "INSERT OR IGNORE INTO items (name, category, unit, qty, par_level) "
            "VALUES (?, ?, ?, ?, ?)", ITEMS,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO markets (name, distance) VALUES (?, ?)", MARKETS,
        )
    markets.fill_demo_gaps([row[0] for row in ITEMS])
    return True


if __name__ == "__main__":
    db.init()
    print("seeded" if seed_if_empty() else "already has data")
