import { expect, test } from "@playwright/test";

import { login } from "./auth";

test.describe("Al-Mahmoud accounting tools", () => {
  test("validates an opening-balance file without posting it", async ({ page }) => {
    await login(page);
    await expect(page.getByTestId("invoice-table")).toBeVisible();

    await page.getByRole("button", { name: /أدوات المحاسبة/ }).click();
    await expect(page.getByRole("heading", { name: "أدوات المحاسبة" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "الأرصدة الافتتاحية" })).toBeVisible();

    const periodSelect = page.getByLabel("الفترة");
    await expect
      .poll(async () => periodSelect.locator("option").count())
      .toBeGreaterThan(1);
    await periodSelect.selectOption({ index: 1 });

    const csv = "account_code,debit,credit,description\n1000,100.00,,Cash\n3000,,100.00,Capital\n";
    await page
      .getByLabel("الأرصدة الافتتاحية")
      .setInputFiles({ name: "opening.csv", mimeType: "text/csv", buffer: Buffer.from(csv) });
    await page.getByRole("button", { name: "تحقق فقط" }).click();

    await expect(page.getByTestId("opening-balance-table")).toBeVisible();
    await expect(page.getByTestId("opening-balance-table")).toContainText("النقدية");
    await expect(page.getByTestId("opening-balance-table")).toContainText("رأس المال");
  });

  test("shows the audit trail and the close calendar", async ({ page }) => {
    await login(page);
    await expect(page.getByTestId("invoice-table")).toBeVisible();

    await page.getByRole("button", { name: /أدوات المحاسبة/ }).click();
    await page.getByRole("tab", { name: "سجل التدقيق" }).click();
    await expect(page.getByTestId("audit-table")).toBeVisible();
    await expect(page.getByTestId("audit-table")).toContainText("auth.login");

    await page.getByRole("tab", { name: "إغلاق الفترات والسنة" }).click();
    await expect(page.getByRole("heading", { name: /FY-2026|FY2026/ })).toBeVisible();
    await expect(page.getByRole("button", { name: "إغلاق الفترة" }).first()).toBeVisible();
  });

  test("adds a custom chart account", async ({ page }) => {
    await login(page);
    await expect(page.getByTestId("invoice-table")).toBeVisible();

    await page.getByRole("button", { name: /أدوات المحاسبة/ }).click();
    await page.getByRole("tab", { name: "دليل الحسابات" }).click();
    await expect(page.getByTestId("account-table")).toBeVisible();

    const code = "T" + String(Date.now()).slice(-8);
    const name = "حساب اختبار " + code;
    await page.getByLabel("رمز الحساب").fill(code);
    await page.getByLabel("اسم الحساب", { exact: true }).fill(name);
    await page.getByRole("button", { name: "إضافة الحساب" }).click();

    await expect(page.getByRole("status")).toContainText("تم حفظ الحساب");
    await expect(page.getByTestId("account-table")).toContainText(code);
    await expect(page.getByTestId("account-table")).toContainText(name);
  });

  test("shows active sessions and prepares two-factor enrolment", async ({ page }) => {
    await login(page);
    await expect(page.getByTestId("invoice-table")).toBeVisible();

    await page.getByRole("button", { name: /أدوات المحاسبة/ }).click();
    await page.getByRole("tab", { name: "الأمان" }).click();
    await expect(page.getByTestId("session-table")).toBeVisible();
    await expect(page.getByTestId("session-table").locator("tbody tr").first()).toBeVisible();
    await expect(page.getByTestId("mfa-policy-card")).toBeVisible();
    await expect(page.getByTestId("mfa-policy-card").getByRole("checkbox").first()).not.toBeChecked();

    await page.getByRole("button", { name: "تفعيل التحقق بخطوتين" }).click();
    await expect(page.getByTestId("mfa-secret")).toBeVisible();
    await expect(page.getByTestId("mfa-secret")).not.toHaveValue("");
    await page.getByRole("button", { name: "إلغاء" }).click();
    await expect(page.getByTestId("mfa-secret")).toHaveCount(0);
  });

  test("shows the effective posting account settings", async ({ page }) => {
    await login(page);
    await expect(page.getByTestId("invoice-table")).toBeVisible();

    await page.getByRole("button", { name: /أدوات المحاسبة/ }).click();
    await page.getByRole("tab", { name: "إعدادات الترحيل" }).click();
    await expect(page.getByTestId("posting-mapping-table")).toBeVisible();
    const rows = page.getByTestId("posting-mapping-table").locator("tbody tr");
    await expect(rows).toHaveCount(15);
    await expect(page.getByTestId("posting-mapping-table")).toContainText("العملاء");
    await expect(page.getByTestId("posting-mapping-table")).toContainText("1100");

    const receivableSelect = page.getByLabel("العملاء", { exact: true });
    await expect(receivableSelect).toHaveValue(/[0-9a-f-]{36}/);

    await expect(page.getByTestId("tax-code-table")).toBeVisible();
    const code = "WHT" + String(Date.now()).slice(-6);
    await page.getByLabel("الرمز").fill(code);
    await page.getByLabel("اسم الضريبة").fill("استقطاع اختبار " + code);
    await page.getByLabel("النوع").selectOption("withholding");
    await page.getByLabel("الاختصاص الضريبي").fill("IQ");
    await page.getByLabel("النسبة %").fill("5");
    await page.getByRole("button", { name: "إضافة كود ضريبة" }).click();
    await expect(page.getByRole("status")).toContainText("تم حفظ كود الضريبة");
    await expect(page.getByTestId("tax-code-table")).toContainText(code);

    await page.getByTestId(`change-rate-${code}`).click();
    await page.getByTestId("new-rate-value").fill("7.5");
    await page.getByTestId("new-rate-from").fill("2026-02-01");
    await page.getByTestId("save-rate").click();
    await expect(page.getByRole("status")).toContainText("تم حفظ نسبة الضريبة");
    await expect(page.getByTestId("tax-code-table")).toContainText("7.50%");
  });
});
