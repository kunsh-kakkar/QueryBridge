"""
Deterministic sample-data generator for the QueryBridge demo database.

``generate_sample_data(seed)`` always returns the same rows for the same seed,
so the evaluation's gold answers stay reproducible. The data follows
real-world business rules, and the tests check each one:

* every sale happens after the customer registered and after the selling
  employee joined; only Sales-department employees sell;
* ``sale_amount = price x quantity x (1 - discount)`` with segment-based
  discounts, more sales in Q4 and more transactions each year;
* a three-level hierarchy: the CEO, department heads who report to the CEO,
  and staff who report to their department head;
* each project has exactly one Lead; members joined before the project started;
* reviews are written by the employee's manager, and each employee's ratings
  stay fairly consistent from review to review (a hidden per-person level).
"""

from __future__ import annotations

import calendar
import random
from dataclasses import dataclass
from datetime import date, timedelta

DEFAULT_SEED = 42
DATA_START = date(2023, 1, 1)
DATA_END = date(2026, 10, 31)
NUM_SALES = 2500

# Insert order respects foreign keys (managers are inserted before reports).
TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "departments": ("department_id", "department_name", "location", "budget"),
    "employees": (
        "employee_id", "employee_name", "email", "job_title",
        "department_id", "salary", "joining_date", "manager_id",
    ),
    "products": ("product_id", "product_name", "category", "price", "stock_quantity"),
    "customers": ("customer_id", "customer_name", "city", "country", "registration_date", "segment"),
    "sales": ("sale_id", "employee_id", "product_id", "customer_id", "quantity", "sale_amount", "sale_date"),
    "projects": ("project_id", "project_name", "department_id", "start_date", "end_date", "status", "budget"),
    "employee_projects": ("employee_id", "project_id", "role", "hours_allocated"),
    "performance_reviews": ("review_id", "employee_id", "reviewer_id", "review_date", "rating", "comments"),
}

DEPARTMENTS: list[tuple[int, str, str, float]] = [
    (1, "Engineering", "San Francisco", 4_800_000.00),
    (2, "Sales", "New York", 2_600_000.00),
    (3, "Marketing", "London", 1_400_000.00),
    (4, "Finance", "New York", 900_000.00),
    (5, "Human Resources", "Toronto", 650_000.00),
    (6, "Operations", "Chicago", 1_100_000.00),
    (7, "Customer Support", "Bengaluru", 750_000.00),
    (8, "Research & Development", "Berlin", 2_100_000.00),
]
SALES_DEPARTMENT_ID = 2
CEO_DEPARTMENT_ID = 6

# department_id -> staffing profile
DEPARTMENT_PROFILES: dict[int, dict] = {
    1: {"staff": 12, "salary": (72_000, 165_000), "head": "VP of Engineering",
        "titles": ["Software Engineer", "Senior Software Engineer", "DevOps Engineer", "QA Engineer", "Data Engineer"]},
    2: {"staff": 12, "salary": (42_000, 120_000), "head": "Head of Sales",
        "titles": ["Account Executive", "Senior Account Executive", "Sales Representative",
                   "Sales Development Representative"]},
    3: {"staff": 5, "salary": (45_000, 105_000), "head": "Marketing Director",
        "titles": ["Marketing Specialist", "Content Strategist", "SEO Analyst", "Brand Manager"]},
    4: {"staff": 4, "salary": (55_000, 125_000), "head": "Chief Financial Officer",
        "titles": ["Financial Analyst", "Accountant", "Senior Accountant"]},
    5: {"staff": 3, "salary": (40_000, 90_000), "head": "HR Director",
        "titles": ["HR Generalist", "Recruiter", "HR Coordinator"]},
    6: {"staff": 5, "salary": (38_000, 95_000), "head": "Operations Director",
        "titles": ["Operations Analyst", "Logistics Coordinator", "Procurement Specialist"]},
    7: {"staff": 6, "salary": (32_000, 68_000), "head": "Support Manager",
        "titles": ["Support Specialist", "Senior Support Specialist", "Technical Support Engineer"]},
    8: {"staff": 4, "salary": (80_000, 170_000), "head": "Head of Research",
        "titles": ["Research Scientist", "Machine Learning Engineer", "Research Engineer"]},
}

