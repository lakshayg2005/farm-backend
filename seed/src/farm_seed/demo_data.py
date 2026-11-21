"""The curated demo cast: a small, believable slice of Durg district, Chhattisgarh.

Everything is fixed (no randomness) so every run and every machine shows the same story. Village points are
approximate: close enough to look right on a map, not survey data. All names are invented.

Logins (password PASSWORD for everyone except the admin):
    farmers      +91 91000 00001 ... 00008   (the last two are waiting for admin verification)
    customers    +91 92000 00001 ... 00010
    inspectors   +91 93000 00001 ... 00002
    delivery     +91 94000 00001 ... 00003
"""

from dataclasses import dataclass, field

PASSWORD = "Farm@12345"  # dev and demo only


@dataclass(frozen=True)
class Listing:
    crop: str  # catalog crop id
    variety: str
    description: str
    qty: float
    price: float  # per catalog unit of the crop (kg, quintal, piece, bunch, dozen)
    harvest_days_ago: int
    organic: bool = False
    photo: str | None = None  # key in seed/assets/photos


@dataclass(frozen=True)
class Farm:
    name: str
    village: str
    lat: float
    lng: float
    photo: str
    listings: tuple[Listing, ...]


@dataclass(frozen=True)
class Farmer:
    name: str
    phone: str
    verified: bool
    farms: tuple[Farm, ...] = ()


DISTRICT, STATE = "Durg", "Chhattisgarh"

FARMERS: tuple[Farmer, ...] = (
    Farmer(
        "Ramesh Sahu",
        "+919100000001",
        True,
        (
            Farm(
                "Sahu Krishi Farm",
                "Patan",
                21.0361,
                81.1752,
                "farm_2",
                (
                    Listing(
                        "rice",
                        "Dubraj",
                        "Aromatic Dubraj rice, sun dried and hand cleaned.",
                        18,
                        6200,
                        12,
                        False,
                        "rice",
                    ),
                    Listing(
                        "tomato",
                        "Hybrid",
                        "Firm red tomatoes picked this morning.",
                        160,
                        30,
                        1,
                        False,
                        "tomato",
                    ),
                    Listing(
                        "brinjal",
                        "Round purple",
                        "Tender round brinjal, good for bharta.",
                        90,
                        28,
                        1,
                        False,
                        "brinjal",
                    ),
                    Listing(
                        "bottle-gourd",
                        "Long green",
                        "Fresh lauki, about 1 kg each.",
                        120,
                        25,
                        2,
                        False,
                        "bottle_gourd",
                    ),
                ),
            ),
        ),
    ),
    Farmer(
        "Sunita Verma",
        "+919100000002",
        True,
        (
            Farm(
                "Verma Organic Farm",
                "Dhamdha",
                21.4142,
                81.3128,
                "farm_3",
                (
                    Listing(
                        "cauliflower",
                        "Snowball",
                        "Organic cauliflower, no chemical sprays.",
                        140,
                        32,
                        1,
                        True,
                        "cauliflower",
                    ),
                    Listing(
                        "cabbage",
                        "Green round",
                        "Organic cabbage heads, 800 g to 1 kg.",
                        180,
                        18,
                        1,
                        True,
                        "cabbage",
                    ),
                    Listing(
                        "carrot",
                        "Local red",
                        "Sweet red carrots, washed.",
                        110,
                        40,
                        2,
                        True,
                        "carrot",
                    ),
                    Listing(
                        "peas", "Green peas", "Fresh peas in the pod.", 70, 60, 1, True, "peas"
                    ),
                ),
            ),
        ),
    ),
    Farmer(
        "Mahesh Yadav",
        "+919100000003",
        True,
        (
            Farm(
                "Yadav Kisan Farm",
                "Ahiwara",
                21.1747,
                81.2943,
                "farm_1",
                (
                    Listing(
                        "onion", "Red", "Red onions, cured and dry.", 400, 24, 6, False, "onion"
                    ),
                    Listing(
                        "potato",
                        "Kufri Jyoti",
                        "Potatoes from our own field, good for storage.",
                        520,
                        22,
                        8,
                        False,
                        "potato",
                    ),
                    Listing(
                        "pumpkin",
                        "Kaddu",
                        "Ripe pumpkin, 3 to 5 kg each.",
                        150,
                        20,
                        5,
                        False,
                        "pumpkin",
                    ),
                ),
            ),
        ),
    ),
    Farmer(
        "Kavita Chandrakar",
        "+919100000004",
        True,
        (
            Farm(
                "Chandrakar Orchard",
                "Kumhari",
                21.2696,
                81.4603,
                "farm_1",
                (
                    Listing(
                        "guava",
                        "Allahabad Safeda",
                        "Sweet guavas from the orchard, handled with care.",
                        140,
                        45,
                        2,
                        False,
                        "guava",
                    ),
                    Listing(
                        "banana",
                        "Grand Naine",
                        "Ripe bananas, sold by the dozen.",
                        60,
                        55,
                        3,
                        False,
                        "banana",
                    ),
                    Listing(
                        "maize",
                        "Yellow",
                        "Dry maize, shelled and cleaned.",
                        12,
                        2300,
                        20,
                        False,
                        "maize",
                    ),
                ),
            ),
        ),
    ),
    Farmer(
        "Dinesh Tandon",
        "+919100000005",
        True,
        (
            Farm(
                "Tandon Grains",
                "Jamul",
                21.1458,
                81.3361,
                "farm_2",
                (
                    Listing(
                        "chana",
                        "Desi",
                        "Whole desi chana, machine cleaned.",
                        25,
                        6000,
                        25,
                        False,
                        "chana",
                    ),
                    Listing(
                        "wheat",
                        "Sharbati",
                        "Sharbati wheat, stone free.",
                        30,
                        2900,
                        30,
                        False,
                        "wheat",
                    ),
                    Listing(
                        "mustard",
                        "Black mustard",
                        "Mustard seed for oil pressing.",
                        14,
                        5800,
                        30,
                        False,
                        "mustard",
                    ),
                ),
            ),
        ),
    ),
    Farmer(
        "Meena Dewangan",
        "+919100000006",
        True,
        (
            Farm(
                "Dewangan Greens",
                "Nandini",
                21.1651,
                81.3395,
                "farm_3",
                (
                    Listing(
                        "spinach",
                        "Palak",
                        "Tender palak, cut at dawn.",
                        90,
                        12,
                        1,
                        False,
                        "spinach",
                    ),
                    Listing("okra", "Bhindi", "Slim, tender bhindi.", 70, 42, 1, False, "okra"),
                    Listing(
                        "peas", "Matar", "Early season matar in the pod.", 60, 62, 1, False, "peas"
                    ),
                ),
            ),
        ),
    ),
    # Waiting for the admin: they fill the verification queue and cannot sell yet.
    Farmer("Pooja Thakur", "+919100000007", False),
    Farmer("Vijay Nishad", "+919100000008", False),
)


