"""
Few-shot example pool.

Every example is written for a separate, neutral **bookstore** schema, not
the demo business database. This is deliberate:

* the examples teach SQL *patterns* and the *response style*, not answers;
* they still work when users connect their own database;
* they cannot leak answers into the evaluation (a test checks that no example
  references a demo-database table).

The pool covers the required patterns (SELECT/WHERE, ORDER BY/LIMIT,
GROUP BY/HAVING, joins, aggregates, date filtering, nested queries) plus
CTEs/window functions, self-joins, ratios, CASE bucketing, follow-ups, and
three behaviour demonstrations: clarification, unsafe-request refusal and
missing data. ``tags`` are what ``ExampleSelector`` matches against;
``trigger_tags`` restrict behaviour examples to questions that show the
trigger.

All SQL is SQLite. ``BOOKSTORE_DDL`` is real DDL: the test suite creates
this database and executes every example query against it. For PostgreSQL
and MySQL the SQL is transpiled with SQLGlot. Where SQLGlot cannot translate
a construct (SQLite date modifiers such as ``date(d, '-30 days')``), the
example carries hand-written ``dialect_sql`` overrides, and a test ensures no
SQLite-only syntax reaches a non-SQLite prompt.
"""

from __future__ import annotations

from backend.models import FewShotExample

EXAMPLE_REFERENCE_DATE = "2025-06-30"

BOOKSTORE_DDL = """\
CREATE TABLE authors (
  author_id INTEGER PRIMARY KEY,
  author_name TEXT NOT NULL,
  country TEXT,
  birth_year INTEGER
);
CREATE TABLE books (
  book_id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  author_id INTEGER NOT NULL REFERENCES authors(author_id),
  genre TEXT NOT NULL,            -- e.g. 'Fantasy', 'Science Fiction', 'History'
  price DECIMAL(8, 2) NOT NULL,
  published_date DATE NOT NULL,
  page_count INTEGER
);
CREATE TABLE members (
  member_id INTEGER PRIMARY KEY,
  member_name TEXT NOT NULL,
  city TEXT NOT NULL,
  join_date DATE NOT NULL,
  tier TEXT NOT NULL              -- 'Basic', 'Gold' or 'Platinum'
);
CREATE TABLE orders (
  order_id INTEGER PRIMARY KEY,
  member_id INTEGER NOT NULL REFERENCES members(member_id),
  order_date DATE NOT NULL,
  status TEXT NOT NULL            -- 'Shipped', 'Pending' or 'Cancelled'
);
CREATE TABLE order_items (
  order_id INTEGER NOT NULL REFERENCES orders(order_id),
  book_id INTEGER NOT NULL REFERENCES books(book_id),
  quantity INTEGER NOT NULL,
  unit_price DECIMAL(8, 2) NOT NULL,
  PRIMARY KEY (order_id, book_id)
);
CREATE TABLE staff (
  staff_id INTEGER PRIMARY KEY,
  staff_name TEXT NOT NULL,
  supervisor_id INTEGER REFERENCES staff(staff_id),
  hourly_rate DECIMAL(6, 2) NOT NULL,
  hire_date DATE NOT NULL
);"""

BOOKSTORE_TABLES = frozenset({"authors", "books", "members", "orders", "order_items", "staff"})


def _ex(id: str, question: str, sql: str | None, explanation: str, tags: set[str], **extra) -> FewShotExample:
    return FewShotExample(id=id, question=question, sql=sql, explanation=explanation,
                          tags=frozenset(tags), **extra)


