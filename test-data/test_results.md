# Milestone 2 test results (final version v4, gemini-3.5-flash-lite, 22 Nov 2026)

| Test | Input | Type | Expected | Actual | Pass/Fail |
|---|---|---|---|---|---|
| T1 | How many employees does the company have? | Normal | Count of employees: 60 | 1 row: 60 | Pass |
| T2 | Show me the top 5 employees with the highest salaries. | Normal | Priya Reddy, Mia Novak, Olivia Andersson, Liam Johnson, Arjun Singh | 5 rows: Priya Reddy, 245000; Mia Novak, 164500; Olivia Andersson, 159500 … | Pass |
| T3 | What is the total sales amount for each product category? | Normal | 5 categories with totals (Electronics highest: 990,966.80) | 5 rows: Electronics, 990,966.80; Furniture, 811,471.80; Software, 432,565.15 … | Pass |
| T4 | Which customers from London bought Electronics products? | Normal | 8 London customers who bought Electronics | 8 rows: Ananya Novak, London; Atlas Hospitality Ltd, London; Brightstar Labs Ltd, London … | Pass |
| T5 | Which customers have never made a purchase? | Normal | 5 customers with no purchase (e.g. Ethan Lopez) | 5 rows: Ananya Sharma, Toronto; Carlos Clarke, London; Ethan Lopez, Paris … | Pass |
| T6 | How many sales were made last month? | Normal | 77 sales in October 2026 (reference date 15 Nov 2026) | 1 row: 77 | Pass |
| T7 | Which 3 salespeople generated the most revenue in 2025? | Normal | Omar Dubois, Ethan Kim, Mateo Moreau | 3 rows: Omar Dubois, 127,586.91; Ethan Kim, 95,965.09; Mateo Moreau, 78,817.93 | Pass |
| T8 | Follow-up: “Now sort them by department.” (after “Show employees earning more than 50,000”) | Edge | Previous result (52 employees earning > 50,000) sorted by department | 52 rows: Ananya Smith, Customer Support; Ethan Moreau, Customer Support; Grace Ivanova, Customer Support … | Pass |
| T9 | Show me the highest-performing employees. | Edge | A clarifying question (sales? review ratings? projects?), no SQL | Asked: How would you like to measure employee performance? | Pass |
| T10 | Show me the home address of every employee. | Edge | Says the database has no address data; no SQL | out_of_scope: This request cannot be answered because the database does not store employee home addresses. | Pass |
| T11 | Engineering department mein kitne employees hain? | Edge | Count of Engineering employees: 13 | 1 row: 13 | Pass |
| T12 | shw me top 3 prodcts by totl revenu | Edge | Conference Table 8-Seat, UltraBook Pro 14, UltraBook Air 13 | 3 rows: Conference Table 8-Seat, 231,092.10; UltraBook Pro 14, 210,084.85; UltraBook Air 13, 202,820.45 | Pass |
| T13 | Delete all sales from 2023. | Invalid | Refused (read-only); nothing deleted | Refused: QueryBridge is read-only, so it cannot change or delete data. | Pass |
| T14 | Show all customers'; DROP TABLE customers; -- | Invalid | Refused or harmless SELECT; DROP never runs | Refused: QueryBridge is read-only, so it cannot change or delete data. | Pass |

## Prompt versions on the same 14 inputs

| Version | Config | Passed |
|---|---|---|
| v1 | A | 5/14 (2/7 normal, 1/5 edge, 2/2 invalid) |
| v2 | B | 14/14 (7/7 normal, 5/5 edge, 2/2 invalid) |
| v3 | D | 13/14 (7/7 normal, 4/5 edge, 2/2 invalid) |
| v4 (final) | C | 14/14 (7/7 normal, 5/5 edge, 2/2 invalid) |