@dataclass(frozen=True)
class Customer:
    name: str
    phone: str
    area: str
    lat: float
    lng: float


# Homes in Bhilai, Durg and Raipur: close enough together that pooling finds clusters.
CUSTOMERS: tuple[Customer, ...] = (
    Customer("Aarav Mishra", "+919200000001", "Sector 6, Bhilai", 21.1983, 81.3612),
    Customer("Neha Agrawal", "+919200000002", "Sector 9, Bhilai", 21.2107, 81.3731),
    Customer("Rohit Kumar", "+919200000003", "Supela, Bhilai", 21.2025, 81.3548),
    Customer("Priya Soni", "+919200000004", "Civic Center, Bhilai", 21.2124, 81.3897),
    Customer("Sanjay Dubey", "+919200000005", "Station Road, Durg", 21.1893, 81.2837),
    Customer("Anjali Tiwari", "+919200000006", "Nehru Nagar, Bhilai", 21.2189, 81.3459),
    Customer("Manoj Pandey", "+919200000007", "Padmanabhpur, Durg", 21.1956, 81.2912),
    Customer("Ritu Jain", "+919200000008", "Kohka, Bhilai", 21.2301, 81.3405),
    Customer("Harish Chandra", "+919200000009", "Shankar Nagar, Raipur", 21.2465, 81.6377),
    Customer("Divya Sharma", "+919200000010", "Tatibandh, Raipur", 21.2237, 81.6628),
)

INSPECTORS = (
    ("Asha Tiwari", "+919300000001", 21.1904, 81.2849),  # Durg
    ("Bharat Soni", "+919300000002", 21.2090, 81.3790),  # Bhilai
)
PARTNERS = (
    ("Deepak Rana", "+919400000001"),
    ("Esha Khan", "+919400000002"),
    ("Farhan Ali", "+919400000003"),
)


@dataclass(frozen=True)
class PlotSpec:
    farmer: int  # index into FARMERS
    title: str
    lat: float
    lng: float
    side_m: float
    soil: str
    water: str
    crops: tuple[str, ...]
    price_per_kg: float  # per kg of expected harvest