FIRST_NAMES = [
    "Aarav", "Olivia", "Liam", "Sofia", "Noah", "Mia", "Ethan", "Priya", "Lucas", "Amelia",
    "Mateo", "Hana", "James", "Chloe", "Arjun", "Emma", "Daniel", "Yuki", "Omar", "Grace",
    "Leo", "Fatima", "Benjamin", "Isabella", "Ravi", "Zoe", "Samuel", "Aisha", "Henry", "Elena",
    "Kenji", "Nora", "Carlos", "Maya", "David", "Leila", "Jonas", "Ananya", "Felix", "Clara",
]
LAST_NAMES = [
    "Sharma", "Johnson", "Nguyen", "Garcia", "Muller", "Okafor", "Smith", "Tanaka", "Rossi", "Kim",
    "Patel", "Brown", "Silva", "Chen", "Kowalski", "Haddad", "Wilson", "Fischer", "Reddy", "Martin",
    "Lopez", "Andersson", "Ivanova", "Dubois", "Mensah", "Clarke", "Moreau", "Singh", "Novak", "Lee",
]

CITIES: list[tuple[str, str]] = [
    ("New York", "USA"), ("San Francisco", "USA"), ("Chicago", "USA"), ("Austin", "USA"),
    ("Toronto", "Canada"), ("Vancouver", "Canada"), ("London", "UK"), ("Manchester", "UK"),
    ("Berlin", "Germany"), ("Munich", "Germany"), ("Paris", "France"), ("Bengaluru", "India"),
    ("Mumbai", "India"), ("Singapore", "Singapore"), ("Sydney", "Australia"),
]
COMPANY_PREFIXES = [
    "Apex", "Blue River", "Summit", "Northwind", "Silverline", "Redwood", "Pioneer", "Brightstar",
    "Ironclad", "Evergreen", "Harbor", "Crescent", "Vertex", "Lakeside", "Golden Gate", "Maple",
    "Quantum", "Horizon", "Atlas", "Cobalt",
]
COMPANY_INDUSTRIES = [
    "Logistics", "Foods", "Technologies", "Retail", "Health", "Manufacturing",
    "Consulting", "Media", "Energy", "Financial", "Labs", "Hospitality",
]
COUNTRY_SUFFIX = {"USA": "Inc.", "Canada": "Inc.", "UK": "Ltd", "Germany": "GmbH",
                  "France": "SA", "India": "Pvt Ltd", "Singapore": "Pte Ltd", "Australia": "Pty Ltd"}
SEGMENTS = ("Enterprise", "SMB", "Consumer")
SEGMENT_WEIGHTS = (0.20, 0.45, 0.35)

# (product_name, category, price, stock_quantity) – fixed so questions can name real products.
PRODUCTS: list[tuple[str, str, float, int]] = [
    ("UltraBook Pro 14", "Electronics", 1499.00, 42),
    ("UltraBook Air 13", "Electronics", 1099.00, 65),
    ("4K Monitor 27-inch", "Electronics", 429.00, 120),
    ("Wireless Earbuds X2", "Electronics", 129.00, 310),
    ("Noise-Cancelling Headphones", "Electronics", 299.00, 0),
    ("Smartphone Z10", "Electronics", 899.00, 88),
    ("Tablet Tab S", "Electronics", 549.00, 57),
    ("Smartwatch Fit 3", "Electronics", 249.00, 140),
    ("Office Suite Annual License", "Software", 149.00, 500),
    ("Antivirus Pro (1 Year)", "Software", 59.00, 500),
    ("Cloud Backup 1TB Plan", "Software", 99.00, 500),
    ("Project Planner Pro", "Software", 199.00, 450),
    ("Design Studio License", "Software", 599.00, 300),
    ("Accounting Cloud (Annual)", "Software", 349.00, 400),
    ("VPN Secure (1 Year)", "Software", 79.00, 500),
    ("Developer IDE Pro", "Software", 249.00, 350),
    ("Ergonomic Office Chair", "Furniture", 389.00, 75),
    ("Standing Desk Electric", "Furniture", 649.00, 34),
    ("Conference Table 8-Seat", "Furniture", 1299.00, 6),
    ("Bookshelf Oak 5-Tier", "Furniture", 219.00, 0),
    ("Filing Cabinet 3-Drawer", "Furniture", 179.00, 48),
    ("Lounge Sofa 3-Seat", "Furniture", 999.00, 9),
    ("Desk Lamp LED", "Furniture", 49.00, 210),
    ("Monitor Arm Dual", "Furniture", 129.00, 96),
    ("Premium Printer Paper (5 Reams)", "Office Supplies", 39.99, 480),
    ("Gel Pen Set (12)", "Office Supplies", 12.50, 455),
    ("Sticky Notes Mega Pack", "Office Supplies", 15.99, 390),
    ("Whiteboard 120x90", "Office Supplies", 149.00, 27),
    ("Laser Toner Cartridge", "Office Supplies", 89.99, 160),
    ("Notebook A5 (Pack of 5)", "Office Supplies", 18.75, 0),
    ("Desk Organizer Set", "Office Supplies", 34.50, 230),
    ("Label Maker Pro", "Office Supplies", 69.00, 61),
    ("Wireless Mouse", "Accessories", 39.99, 340),
    ("Mechanical Keyboard", "Accessories", 119.00, 150),
    ("USB-C Hub 7-in-1", "Accessories", 59.99, 275),
    ("Laptop Backpack", "Accessories", 79.00, 190),
    ("Webcam HD 1080p", "Accessories", 89.00, 0),
    ("Portable SSD 1TB", "Accessories", 139.00, 115),
    ("Phone Charger 65W", "Accessories", 45.00, 260),
    ("HDMI Cable 2m", "Accessories", 14.99, 500),
]

