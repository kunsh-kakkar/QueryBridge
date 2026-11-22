-- =============================================================================
-- QueryBridge demonstration database (SQLite)
-- A small company: departments, employees (with a manager hierarchy), products,
-- customers, sales transactions, projects with staffing, and performance reviews.
-- Dates are ISO-8601 text (YYYY-MM-DD); money is USD with 2 decimals.
-- =============================================================================

PRAGMA foreign_keys = ON;

CREATE TABLE departments (
    department_id    INTEGER        PRIMARY KEY,
    department_name  TEXT           NOT NULL UNIQUE,
    location         TEXT           NOT NULL,
    budget           DECIMAL(12, 2) NOT NULL CHECK (budget >= 0)
);

CREATE TABLE employees (
    employee_id      INTEGER        PRIMARY KEY,
    employee_name    TEXT           NOT NULL,
    email            TEXT           NOT NULL UNIQUE,
    job_title        TEXT           NOT NULL,
    department_id    INTEGER        NOT NULL,
    salary           DECIMAL(12, 2) NOT NULL CHECK (salary > 0),
    joining_date     DATE           NOT NULL,
    manager_id       INTEGER,
    FOREIGN KEY (department_id) REFERENCES departments (department_id),
    FOREIGN KEY (manager_id)    REFERENCES employees (employee_id)
);

CREATE TABLE products (
    product_id       INTEGER        PRIMARY KEY,
    product_name     TEXT           NOT NULL UNIQUE,
    category         TEXT           NOT NULL,
    price            DECIMAL(10, 2) NOT NULL CHECK (price > 0),
    stock_quantity   INTEGER        NOT NULL CHECK (stock_quantity >= 0)
);

CREATE TABLE customers (
    customer_id        INTEGER      PRIMARY KEY,
    customer_name      TEXT         NOT NULL,
    city               TEXT         NOT NULL,
    country            TEXT         NOT NULL,
    registration_date  DATE         NOT NULL,
    segment            TEXT         NOT NULL CHECK (segment IN ('Enterprise', 'SMB', 'Consumer'))
);

CREATE TABLE sales (
    sale_id          INTEGER        PRIMARY KEY,
    employee_id      INTEGER        NOT NULL,
    product_id       INTEGER        NOT NULL,
    customer_id      INTEGER        NOT NULL,
    quantity         INTEGER        NOT NULL CHECK (quantity > 0),
    sale_amount      DECIMAL(12, 2) NOT NULL CHECK (sale_amount > 0),
    sale_date        DATE           NOT NULL,
    FOREIGN KEY (employee_id) REFERENCES employees (employee_id),
    FOREIGN KEY (product_id)  REFERENCES products (product_id),
    FOREIGN KEY (customer_id) REFERENCES customers (customer_id)
);

CREATE TABLE projects (
    project_id       INTEGER        PRIMARY KEY,
    project_name     TEXT           NOT NULL UNIQUE,
    department_id    INTEGER        NOT NULL,
    start_date       DATE           NOT NULL,
    end_date         DATE,
    status           TEXT           NOT NULL CHECK (status IN ('Planned', 'Active', 'Completed', 'On Hold')),
    budget           DECIMAL(12, 2) NOT NULL CHECK (budget >= 0),
    FOREIGN KEY (department_id) REFERENCES departments (department_id)
);

CREATE TABLE employee_projects (
    employee_id      INTEGER        NOT NULL,
    project_id       INTEGER        NOT NULL,
    role             TEXT           NOT NULL,
    hours_allocated  INTEGER        NOT NULL CHECK (hours_allocated > 0),
    PRIMARY KEY (employee_id, project_id),
    FOREIGN KEY (employee_id) REFERENCES employees (employee_id),
    FOREIGN KEY (project_id)  REFERENCES projects (project_id)
);

CREATE TABLE performance_reviews (
    review_id        INTEGER        PRIMARY KEY,
    employee_id      INTEGER        NOT NULL,
    reviewer_id      INTEGER        NOT NULL,
    review_date      DATE           NOT NULL,
    rating           INTEGER        NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comments         TEXT,
    FOREIGN KEY (employee_id) REFERENCES employees (employee_id),
    FOREIGN KEY (reviewer_id) REFERENCES employees (employee_id)
);

-- Indexes for the most common join and filter columns.
CREATE INDEX idx_employees_department   ON employees (department_id);
CREATE INDEX idx_employees_manager      ON employees (manager_id);
CREATE INDEX idx_sales_employee         ON sales (employee_id);
CREATE INDEX idx_sales_product          ON sales (product_id);
CREATE INDEX idx_sales_customer         ON sales (customer_id);
CREATE INDEX idx_sales_date             ON sales (sale_date);
CREATE INDEX idx_projects_department    ON projects (department_id);
CREATE INDEX idx_employee_projects_proj ON employee_projects (project_id);
CREATE INDEX idx_reviews_employee       ON performance_reviews (employee_id);
