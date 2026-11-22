# Example value of `{database_schema}` (demo database)

```sql
About this database: A mid-sized company that sells electronics, software, furniture, office supplies and accessories to business and consumer customers. Data covers January 2023 to October 2026. Money is in USD; dates are ISO strings (YYYY-MM-DD).

-- customers: 150 rows. Customers (companies or individual consumers) who buy from the company.
CREATE TABLE customers (
  customer_id INTEGER PRIMARY KEY,  -- Unique identifier of the customer
  customer_name TEXT NOT NULL,  -- Company name for Enterprise/SMB customers, person name for Consumers
  city TEXT NOT NULL,  -- City where the customer is located; Example values: <data>Austin | Bengaluru | Berlin | Chicago | London | Manchester | Mumbai | Munich | New York | Paris</data>
  country TEXT NOT NULL,  -- Country where the customer is located; Values: <data>Australia | Canada | France | Germany | India | Singapore | UK | USA</data>
  registration_date DATE NOT NULL,  -- Date the customer account was created; Range: 2022-01-21 to 2026-09-17
  segment TEXT NOT NULL  -- Customer segment: Enterprise, SMB (small/medium business) or Consumer; Values: <data>Consumer | Enterprise | SMB</data>
);

-- departments: 8 rows. Organisational departments of the company and where they are based.
CREATE TABLE departments (
  department_id INTEGER PRIMARY KEY,  -- Unique identifier of the department
  department_name TEXT NOT NULL,  -- Name of the department, e.g. Engineering or Sales; Values: <data>Customer Support | Engineering | Finance | Human Resources | Marketing | Operations | Research & Development | Sales</data>
  location TEXT NOT NULL,  -- City where the department is headquartered; Values: <data>Bengaluru | Berlin | Chicago | London | New York | San Francisco | Toronto</data>
  budget DECIMAL(12, 2) NOT NULL  -- Annual operating budget of the department in USD; Range: 650000 to 4800000
);

-- employee_projects: 105 rows. Assignment of employees to projects (many-to-many), with each person's role and allocated hours.
CREATE TABLE employee_projects (
  employee_id INTEGER NOT NULL REFERENCES employees(employee_id),  -- Assigned employee (references employees)
  project_id INTEGER NOT NULL REFERENCES projects(project_id),  -- Project the employee is assigned to (references projects)
  role TEXT NOT NULL,  -- Role on the project, e.g. Lead, Developer, Analyst; Values: <data>Analyst | Architect | Consultant | Coordinator | Developer | Lead | Researcher | Specialist | Tester</data>
  hours_allocated INTEGER NOT NULL,  -- Total hours allocated to this employee on the project; Range: 20 to 500
  PRIMARY KEY (employee_id, project_id)
);

-- employees: 60 rows. Everyone employed by the company, including the CEO and department heads.
CREATE TABLE employees (
  employee_id INTEGER PRIMARY KEY,  -- Unique identifier of the employee
  employee_name TEXT NOT NULL,  -- Full name of the employee
  email TEXT NOT NULL,  -- Work email address of the employee
  job_title TEXT NOT NULL,  -- Current job title, e.g. Senior Software Engineer
  department_id INTEGER NOT NULL REFERENCES departments(department_id),  -- Department the employee belongs to (references departments)
  salary DECIMAL(12, 2) NOT NULL,  -- Annual gross salary in USD; Range: 34000 to 245000
  joining_date DATE NOT NULL,  -- Date the employee joined the company (hire date); Range: 2015-03-02 to 2026-09-12
  manager_id INTEGER REFERENCES employees(employee_id)  -- The employee's direct manager (references employees.employee_id); NULL only for the CEO
);

-- performance_reviews: 209 rows. Periodic performance reviews of employees, written by their manager.
CREATE TABLE performance_reviews (
  review_id INTEGER PRIMARY KEY,  -- Unique identifier of the review
  employee_id INTEGER NOT NULL REFERENCES employees(employee_id),  -- Employee being reviewed (references employees)
  reviewer_id INTEGER NOT NULL REFERENCES employees(employee_id),  -- Employee who wrote the review, normally the manager (references employees)
  review_date DATE NOT NULL,  -- Date of the review; Range: 2023-12-15 to 2026-06-25
  rating INTEGER NOT NULL,  -- Overall rating from 1 (poor) to 5 (outstanding); Range: 1 to 5
  comments TEXT  -- Free-text reviewer comments; may be NULL
);

-- products: 40 rows. Catalogue of products the company sells.
CREATE TABLE products (
  product_id INTEGER PRIMARY KEY,  -- Unique identifier of the product
  product_name TEXT NOT NULL,  -- Name of the product
  category TEXT NOT NULL,  -- Product category: Electronics, Software, Furniture, Office Supplies or Accessories; Values: <data>Accessories | Electronics | Furniture | Office Supplies | Software</data>
  price DECIMAL(10, 2) NOT NULL,  -- Current list price per unit in USD; Range: 12.5 to 1499
  stock_quantity INTEGER NOT NULL  -- Units currently in stock (0 means out of stock); Range: 0 to 500
);

-- projects: 20 rows. Internal projects owned by a department.
CREATE TABLE projects (
  project_id INTEGER PRIMARY KEY,  -- Unique identifier of the project
  project_name TEXT NOT NULL,  -- Name of the project; Example values: <data>AI Research Initiative | Annual Budget Planning | CRM Integration | Cost Reduction Program | Data Warehouse Migration | Edge Computing Study | Enterprise Upsell Program | Help Center Revamp | Mobile App Redesign | Partner Channel Expansion</data>
  department_id INTEGER NOT NULL REFERENCES departments(department_id),  -- Department that owns the project (references departments)
  start_date DATE NOT NULL,  -- Date the project started (or is planned to start); Range: 2023-03-15 to 2027-02-25
  end_date DATE,  -- Date the project finished; NULL if not finished; Range: 2023-07-28 to 2025-10-01
  status TEXT NOT NULL,  -- Project status: Planned, Active, Completed or On Hold; Values: <data>Active | Completed | On Hold | Planned</data>
  budget DECIMAL(12, 2) NOT NULL  -- Approved project budget in USD; Range: 82000 to 814000
);

-- sales: 2,500 rows. One row per sales transaction: which employee sold which product to which customer, and when.
CREATE TABLE sales (
  sale_id INTEGER PRIMARY KEY,  -- Unique identifier of the sale
  employee_id INTEGER NOT NULL REFERENCES employees(employee_id),  -- Employee who made the sale (references employees)
  product_id INTEGER NOT NULL REFERENCES products(product_id),  -- Product that was sold (references products)
  customer_id INTEGER NOT NULL REFERENCES customers(customer_id),  -- Customer who bought (references customers)
  quantity INTEGER NOT NULL,  -- Number of units sold in this transaction; Range: 1 to 40
  sale_amount DECIMAL(12, 2) NOT NULL,  -- Total value of the transaction in USD after any discount (this is the revenue figure); Range: 11.25 to 40918.5
  sale_date DATE NOT NULL  -- Date of the sale; Range: 2023-01-01 to 2026-10-31
);

Relationships (use these join conditions):
- employee_projects.employee_id = employees.employee_id  (many employee_projects rows to one employees row)
- employee_projects.project_id = projects.project_id  (many employee_projects rows to one projects row)
- employees.department_id = departments.department_id  (many employees rows to one departments row)
- employees.manager_id = employees.employee_id  (self-reference: join employees to itself using two aliases)
- performance_reviews.employee_id = employees.employee_id  (many performance_reviews rows to one employees row)
- performance_reviews.reviewer_id = employees.employee_id  (many performance_reviews rows to one employees row)
- projects.department_id = departments.department_id  (many projects rows to one departments row)
- sales.employee_id = employees.employee_id  (many sales rows to one employees row)
- sales.product_id = products.product_id  (many sales rows to one products row)
- sales.customer_id = customers.customer_id  (many sales rows to one customers row)
- employee_projects is a junction table linking employees and projects (many-to-many).
```