# (project_name, department_id, status)
PROJECTS: list[tuple[str, int, str]] = [
    ("Platform Modernization", 1, "Completed"),
    ("Mobile App Redesign", 1, "Active"),
    ("Data Warehouse Migration", 1, "Completed"),
    ("Security Compliance Audit", 1, "Active"),
    ("CRM Integration", 2, "Completed"),
    ("Partner Channel Expansion", 2, "Active"),
    ("Enterprise Upsell Program", 2, "Planned"),
    ("Website Relaunch", 3, "Completed"),
    ("Q4 Brand Campaign", 3, "Active"),
    ("Annual Budget Planning", 4, "Completed"),
    ("Cost Reduction Program", 4, "On Hold"),
    ("Payroll Automation", 5, "Active"),
    ("Talent Acquisition Drive", 5, "Completed"),
    ("Warehouse Optimization", 6, "Active"),
    ("Supply Chain Analytics", 6, "On Hold"),
    ("Help Center Revamp", 7, "Completed"),
    ("Support Chatbot Pilot", 7, "Active"),
    ("AI Research Initiative", 8, "Active"),
    ("Recommendation Engine", 8, "Planned"),
    ("Edge Computing Study", 8, "Planned"),
]
TECH_DEPARTMENTS = {1, 8}
TECH_ROLES = ("Developer", "Tester", "Researcher", "Architect")
BUSINESS_ROLES = ("Analyst", "Coordinator", "Consultant", "Specialist")

REVIEW_CYCLES = (date(2023, 12, 15), date(2024, 12, 13), date(2025, 12, 12), date(2026, 6, 15))
REVIEW_COMMENTS = {
    1: ["Performance well below expectations; improvement plan agreed.",
        "Missed most objectives this period."],
    2: ["Needs improvement in delivery and communication.",
        "Met some objectives but quality was inconsistent."],
    3: ["Solid, reliable performance.", "Meets expectations across all objectives.",
        "Good team player; steady contributor."],
    4: ["Exceeded several key objectives.", "Strong ownership and high-quality work.",
        "Consistently delivers ahead of schedule."],
    5: ["Outstanding impact across the organisation.",
        "Exceptional results; role model for the team."],
}

MONTH_WEIGHTS = (0.80, 0.75, 0.90, 0.90, 0.95, 0.90, 0.85, 0.90, 1.00, 1.10, 1.25, 1.40)
YEAR_WEIGHTS = {2023: 0.85, 2024: 1.00, 2025: 1.15, 2026: 1.30}


@dataclass(frozen=True)
class SampleData:
    """Generated rows per table, in foreign-key-safe insert order."""

    tables: dict[str, list[tuple]]

    @property
    def columns(self) -> dict[str, tuple[str, ...]]:
        return TABLE_COLUMNS

    def row_counts(self) -> dict[str, int]:
        return {name: len(rows) for name, rows in self.tables.items()}


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------
def _random_date(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, (end - start).days))


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _round_to(value: float, step: int) -> float:
    return float(round(value / step) * step)


def _unique_names(rng: random.Random, count: int) -> list[str]:
    combos = [f"{first} {last}" for first in FIRST_NAMES for last in LAST_NAMES]
    return rng.sample(combos, count)


