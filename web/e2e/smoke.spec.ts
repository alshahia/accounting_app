import { expect, test } from "@playwright/test";

import { DEMO_EMAIL, DEMO_PASSWORD, login } from "./auth";

test.describe("Al-Mahmoud phase-0 shell", () => {
  test("signs in, shows the Arabic shell and loads the trial balance", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator("html")).toHaveAttribute("dir", "rtl");
    await expect(page.getByRole("heading", { name: "تسجيل الدخول" })).toBeVisible();
    // The form ships empty: an operator types their own account, not a demo one.
    await expect(page.getByLabel("البريد الإلكتروني")).toHaveValue("");
    await expect(page.getByLabel("كلمة المرور")).toHaveValue("");

    await page.getByLabel("البريد الإلكتروني").fill(DEMO_EMAIL);
    await page.getByLabel("كلمة المرور").fill(DEMO_PASSWORD);
    await page.getByRole("button", { name: "دخول" }).click();

    await expect(page.getByRole("button", { name: /فواتير المبيعات/ })).toBeVisible();
    await expect(page.getByTestId("invoice-table")).toBeVisible();
    const invoices = page.getByTestId("invoice-table").locator("tbody tr");
    await expect(invoices.first()).toBeVisible();
    expect(await invoices.count()).toBeGreaterThanOrEqual(2);

    await page.getByRole("button", { name: /ميزان المراجعة/ }).click();
    await expect(page.getByTestId("trial-balance-table")).toBeVisible();
    await expect(page.getByTestId("balance-status")).toContainText("متوازن");
    const debit = (await page.getByTestId("total-debit").innerText()).trim();
    const credit = (await page.getByTestId("total-credit").innerText()).trim();
    expect(debit).toBe(credit);
    await expect(page.getByTestId("trial-balance-meta")).toContainText("وقت الإنشاء");

    await page.getByRole("checkbox", { name: "مقارنة بالفترة السابقة" }).check();
    await page.getByRole("button", { name: "عرض" }).click();
    await expect(page.getByRole("columnheader", { name: "الرصيد السابق" })).toBeVisible();
    await expect(page.getByRole("columnheader", { name: "التغير" })).toBeVisible();

    await page.getByTestId("tb-drill").first().click();
    await expect(page.getByTestId("tb-drill-table")).toBeVisible();
    const entryNumber = (
      await page.getByTestId("tb-entry-link").first().innerText()
    ).trim();
    await page.getByTestId("tb-entry-link").first().click();
    await expect(page.getByRole("heading", { name: "قيود اليومية", exact: true })).toBeVisible();
    await expect(page.getByTestId("journal-detail")).toContainText(entryNumber);

    await page.getByRole("button", { name: /ميزان المراجعة/ }).click();
    await expect(page.getByTestId("trial-balance-table")).toBeVisible();
    await page.getByRole("button", { name: "English" }).click();
    await expect(page.locator("html")).toHaveAttribute("dir", "ltr");
    await expect(page.getByRole("heading", { name: "Trial balance" })).toBeVisible();
  });

  test("creates and posts a customer invoice from the form", async ({ page }) => {
    await login(page);
    await expect(page.getByTestId("invoice-table")).toBeVisible();

    const rows = page.getByTestId("invoice-table").locator("tbody tr");
    const before = await rows.count();

    await page.getByRole("button", { name: "فاتورة جديدة" }).click();
    await expect(page.getByRole("button", { name: "حفظ وترحيل" })).toBeVisible();

    const line = page.locator(".line-table tbody tr").first();
    await line.getByPlaceholder("الوصف").fill("خدمة استشارية");
    await line.locator("input[type=number]").nth(0).fill("1");
    await line.locator("input[type=number]").nth(1).fill("250");
    await page.getByRole("button", { name: "حفظ وترحيل" }).click();

    await expect(page.getByRole("status")).toContainText("تم حفظ الفاتورة");
    await expect(rows).toHaveCount(before + 1);
  });
});
  test("saves a draft invoice and posts it from the list", async ({ page }) => {
    await login(page);
    await expect(page.getByTestId("invoice-table")).toBeVisible();

    const draftBadges = page.getByTestId("invoice-table").locator(".badge.draft");
    const draftsBefore = await draftBadges.count();

    await page.getByRole("button", { name: "فاتورة جديدة" }).click();
    const line = page.locator(".line-table tbody tr").first();
    await line.getByPlaceholder("الوصف").fill("مسودة للاختبار");
    await line.locator("input[type=number]").nth(0).fill("1");
    await line.locator("input[type=number]").nth(1).fill("100");
    await page.getByRole("button", { name: "حفظ كمسودة" }).click();
    await expect(page.getByRole("status")).toContainText("تم حفظ الفاتورة");
    await expect(draftBadges).toHaveCount(draftsBefore + 1);

    const draftRow = page.getByTestId("invoice-table").locator("tbody tr").filter({
      has: page.locator(".badge.draft"),
    }).first();
    await draftRow.getByRole("button", { name: "ترحيل" }).click();
    await expect(draftBadges).toHaveCount(draftsBefore);
  });

