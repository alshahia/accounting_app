import { expect, test } from "@playwright/test";

import { login } from "./auth";

test.describe("Al-Mahmoud settlement screens", () => {
  test("shows cheques and their lifecycle status", async ({ page }) => {
    await login(page);
    await expect(page.getByTestId("invoice-table")).toBeVisible();

    await page.getByRole("button", { name: /الشيكات/ }).click();
    await expect(page.getByRole("heading", { name: "الشيكات" })).toBeVisible();
    const table = page.getByTestId("cheque-table");
    await expect(table).toBeVisible();
    await expect(table).toContainText("CH-1001");
    await expect(table).toContainText("CH-2001");
    await expect(table).toContainText("مودع");
    await expect(table).toContainText("شيك صادر");
  });

  test("lists cash transfers between cash and bank", async ({ page }) => {
    await login(page);
    await expect(page.getByTestId("invoice-table")).toBeVisible();

    await page.getByRole("button", { name: /التحويلات/ }).click();
    await expect(page.getByRole("heading", { name: "التحويلات" })).toBeVisible();
    const table = page.getByTestId("transfer-table");
    await expect(table).toBeVisible();
    await expect(table).toContainText("الصندوق الرئيسي");
    await expect(table).toContainText("الحساب البنكي الرئيسي");
  });

  test("lists bank statement lines with matched and unmatched status", async ({ page }) => {
    await login(page);
    await expect(page.getByTestId("invoice-table")).toBeVisible();

    await page.getByRole("button", { name: /المطابقة البنكية/ }).click();
    await expect(page.getByRole("heading", { name: "المطابقة البنكية" })).toBeVisible();
    const table = page.getByTestId("statement-table");
    await expect(table).toBeVisible();
    await expect(table).toContainText("Bank charges");
    await expect(table).toContainText("غير مطابق");
    await expect(table).toContainText("مطابق");
  });
});

test.describe("Al-Mahmoud purchase bills", () => {
  test("creates and posts a supplier bill from the form", async ({ page }) => {
    await login(page);
    await expect(page.getByTestId("invoice-table")).toBeVisible();

    await page.getByRole("button", { name: /فواتير الشراء/ }).click();
    await expect(page.getByTestId("bill-table")).toBeVisible();
    const rows = page.getByTestId("bill-table").locator("tbody tr");
    const before = await rows.count();

    await page.getByRole("button", { name: "فاتورة شراء جديدة" }).click();
    await expect(page.getByRole("button", { name: "حفظ وترحيل" })).toBeVisible();

    const line = page.locator(".line-table tbody tr").first();
    await line.getByPlaceholder("الوصف").fill("خدمة صيانة");
    await line.locator("input[type=number]").nth(0).fill("2");
    await line.locator("input[type=number]").nth(1).fill("75");
    await page.getByRole("button", { name: "حفظ وترحيل" }).click();

    await expect(page.getByRole("status")).toContainText("تم حفظ فاتورة الشراء");
    await expect(rows).toHaveCount(before + 1);
  });
});