# -----------------------------------------------------------------------------
# table generators
# -----------------------------------------------------------------------------
def _employees(rng: random.Random, names: list[str]) -> list[tuple]:
    rows: list[tuple] = []
    name_iter = iter(names)

    def add(title: str, dept: int, salary: float, joined: date, manager: int | None) -> int:
        emp_id = len(rows) + 1
        name = next(name_iter)
        email = f"{name.lower().replace(' ', '.')}@example.com"
        rows.append((emp_id, name, email, title, dept, salary, joined.isoformat(), manager))
        return emp_id

    ceo = add("Chief Executive Officer", CEO_DEPARTMENT_ID, 245_000.00, date(2015, 3, 2), None)
    heads: dict[int, int] = {}
    for dept_id, profile in DEPARTMENT_PROFILES.items():
        low, high = profile["salary"]
        # Head salaries overlap the top of the staff band, so a few senior
        # specialists out-earn their manager (good self-join questions).
        salary = _round_to(rng.uniform(low + 0.6 * (high - low), high * 1.05), 500)
        joined = _random_date(rng, date(2015, 6, 1), date(2019, 12, 31))
        heads[dept_id] = add(profile["head"], dept_id, salary, joined, ceo)
    for dept_id, profile in DEPARTMENT_PROFILES.items():
        low, high = profile["salary"]
        for index in range(profile["staff"]):
            title = rng.choice(profile["titles"])
            salary = _round_to(rng.uniform(low, high), 500)
            if index == profile["staff"] - 1 and profile["staff"] >= 5:
                # growth hiring: larger departments made one hire in 2026
                joined = _random_date(rng, date(2026, 1, 5), date(2026, 9, 30))
            else:
                joined = _random_date(rng, date(2016, 1, 1), date(2025, 12, 31))
            add(title, dept_id, salary, joined, heads[dept_id])
    return rows


def _customers(rng: random.Random) -> list[tuple]:
    rows: list[tuple] = []
    used: set[str] = set()
    person_names = iter(rng.sample([f"{f} {l}" for f in FIRST_NAMES for l in LAST_NAMES], 200))
    for customer_id in range(1, 151):
        city, country = rng.choice(CITIES)
        segment = rng.choices(SEGMENTS, weights=SEGMENT_WEIGHTS)[0]
        if segment == "Consumer":
            name = next(person_names)
        else:
            while True:
                name = f"{rng.choice(COMPANY_PREFIXES)} {rng.choice(COMPANY_INDUSTRIES)} {COUNTRY_SUFFIX[country]}"
                if name not in used:
                    break
        used.add(name)
        registered = _random_date(rng, date(2022, 1, 1), date(2026, 9, 30))
        rows.append((customer_id, name, city, country, registered.isoformat(), segment))
    return rows


def _sales(rng: random.Random, employees: list[tuple], products: list[tuple],
           customers: list[tuple]) -> list[tuple]:
    sellers = sorted(
        [(date.fromisoformat(e[6]), e[0]) for e in employees if e[4] == SALES_DEPARTMENT_ID]
    )
    buyers = sorted([(date.fromisoformat(c[4]), c[0], c[5]) for c in customers])
    product_weights = [1 / (p[3] ** 0.5) for p in products]

    months: list[tuple[int, int]] = []
    weights: list[float] = []
    for year in range(DATA_START.year, DATA_END.year + 1):
        for month in range(1, 13):
            if date(year, month, 1) > DATA_END:
                continue
            months.append((year, month))
            weights.append(YEAR_WEIGHTS[year] * MONTH_WEIGHTS[month - 1])

    drafts: list[tuple] = []
    for _ in range(NUM_SALES):
        year, month = rng.choices(months, weights=weights)[0]
        last_day = calendar.monthrange(year, month)[1]
        if (year, month) == (DATA_END.year, DATA_END.month):
            last_day = min(last_day, DATA_END.day)
        sale_date = date(year, month, rng.randint(1, last_day))

        eligible_sellers = [emp for joined, emp in sellers if joined <= sale_date]
        seller = rng.choice(eligible_sellers)

        eligible_buyers = [(cid, seg) for reg, cid, seg in buyers if reg <= sale_date]
        if not eligible_buyers:  # before any customer registered: use the earliest
            eligible_buyers = [(buyers[0][1], buyers[0][2])]
        seg_weight = {"Enterprise": 3.0, "SMB": 2.0, "Consumer": 1.0}
        customer_id, segment = rng.choices(
            eligible_buyers, weights=[seg_weight[s] for _, s in eligible_buyers]
        )[0]

        product = rng.choices(products, weights=product_weights)[0]
        if segment == "Consumer":
            quantity = rng.choices((1, 2, 3), weights=(0.7, 0.2, 0.1))[0]
            discount = rng.choice((0.0, 0.0, 0.05))
        elif segment == "SMB":
            quantity = rng.randint(1, 10)
            discount = rng.choice((0.0, 0.05, 0.10))
        else:
            quantity = rng.randint(5, 40)
            discount = rng.choice((0.10, 0.15, 0.20))
        amount = round(product[3] * quantity * (1 - discount), 2)
        drafts.append((sale_date, rng.random(), seller, product[0], customer_id, quantity, amount))

    drafts.sort(key=lambda row: (row[0], row[1]))
    return [
        (sale_id, seller, product_id, customer_id, qty, amount, sale_date.isoformat())
        for sale_id, (sale_date, _, seller, product_id, customer_id, qty, amount) in enumerate(drafts, 1)
    ]


