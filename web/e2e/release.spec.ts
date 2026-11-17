import { expect, test } from "@playwright/test";

import { login } from "./auth";

test.describe("release hardening", () => {
  test("an expired access token is refreshed without losing the invoice draft", async ({ page }) => {
    await login(page);

    const draftBadges = page.getByTestId("invoice-table").locator(".badge.draft");
    const draftsBefore = await draftBadges.count();

    await page.getByRole("button", { name: "فاتورة جديدة" }).click();
    await expect(page.getByRole("button", { name: "حفظ كمسودة" })).toBeVisible();
    const line = page.locator(".line-table tbody tr").first();
    await line.getByPlaceholder("الوصف").fill("مسودة بعد انتهاء الرمز");
    await line.locator("input[type=number]").nth(0).fill("1");
    await line.locator("input[type=number]").nth(1).fill("42");

    // Simulate a tab left open past the fifteen-minute access window while the
    // refresh cookie is still alive (ruling AC-5).
    await page.evaluate(() => {
      sessionStorage.setItem("almahmoud.access_token", "expired-token-for-the-test");
    });

    await page.getByRole("button", { name: "حفظ كمسودة" }).click();

    await expect(page.getByRole("status")).toContainText("تم حفظ الفاتورة");
    // Still signed in: no bounce to the login screen and the draft reached the
    // server, so nothing the operator typed was discarded by the re-auth.
    await expect(page.getByRole("button", { name: /فواتير المبيعات/ })).toBeVisible();
    await expect(page.getByTestId("invoice-table")).toBeVisible();
    await expect(draftBadges).toHaveCount(draftsBefore + 1);
  });

  test("signing out ends the session and the browser cannot silently sign back in", async ({
    page,
  }) => {
    await login(page);

    await page.getByRole("button", { name: /خروج/ }).click();
    await expect(page.getByRole("heading", { name: "تسجيل الدخول" })).toBeVisible();

    await page.reload();
    await expect(page.getByRole("heading", { name: "تسجيل الدخول" })).toBeVisible();
    await expect(page.getByTestId("invoice-table")).not.toBeVisible();
  });
});
