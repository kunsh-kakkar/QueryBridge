// Browser walkthrough: drives the interface like a first-time user and fails on console errors,
// CSP violations, failed requests or horizontal overflow on a phone.
//   1. python -m tests.browser.demo_server 8765      (scripted model, temporary data folder)
//   2. node tests/browser/walkthrough.js              (needs Node.js and the playwright package)
// Screenshots go to QB_OUT (default: tests/browser/screenshots).
const { chromium } = require("playwright");

const BASE = process.env.QB_URL || "http://127.0.0.1:8765";
const OUT = process.env.QB_OUT || __dirname + "/screenshots";
const SCALE = Number(process.env.QB_SCALE || 1);

(async () => {
  const fs = require("fs");
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const problems = [];
  async function newPage(opts = {}) {
    const context = await browser.newContext({ viewport: { width: 1360, height: 900 }, deviceScaleFactor: SCALE, ...opts });
    const page = await context.newPage();
    page.on("console", (m) => { if (m.type() === "error" || m.type() === "warning") problems.push(`[console ${m.type()}] ${m.text()}`); });
    page.on("pageerror", (e) => problems.push(`[pageerror] ${e.message}`));
    page.on("requestfailed", (r) => {
      const error = r.failure() && r.failure().errorText;
      // Chromium's devtools sometimes report a fully read streamed response as ERR_ABORTED; the answer
      // itself is checked on the page, so only other failures count.
      if (r.url().endsWith("/api/ask") && error === "net::ERR_ABORTED") return;
      problems.push(`[requestfailed] ${r.url()} ${error}`);
    });
    return page;
  }
  const shot = (page, name, full = false) => page.screenshot({ path: `${OUT}/${name}.png`, fullPage: full });

  const page = await newPage();
  await page.goto(BASE + "/");
  await page.waitForSelector("#welcome-dialog[open]");
  await page.waitForTimeout(400);
  await shot(page, "01-welcome");
  await page.click("#welcome-dialog button[value=ask]");
  await page.waitForSelector(".ask-box textarea");
  await shot(page, "02-ask-home", true);

  // ask a question by clicking an example
  await page.click("text=Show me the top 5 employees with the highest salaries.");
  await page.waitForSelector(".answer", { timeout: 30000 });
  await page.waitForTimeout(400);
  await shot(page, "03-answer-table", true);

  // chart tab on a category question
  await page.fill(".ask-box textarea", "What is the total sales amount by product category?");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelectorAll(".answer").length >= 1 && !document.querySelector(".progress-card"), null, { timeout: 30000 });
  await page.waitForTimeout(300);
  await page.click(".answer >> nth=0 >> role=tab[name='Chart']");
  await page.waitForSelector(".answer .chart-box .main-svg", { timeout: 30000 });
  await page.waitForTimeout(600);
  await shot(page, "04-answer-chart");

  await page.click(".answer >> nth=0 >> role=tab[name='How I got this']");
  await shot(page, "05-how");
  await page.click(".answer >> nth=0 >> role=tab[name='SQL']");
  await shot(page, "06-sql");

  // clarification
  await page.fill(".ask-box textarea", "Show me the highest-performing employees");
  await page.keyboard.press("Enter");
  await page.waitForSelector(".clarify .chip", { timeout: 30000 });
  await shot(page, "08-clarify");
  await page.click(".clarify .chip >> nth=0");
  await page.waitForFunction(() => !document.querySelector(".progress-card") && document.querySelector(".answer .pill.tone-good"), null, { timeout: 30000 });
  await page.waitForTimeout(300);
  await shot(page, "09-clarified");

  // kpi
  await page.fill(".ask-box textarea", "How many employees are there?");
  await page.keyboard.press("Enter");
  await page.waitForSelector(".answer .kpi", { timeout: 30000 });
  await shot(page, "10-kpi");

  // refusal
  await page.fill(".ask-box textarea", "Delete all sales from 2023");
  await page.keyboard.press("Enter");
  await page.waitForSelector(".answer .tone-warn", { timeout: 30000 });
  await shot(page, "11-refused");

  // data page
  await page.click("nav a[data-route=data]");
  await page.waitForSelector(".table-list .table-item");
  await page.click(".table-item[data-name=employees]");
  await page.waitForTimeout(200);
  await shot(page, "12-data", true);
  await page.click("text=Show 10 example rows");
  await page.waitForSelector("text=Example rows");
  await shot(page, "13-data-preview", true);

  // history
  await page.click("nav a[data-route=history]");
  await page.waitForSelector(".history-item");
  await page.click(".history-summary >> nth=0");
  await page.waitForTimeout(200);
  await shot(page, "14-history", true);

  // explain
  await page.click("nav a[data-route=explain]");
  await page.waitForSelector("textarea.mono");
  await page.click("button:has-text('Explain it')");
  await page.waitForSelector("text=What this query does", { timeout: 20000 });
  await shot(page, "15-explain", true);
  await page.fill("textarea.mono", "DROP TABLE employees");
  await page.click("button:has-text('Run it')");
  await page.waitForSelector(".answer", { timeout: 20000 });
  await shot(page, "16-explain-blocked");

  // settings
  for (const section of ["database", "answers", "lab", "evaluation", "about"]) {
    await page.goto(`${BASE}/#/settings/${section}`);
    await page.waitForTimeout(section === "lab" ? 1200 : 500);
    await shot(page, `17-settings-${section}`, true);
  }

  // dark theme
  await page.goto(`${BASE}/#/ask`);
  await page.click("#theme-button"); // auto -> light
  await page.click("#theme-button"); // light -> dark
  await page.waitForTimeout(300);
  await shot(page, "18-dark-ask", true);

  // mobile
  const phone = await newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  await phone.goto(BASE + "/");
  await phone.waitForSelector("#welcome-dialog[open]");
  await phone.waitForTimeout(400);
  await shot(phone, "19-phone-welcome");
  await phone.click("#welcome-dialog button[value=ask]");
  await phone.waitForTimeout(300);
  await shot(phone, "20-phone-ask", true);
  await phone.click(".menu-toggle");
  await shot(phone, "21-phone-menu");
  const overflow = await phone.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  if (overflow > 0) problems.push(`[layout] phone horizontal overflow ${overflow}px`);

  await browser.close();
  console.log(problems.length ? problems.join("\n") : "NO PROBLEMS");
  if (problems.length) process.exitCode = 1;
})().catch((e) => { console.error("FAILED", e); process.exit(1); });