def _projects(rng: random.Random) -> list[tuple]:
    rows: list[tuple] = []
    for project_id, (name, dept_id, status) in enumerate(PROJECTS, 1):
        end: date | None = None
        if status == "Completed":
            start = _random_date(rng, date(2023, 1, 9), date(2025, 6, 30))
            end = min(start + timedelta(days=rng.randint(60, 420)), DATA_END)
        elif status == "Active":
            start = _random_date(rng, date(2025, 1, 6), date(2026, 9, 1))
        elif status == "On Hold":
            start = _random_date(rng, date(2024, 1, 8), date(2025, 9, 30))
        else:  # Planned – starts in the future relative to the data range
            start = _random_date(rng, date(2026, 12, 1), date(2027, 3, 1))
        budget = _round_to(rng.uniform(50_000, 900_000), 1000)
        rows.append((project_id, name, dept_id, start.isoformat(), _iso(end), status, budget))
    return rows


def _assignments(rng: random.Random, employees: list[tuple], projects: list[tuple]) -> list[tuple]:
    rows: list[tuple] = []
    for project in projects:
        project_id, dept_id, start = project[0], project[2], date.fromisoformat(project[3])
        joined_before = [e for e in employees if date.fromisoformat(e[6]) <= start and e[0] != 1]
        home = [e for e in joined_before if e[4] == dept_id] or [e for e in employees if e[4] == dept_id]
        others = [e for e in joined_before if e[4] != dept_id]

        team_size = rng.randint(4, 8)
        lead = rng.choice(home)
        members = [lead]
        pool = [e for e in home if e is not lead]
        rng.shuffle(pool)
        while len(members) < team_size and (pool or others):
            if others and (not pool or rng.random() < 0.25):
                candidate = others.pop(rng.randrange(len(others)))
            else:
                candidate = pool.pop()
            if candidate not in members:
                members.append(candidate)

        roles = TECH_ROLES if dept_id in TECH_DEPARTMENTS else BUSINESS_ROLES
        for index, member in enumerate(members):
            role = "Lead" if index == 0 else rng.choice(roles)
            hours = rng.randrange(20, 401, 10) if index else rng.randrange(200, 501, 10)
            rows.append((member[0], project_id, role, hours))
    rows.sort(key=lambda row: (row[1], row[0]))
    return rows


def _reviews(rng: random.Random, employees: list[tuple]) -> list[tuple]:
    rows: list[tuple] = []
    latent = {e[0]: rng.gauss(3.3, 0.7) for e in employees}
    for cycle in REVIEW_CYCLES:
        for emp in employees:
            emp_id, manager_id, joined = emp[0], emp[7], date.fromisoformat(emp[6])
            if manager_id is None or joined > cycle - timedelta(days=90):
                continue
            rating = int(min(5, max(1, round(latent[emp_id] + rng.gauss(0, 0.5)))))
            comments = None if rng.random() < 0.15 else rng.choice(REVIEW_COMMENTS[rating])
            review_date = cycle + timedelta(days=rng.randint(0, 10))
            rows.append((emp_id, manager_id, review_date.isoformat(), rating, comments))
    rows.sort(key=lambda row: (row[2], row[0]))
    return [(review_id, *row) for review_id, row in enumerate(rows, 1)]


# -----------------------------------------------------------------------------
# public API
# -----------------------------------------------------------------------------
def generate_sample_data(seed: int = DEFAULT_SEED) -> SampleData:
    """Generate the complete, internally consistent demo dataset."""
    rng = random.Random(seed)
    total_staff = 1 + len(DEPARTMENT_PROFILES) + sum(p["staff"] for p in DEPARTMENT_PROFILES.values())

    departments = [tuple(row) for row in DEPARTMENTS]
    employees = _employees(rng, _unique_names(rng, total_staff))
    products = [(pid, name, cat, price, stock) for pid, (name, cat, price, stock) in enumerate(PRODUCTS, 1)]
    customers = _customers(rng)
    sales = _sales(rng, employees, products, customers)
    projects = _projects(rng)
    assignments = _assignments(rng, employees, projects)
    reviews = _reviews(rng, employees)

    return SampleData(tables={
        "departments": departments,
        "employees": employees,
        "products": products,
        "customers": customers,
        "sales": sales,
        "projects": projects,
        "employee_projects": assignments,
        "performance_reviews": reviews,
    })
