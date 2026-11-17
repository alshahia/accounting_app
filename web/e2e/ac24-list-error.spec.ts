import { expect, test } from "@playwright/test";

import { login } from "./auth";

// AC-24: a page rendered with the API stopped shows an error and a retry, not
// an empty table. The invoice list request is aborted while the session and the
// rest of the API stay up, which is exactly the "list endpoint failed" case.
test("a failed list offers a retry instead of an empty table", async ({ page }) => {
  await login(page);
  await page.route("**/api/v1/sales/invoices*", (route) => route.abort("failed"));
  await page.reload();

  const alert = page.getByRole("alert").filter({ hasText: /تعذّر تحميل البيانات|Could not load/ });
  await expect(alert).toBeVisible();
  const retry = page.getByRole("button", { name: /إعادة المحاولة|Retry/ });
  await expect(retry).toBeVisible();
  await expect(page.getByTestId("invoice-table")).toHaveCount(0);
  await expect(page.getByText(/لا توجد فواتير|No invoices/)).toHaveCount(0);

  await page.unroute("**/api/v1/sales/invoices*");
  await retry.click();
  await expect(page.getByTestId("invoice-table")).toBeVisible();
});