PLOTS: tuple[PlotSpec, ...] = (
    PlotSpec(
        0,
        "Patan river-side plot",
        21.0402,
        81.1810,
        120,
        "Alluvial loam",
        "Canal",
        ("rice", "tomato", "brinjal"),
        14,
    ),
    PlotSpec(
        1,
        "Dhamdha polyhouse field",
        21.4180,
        81.3180,
        90,
        "Sandy loam",
        "Borewell",
        ("cauliflower", "cabbage", "peas", "carrot"),
        22,
    ),
    PlotSpec(
        2,
        "Ahiwara north field",
        21.1790,
        81.2990,
        130,
        "Black cotton soil",
        "Borewell",
        ("onion", "potato", "wheat"),
        12,
    ),
    PlotSpec(
        4,
        "Jamul chana field",
        21.1500,
        81.3420,
        140,
        "Red loam",
        "Rain fed and borewell",
        ("chana", "wheat", "mustard"),
        9,
    ),
    PlotSpec(
        4,
        "Jamul mustard strip",
        21.1535,
        81.3480,
        100,
        "Red loam",
        "Rain fed",
        ("mustard", "chana"),
        10,
    ),
    PlotSpec(
        3,
        "Kumhari orchard block",
        21.2730,
        81.4660,
        110,
        "Loam",
        "Borewell",
        ("guava", "banana", "maize"),
        18,
    ),
    PlotSpec(
        5,
        "Nandini kitchen garden",
        21.1690,
        81.3440,
        90,
        "Loam",
        "Borewell",
        ("spinach", "okra", "peas"),
        20,
    ),
)

# Seasons offered to customers all lie ahead. Two plots also keep last Kharif (already harvested), because
# the harvest and delivery story needs a season that has finished.
SEASONS = (
    ("Kharif", "2027-06-16", "2027-10-31"),
    ("Rabi", "2026-11-15", "2027-03-31"),
    ("Zaid", "2027-04-01", "2027-06-15"),
)
PAST_KHARIF = ("Kharif", "2026-06-15", "2026-10-31")
PLOTS_WITH_PAST_KHARIF = (0, 6)


@dataclass(frozen=True)
class ReservationSpec:
    customer: int
    plot: int  # index into PLOTS
    season: str
    crop: str
    qty_kg: float
    stage: str  # PENDING, ACCEPTED, REJECTED, CANCELLED, ACTIVE, HARVEST_READY, HARVESTED
    updates: tuple[tuple[str, str, str, int], ...] = field(
        default_factory=tuple
    )  # (stage, note, photo, days ago)


RESERVATIONS: tuple[ReservationSpec, ...] = (
    ReservationSpec(0, 1, "Rabi", "cauliflower", 300, "PENDING"),
    ReservationSpec(1, 0, "Rabi", "tomato", 240, "ACCEPTED"),
    ReservationSpec(2, 2, "Rabi", "wheat", 600, "REJECTED"),
    ReservationSpec(3, 5, "Rabi", "maize", 400, "CANCELLED"),
    ReservationSpec(
        4,
        3,
        "Rabi",
        "chana",
        500,
        "ACTIVE",
        (
            (
                "SOWN",
                "Sowing finished on the whole plot. Seed treated and the soil was moist.",
                "stage_sown",
                12,
            ),
            (
                "GROWING",
                "Plants are 15 cm tall and the rows are even. First weeding done.",
                "stage_growing_wheat",
                4,
            ),
        ),
    ),
    ReservationSpec(
        5,
        4,
        "Rabi",
        "mustard",
        350,
        "ACTIVE",
        (
            ("SOWN", "Mustard sown in rows 30 cm apart.", "stage_sown", 20),
            (
                "GROWING",
                "Good germination across the strip. Irrigated once.",
                "stage_growing_paddy",
                9,
            ),
            ("FLOWERING", "First yellow flowers have opened.", "mustard", 1),
        ),
    ),
    ReservationSpec(
        6,
        6,
        "Kharif",
        "okra",
        180,
        "HARVEST_READY",
        (
            ("SOWN", "Okra sown at the start of the season.", "stage_sown", 120),
            ("GROWING", "Healthy plants, picking will start soon.", "stage_growing_paddy", 80),
            ("FLOWERING", "Flowering well, pods forming.", "okra", 50),
        ),
    ),
    ReservationSpec(
        7,
        0,
        "Kharif",
        "rice",
        800,
        "HARVESTED",
        (
            ("SOWN", "Paddy transplanted after the first rains.", "stage_sown", 130),
            ("GROWING", "Green and healthy, water level kept steady.", "stage_growing_paddy", 90),
            ("FLOWERING", "Panicles are out, grains filling.", "stage_ready_paddy", 55),
        ),
    ),
)