FEW_SHOT_EXAMPLES: tuple[FewShotExample, ...] = (
    _ex("E01", "List all Fantasy books that cost less than $15, cheapest first.",
        "SELECT b.title, b.price FROM books AS b WHERE b.genre = 'Fantasy' AND b.price < 15 "
        "ORDER BY b.price ASC",
        "Keeps only Fantasy books priced under $15 and sorts them from cheapest to most expensive.",
        {"filter", "order"}),
    _ex("E02", "What are the 5 most expensive books?",
        "SELECT b.title, b.price FROM books AS b ORDER BY b.price DESC LIMIT 5",
        "Sorts all books by price from highest to lowest and keeps the first five.",
        {"order_limit", "superlative", "order"}),
    _ex("E03", "How many books has each author written, and what is their average price?",
        "SELECT a.author_name, COUNT(b.book_id) AS book_count, ROUND(AVG(b.price), 2) AS average_price "
        "FROM authors AS a JOIN books AS b ON b.author_id = a.author_id "
        "GROUP BY a.author_id, a.author_name ORDER BY book_count DESC",
        "Joins books to their authors, groups the books per author, then counts them and averages "
        "their prices.",
        {"aggregate", "group_by", "join"}),
    _ex("E04", "Which genres have more than 20 books?",
        "SELECT b.genre, COUNT(*) AS book_count FROM books AS b GROUP BY b.genre "
        "HAVING COUNT(*) > 20 ORDER BY book_count DESC",
        "Counts books per genre and keeps only genres whose count exceeds 20. HAVING is used because "
        "the condition applies to the count, which only exists after grouping.",
        {"aggregate", "group_by", "having", "comparison"}),
    _ex("E05", "Which members have ordered the book 'Dune'?",
        "SELECT DISTINCT m.member_name, m.city FROM members AS m "
        "JOIN orders AS o ON o.member_id = m.member_id "
        "JOIN order_items AS oi ON oi.order_id = o.order_id "
        "JOIN books AS b ON b.book_id = oi.book_id "
        "WHERE b.title = 'Dune' ORDER BY m.member_name",
        "Follows the chain members -> orders -> order items -> books to find who bought 'Dune'. "
        "DISTINCT lists each member once even if they ordered it several times.",
        {"join", "multi_join", "filter", "distinct"}),
    _ex("E06", "What is the total revenue per genre from shipped orders?",
        "SELECT b.genre, ROUND(SUM(oi.quantity * oi.unit_price), 2) AS total_revenue "
        "FROM order_items AS oi JOIN orders AS o ON o.order_id = oi.order_id "
        "JOIN books AS b ON b.book_id = oi.book_id "
        "WHERE o.status = 'Shipped' GROUP BY b.genre ORDER BY total_revenue DESC",
        "Adds up quantity x unit price of every item in shipped orders, grouped by the genre of the book.",
        {"join", "multi_join", "aggregate", "group_by", "filter"},
        assumptions=("Revenue = quantity x unit_price of order items.",)),
    _ex("E07", "How many orders were placed in the last 30 days?",
        "SELECT COUNT(*) AS order_count FROM orders AS o "
        "WHERE o.order_date > date('2025-06-30', '-30 days') AND o.order_date <= '2025-06-30'",
        "Counts orders dated within the 30 days up to the reference date.",
        {"date", "relative_date", "aggregate", "filter"},
        assumptions=("'Last 30 days' = the 30 days ending on the reference date 2025-06-30.",),
        reference_date=EXAMPLE_REFERENCE_DATE,
        # SQLGlot cannot translate SQLite's date(d, '-30 days') modifier syntax.
        dialect_sql=(
            ("postgresql", "SELECT COUNT(*) AS order_count FROM orders AS o "
                           "WHERE o.order_date > DATE '2025-06-30' - INTERVAL '30 days' "
                           "AND o.order_date <= DATE '2025-06-30'"),
            ("mysql", "SELECT COUNT(*) AS order_count FROM orders AS o "
                      "WHERE o.order_date > DATE_SUB('2025-06-30', INTERVAL 30 DAY) "
                      "AND o.order_date <= '2025-06-30'"),
        )),
    _ex("E08", "Show the number of orders per month in 2024.",
        "SELECT strftime('%Y-%m', o.order_date) AS order_month, COUNT(*) AS order_count "
        "FROM orders AS o WHERE o.order_date >= '2024-01-01' AND o.order_date < '2025-01-01' "
        "GROUP BY order_month ORDER BY order_month",
        "Keeps orders from 2024, groups them by year-month and counts each month.",
        {"date", "date_grouping", "aggregate", "group_by"}),
    _ex("E09", "Which books are priced above the average book price?",
        "SELECT b.title, b.price FROM books AS b "
        "WHERE b.price > (SELECT AVG(price) FROM books) ORDER BY b.price DESC",
        "A subquery computes the average price once; the outer query keeps books priced above it.",
        {"nested", "comparison_to_aggregate", "comparison", "filter"}),
    _ex("E10", "Which members have never placed an order?",
        "SELECT m.member_name, m.join_date FROM members AS m "
        "WHERE NOT EXISTS (SELECT 1 FROM orders AS o WHERE o.member_id = m.member_id) "
        "ORDER BY m.member_name",
        "Keeps members for whom no matching order exists (an anti-join with NOT EXISTS).",
        {"nested", "anti_join"}),
    _ex("E11", "What is the best-selling book in each genre by units sold?",
        "WITH units AS (SELECT b.genre, b.title, SUM(oi.quantity) AS units_sold FROM books AS b "
        "JOIN order_items AS oi ON oi.book_id = b.book_id GROUP BY b.book_id, b.genre, b.title), "
        "ranked AS (SELECT genre, title, units_sold, "
        "RANK() OVER (PARTITION BY genre ORDER BY units_sold DESC) AS genre_rank FROM units) "
        "SELECT genre, title, units_sold FROM ranked WHERE genre_rank = 1 ORDER BY genre",
        "First totals units sold per book, then ranks books within each genre and keeps the rank-1 "
        "book(s); ties are all shown.",
        {"cte", "window", "top_per_group", "aggregate", "join", "superlative"}),
    _ex("E12", "Which staff members earn a higher hourly rate than their supervisor?",
        "SELECT s.staff_name, s.hourly_rate, sup.staff_name AS supervisor_name, "
        "sup.hourly_rate AS supervisor_rate FROM staff AS s "
        "JOIN staff AS sup ON sup.staff_id = s.supervisor_id WHERE s.hourly_rate > sup.hourly_rate",
        "Joins the staff table to itself (once as the employee, once as the supervisor) and compares "
        "their hourly rates.",
        {"self_join", "join", "comparison", "filter"}),
    _ex("E13", "What percentage of orders were cancelled?",
        "SELECT ROUND(100.0 * SUM(CASE WHEN o.status = 'Cancelled' THEN 1 ELSE 0 END) / COUNT(*), 2) "
        "AS cancelled_percentage FROM orders AS o",
        "Counts cancelled orders, divides by all orders and multiplies by 100 (100.0 avoids integer "
        "division).",
        {"ratio", "aggregate", "conditional_aggregation"}),
    _ex("E14", "Compare the number of books published in 2023 and 2024 for each genre.",
        "SELECT b.genre, "
        "SUM(CASE WHEN strftime('%Y', b.published_date) = '2023' THEN 1 ELSE 0 END) AS books_2023, "
        "SUM(CASE WHEN strftime('%Y', b.published_date) = '2024' THEN 1 ELSE 0 END) AS books_2024 "
        "FROM books AS b GROUP BY b.genre ORDER BY b.genre",
        "Counts each genre's books twice, once per year, side by side using conditional sums.",
        {"date", "comparison_over_time", "conditional_aggregation", "group_by", "aggregate"}),
    _ex("E15", "Count books by price range: under $10, $10 to $25, and over $25.",
        "SELECT CASE WHEN b.price < 10 THEN 'Under $10' WHEN b.price <= 25 THEN '$10 to $25' "
        "ELSE 'Over $25' END AS price_range, COUNT(*) AS book_count FROM books AS b "
        "GROUP BY price_range ORDER BY MIN(b.price)",
        "Assigns every book to a price band with CASE, then counts the books in each band.",
        {"case", "bucketing", "aggregate", "group_by"}),
    _ex("E16", "Now only show the ones under $20.",
        "SELECT b.title, b.published_date, b.price FROM books AS b "
        "WHERE b.published_date > '2020-12-31' AND b.price < 20 ORDER BY b.published_date",
        "Keeps the previous query (books published after 2020) and adds the new price filter.",
        {"follow_up", "filter"},
        intent="follow_up_modification",
        trigger_tags=frozenset({"follow_up"}),
        previous_question="List books published after 2020.",
        previous_sql="SELECT b.title, b.published_date, b.price FROM books AS b "
                     "WHERE b.published_date > '2020-12-31' ORDER BY b.published_date"),
    _ex("E17", "Who are our best members?",
        None,
        "'Best' could be measured in several ways in this schema, and each gives a different answer.",
        {"ambiguous"},
        intent="clarification_needed",
        trigger_tags=frozenset({"ambiguous"}),
        clarification_question="How should 'best' be measured?",
        clarification_options=("Highest total spending", "Most orders placed", "Highest membership tier")),
    _ex("E18", "Delete all cancelled orders.",
        None,
        "This would change data. QueryBridge only runs read-only queries, so no SQL was generated. "
        "I can list the cancelled orders instead.",
        {"unsafe"},
        intent="unsafe_request",
        trigger_tags=frozenset({"unsafe"})),
    _ex("E19", "What is each member's phone number?",
        None,
        "The members table has no phone number column (available: member_name, city, join_date, tier), "
        "so this cannot be answered from this database.",
        {"missing_data"},
        intent="out_of_scope",
        trigger_tags=frozenset({"missing_data"})),
    _ex("E20", "Find books with 'war' in the title.",
        "SELECT b.title, b.genre FROM books AS b WHERE LOWER(b.title) LIKE '%war%' ORDER BY b.title",
        "Matches titles containing 'war' anywhere, ignoring upper/lower case.",
        {"text_search", "filter"}),
    _ex("E21", "Which authors published a book between March and May 2024?",
        "SELECT DISTINCT a.author_name FROM authors AS a JOIN books AS b ON b.author_id = a.author_id "
        "WHERE b.published_date BETWEEN '2024-03-01' AND '2024-05-31' ORDER BY a.author_name",
        "Joins authors to their books and keeps those with a publication date from 1 March to 31 May 2024.",
        {"date", "date_range", "join", "distinct", "filter"}),
    _ex("E22", "Which staff members have no supervisor?",
        "SELECT s.staff_name, s.hire_date FROM staff AS s WHERE s.supervisor_id IS NULL",
        "A missing supervisor is stored as NULL, so the query uses IS NULL (= NULL never matches).",
        {"null", "filter"}),
)

EXAMPLES_BY_ID: dict[str, FewShotExample] = {e.id: e for e in FEW_SHOT_EXAMPLES}

# Used to fill remaining slots (broad, high-value patterns) and by the "static" strategy.
CORE_FILL_ORDER: tuple[str, ...] = ("E03", "E02", "E09", "E08", "E05", "E04")
