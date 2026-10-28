// Capture the README screenshots from a running development stack.
//
// Start the stack first: dev PostgreSQL on 127.0.0.1:55432, then
//   uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
//   cd web && pnpm dev --host 127.0.0.1 --port 5173
// and seed the demo tenant once with: uv run python scripts/seed_demo.py
//
// Screenshots land in docs/assets/screenshots/ and are published with the
// public snapshot. They show the seeded demo tenant only, never real data.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "@playwright/test";

const outDir = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../docs/assets/screenshots",
);
fs.mkdirSync(outDir, { recursive: true });

const base = process.env.MAHMOUD_SCREENSHOT_URL ?? "http://127.0.0.1:5173";
const email = "demo@almahmoud.local"; // seeded test fixture, not an application default
const password = "Al-Mahmoud-Demo-2026"; // seeded test fixture, not an application default

const browser = await chromium.launch();

async function open(viewport) {
  const context = await browser.newContext({
    viewport,
    deviceScaleFactor: 2,
    locale: "ar-IQ",
    colorScheme: "light",
  });
  return { context, page: await context.newPage() };
}

async function save(page, name) {
  await page.waitForTimeout(600);
  await page.screenshot({ path: path.join(outDir, name), animations: "disabled" });
  console.log("saved", name);
}

{
  const { context, page } = await open({ width: 1440, height: 900 });
  await page.goto(base + "/", { waitUntil: "networkidle" });
  await save(page, "login-ar.png");
  await page.getByLabel("البريد الإلكتروني").fill(email);
  await page.getByLabel("كلمة المرور").fill(password);
  await page.getByRole("button", { name: "دخول" }).click();
  await page.getByTestId("invoice-table").waitFor();
  await save(page, "invoices-ar.png");
  await page.getByRole("button", { name: /ميزان المراجعة/ }).click();
  await page.getByTestId("trial-balance-table").waitFor();
  await save(page, "trial-balance-ar.png");
  await page.getByRole("button", { name: "English" }).click();
  await page.waitForTimeout(800);
  await save(page, "trial-balance-en.png");
  await context.close();
}

{
  const { context, page } = await open({ width: 390, height: 844 });
  await page.goto(base + "/", { waitUntil: "networkidle" });
  await save(page, "login-mobile.png");
  await context.close();
}

await browser.close();
console.log("screenshots written to", outDir);
