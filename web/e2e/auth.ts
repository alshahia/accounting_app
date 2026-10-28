import { expect } from "@playwright/test";
import type { Page } from "@playwright/test";

// The demo tenant is seeded by global-setup.ts (scripts/seed_demo.py). These
// are test fixtures, not application defaults: the login form itself starts
// empty and ships no credentials (ruling AC-9).
export const DEMO_EMAIL = "demo@almahmoud.local";
export const DEMO_PASSWORD = "Al-Mahmoud-Demo-2026";

export async function login(page: Page): Promise<void> {
  await page.goto("/");
  await page.getByLabel("البريد الإلكتروني").fill(DEMO_EMAIL);
  await page.getByLabel("كلمة المرور").fill(DEMO_PASSWORD);
  await page.getByRole("button", { name: "دخول" }).click();
  await expect(page.getByTestId("invoice-table")).toBeVisible();
}
