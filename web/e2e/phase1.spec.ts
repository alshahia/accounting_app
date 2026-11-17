import { expect, test } from "@playwright/test";

import { login } from "./auth";

test.describe("Al-Mahmoud phase-1 screens", () => {
  test("lists receipts and payments from the seeded demo tenant", async ({ page }) => {
    await login(page);
    await expect(page.getByTestId("invoice-table")).toBeVisible();

    await page.getByRole("button", { name: /سندات القبض/ }).click();
    await expect(page.getByRole("heading", { name: "سندات القبض" })).toBeVisible();
    await expect(page.getByTestId("receipt-table")).toBeVisible();
    expect(await page.getByTestId("receipt-table").locator("tbody tr").count()).toBeGreaterThan(0);
    const receiptDownload = page.waitForEvent("download");
    await page
      .getByTestId("receipt-table")
      .locator("tbody tr")
      .first()
      .getByRole("button", { name: "PDF" })
      .click();
    expect((await receiptDownload).suggestedFilename()).toContain("RC-2026-");

    await page.getByRole("button", { name: /سندات الدفع/ }).click();
    await expect(page.getByRole("heading", { name: "سندات الدفع" })).toBeVisible();
    await expect(page.getByTestId("payment-table")).toBeVisible();
    expect(await page.getByTestId("payment-table").locator("tbody tr").count()).toBeGreaterThan(0);
    await expect(page.getByTestId("payment-table")).toContainText("٢٥٠٫٠٠");
  });

  test("shows credit and debit notes on the returns screen", async ({ page }) => {
    await login(page);
    await page.getByRole("button", { name: /المرتجعات/ }).click();
    await expect(page.getByRole("heading", { name: "المرتجعات" })).toBeVisible();
    await expect(page.getByTestId("return-table")).toBeVisible();
    expect(await page.getByTestId("return-table").locator("tbody tr").count()).toBeGreaterThan(0);
  });

  test("loads the profit and loss and balance sheet reports", async ({ page }) => {
    await login(page);
    await page.getByRole("button", { name: /التقارير/ }).click();
    await expect(page.getByRole("heading", { name: "التقارير" })).toBeVisible();

    await page.getByRole("button", { name: "عرض" }).click();
    await expect(page.locator(".data-table")).toContainText("صافي الربح");

    await page.getByRole("checkbox", { name: "مقارنة بالفترة السابقة" }).check();
    await page.getByRole("button", { name: "عرض" }).click();
    await expect(page.locator(".data-table")).toContainText("المبلغ السابق");
    await expect(page.locator(".data-table")).toContainText("التغير");

    await page.getByRole("tab", { name: "الميزانية العمومية" }).click();
    await page.getByRole("button", { name: "عرض" }).click();
    await expect(page.locator(".data-table")).toContainText("متوازن");
  });

  test("downloads a profit and loss PDF from the reports screen", async ({ page }) => {
    await login(page);
    await page.getByRole("button", { name: /التقارير/ }).click();
    const download = page.waitForEvent("download");
    await page.getByRole("button", { name: "PDF", exact: true }).click();
    expect((await download).suggestedFilename()).toContain("pnl");
  });
});
  test("shows the audit trail report and downloads it", async ({ page }) => {
    await login(page);
    await page.getByRole("button", { name: /التقارير/ }).click();
    await page.getByRole("tab", { name: "سجل التدقيق" }).click();
    await page.getByLabel("من تاريخ").fill("2000-01-01");
    await page.getByLabel("إلى تاريخ").fill("2100-12-31");
    await page.getByRole("button", { name: "عرض" }).click();
    await expect(page.getByTestId("audit-table")).toBeVisible();
    expect(await page.getByTestId("audit-table").locator("tbody tr").count()).toBeGreaterThan(1);

    const download = page.waitForEvent("download");
    await page.getByRole("button", { name: "PDF", exact: true }).click();
    expect((await download).suggestedFilename()).toContain("audit");
  });

  test("drills from an aging row to the source document", async ({ page }) => {
    await login(page);
    await page.getByRole("button", { name: /التقارير/ }).click();
    await page.getByRole("tab", { name: "أعمار المدينين" }).click();
    await page.getByLabel("من تاريخ").fill("2026-01-01");
    await page.getByLabel("إلى تاريخ").fill("2100-12-31");
    await page.getByRole("button", { name: "عرض" }).click();

    const link = page.getByTestId("drill-document").first();
    await expect(link).toBeVisible();
    const number = (await link.textContent())?.trim() ?? "";
    await link.click();
    const drill = page.getByTestId("report-drill");
    await expect(drill).toBeVisible();
    await expect(drill).toContainText(number);
    await expect(drill).toContainText("فواتير المبيعات");
    await expect(drill.getByRole("button", { name: "فتح القيد" })).toBeVisible();
  });

  test("drills from a document-register row to its source document", async ({ page }) => {
    await login(page);
    await page.getByRole("button", { name: /التقارير/ }).click();
    await page.getByRole("tab", { name: "سجل المستندات" }).click();
    await page.getByLabel("من تاريخ").fill("2026-01-01");
    await page.getByLabel("إلى تاريخ").fill("2100-12-31");
    await page.getByRole("button", { name: "عرض" }).click();
    await expect(page.getByTestId("register-table")).toBeVisible();

    const link = page.getByTestId("drill-document").first();
    await expect(link).toBeVisible();
    await link.click();
    await expect(page.getByTestId("report-drill")).toBeVisible();
    await page.getByTestId("report-drill").getByRole("button", { name: "إغلاق" }).click();
    await expect(page.getByTestId("report-drill")).toHaveCount(0);
  });

  test("drills from a cash-transfer register row", async ({ page }) => {
    await login(page);
    await page.getByRole("button", { name: "التحويلات", exact: true }).click();
    await page.getByRole("button", { name: "تحويل جديد" }).click();
    await page.getByLabel("المبلغ").fill("1");
    await page.getByRole("button", { name: "حفظ وترحيل" }).click();
    await expect(page.getByRole("status")).toContainText("تم حفظ السند");

    await page.getByRole("button", { name: /التقارير/ }).click();
    await page.getByRole("tab", { name: "سجل المستندات" }).click();
    await page.getByLabel("من تاريخ").fill("2026-01-01");
    await page.getByLabel("إلى تاريخ").fill("2100-12-31");
    await page.getByRole("combobox", { name: "نوع المستند" }).selectOption("cash_transfer");
    await page.getByRole("button", { name: "عرض" }).click();
    const link = page.getByTestId("register-table").getByTestId("drill-document").first();
    await expect(link).toBeVisible();
    await link.click();
    const drill = page.getByTestId("report-drill");
    await expect(drill).toBeVisible();
    await expect(drill).toContainText("التحويلات");
    await expect(drill.getByRole("button", { name: /فتح القيد/ })).toBeVisible();
  });

  test("drills from a statement row to the source document", async ({ page }) => {
    await login(page);
    await page.getByRole("button", { name: /التقارير/ }).click();
    await page.getByRole("tab", { name: "كشف حساب" }).click();
    await page.getByLabel("من تاريخ").fill("2026-01-01");
    await page.getByLabel("إلى تاريخ").fill("2100-12-31");
    const party = page.getByRole("combobox", { name: "العميل", exact: true });
    await expect(party.locator("option")).not.toHaveCount(0);
    await party.selectOption({ index: 0 });
    await page.getByRole("button", { name: "عرض" }).click();

    const link = page.getByTestId("drill-document").first();
    await expect(link).toBeVisible();
    await link.click();
    await expect(page.getByTestId("report-drill")).toBeVisible();
  });

  test("compares a party statement with the previous period", async ({ page }) => {
    await login(page);
    await page.getByRole("button", { name: /التقارير/ }).click();
    await page.getByRole("tab", { name: "كشف حساب" }).click();
    await page.getByLabel("من تاريخ").fill("2026-01-01");
    await page.getByLabel("إلى تاريخ").fill("2026-01-31");
    const party = page.getByRole("combobox", { name: "العميل", exact: true });
    await expect(party.locator("option")).not.toHaveCount(0);
    await party.selectOption({ index: 0 });
    await page.getByRole("checkbox", { name: "مقارنة بالفترة السابقة" }).check();
    await page.getByRole("button", { name: "عرض" }).click();
    await expect(page.getByTestId("statement-comparison")).toContainText(
      "الرصيد الختامي السابق",
    );

    const download = page.waitForEvent("download");
    await page.getByRole("button", { name: "Excel", exact: true }).click();
    expect((await download).suggestedFilename()).toContain("statement");
  });

  test("shows the general ledger and stock movement reports", async ({ page }) => {
    await login(page);
    await page.getByRole("button", { name: /التقارير/ }).click();
    await page.getByLabel("من تاريخ").fill("2026-01-01");
    await page.getByLabel("إلى تاريخ").fill("2026-01-31");

    await page.getByRole("tab", { name: "دفتر الأستاذ العام" }).click();
    await expect(page.getByLabel("الحساب").locator("option")).not.toHaveCount(0);
    await page.getByLabel("الحساب").selectOption({ index: 1 });
    await page.getByRole("button", { name: "عرض" }).click();
    await expect(page.getByTestId("gl-table")).toContainText("الرصيد الافتتاحي");
    await expect(page.getByTestId("gl-table")).toContainText("الرصيد الختامي");

    await page.getByRole("tab", { name: "حركة المخزون" }).click();
    await page.getByRole("button", { name: "عرض" }).click();
    await expect(page.getByTestId("stock-table")).toBeVisible();
    expect(await page.getByTestId("stock-table").locator("tbody tr").count()).toBeGreaterThan(1);

    await page.getByRole("tab", { name: "سجل المستندات" }).click();
    await page.getByRole("button", { name: "عرض" }).click();
    await expect(page.getByTestId("register-table")).toBeVisible();
    await expect(page.getByTestId("register-table")).toContainText("SI-2026-");
  });
  test("lists journal entries and opens one", async ({ page }) => {
    await login(page);
    await page.getByRole("button", { name: "قيود اليومية" }).click();
    await expect(page.getByRole("heading", { name: "قيود اليومية" })).toBeVisible();
    await expect(page.getByTestId("journal-table")).toBeVisible();

    await page.getByTestId("journal-table").locator("tbody tr").first().click();
    await expect(page.getByTestId("journal-detail")).toBeVisible();
    await expect(page.getByTestId("journal-detail")).toContainText("JE-2026-");
  });
  test("creates a manual journal entry", async ({ page }) => {
    await login(page);
    await page.getByRole("button", { name: "قيود اليومية" }).click();
    await page.getByRole("button", { name: "قيد يدوي جديد" }).click();
    await expect(page.getByTestId("journal-form")).toBeVisible();

    const rows = page.getByTestId("journal-form").locator("tbody tr");
    const pick = async (select: ReturnType<typeof rows.nth>["locator"], code: string) => {
      await expect.poll(async () => select.locator("option").count()).toBeGreaterThan(1);
      const value = await select
        .locator("option")
        .evaluateAll(
          (options, wanted) =>
            options.find((option) => (option.textContent ?? "").startsWith(wanted + " "))?.value ??
            "",
          code,
        );
      expect(value).not.toBe("");
      await select.selectOption(value);
    };
    await pick(rows.nth(0).locator("select"), "1000");
    await rows.nth(0).locator('input[type="number"]').first().fill("10.00");
    await pick(rows.nth(1).locator("select"), "3000");
    await rows.nth(1).locator('input[type="number"]').last().fill("10.00");
    await page.getByRole("button", { name: "حفظ القيد" }).click();

    await expect(page.getByRole("status")).toContainText("تم حفظ القيد");
    await expect(page.getByTestId("journal-detail")).toContainText("JE-2026-");

    await page.getByTestId("edit-draft").click();
    await expect(page.getByTestId("journal-form")).toBeVisible();
    const editRows = page.getByTestId("journal-form").locator("tbody tr");
    await editRows.nth(0).locator('input[type="number"]').first().fill("12.00");
    await editRows.nth(1).locator('input[type="number"]').last().fill("12.00");
    await page.getByRole("button", { name: "حفظ التعديلات" }).click();
    await expect(page.getByRole("status")).toContainText("تم تحديث القيد");
    await page.getByTestId("post-draft").click();
    await expect(page.getByRole("status")).toContainText("تم ترحيل القيد");
    await expect(page.getByTestId("post-draft")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "عكس القيد" })).toBeVisible();

    await page.getByRole("button", { name: "قيد يدوي جديد" }).click();
    const postedRows = page.getByTestId("journal-form").locator("tbody tr");
    const pickPosted = async (
      select: ReturnType<typeof postedRows.nth>["locator"],
      code: string,
    ) => {
      await expect.poll(async () => select.locator("option").count()).toBeGreaterThan(1);
      const value = await select
        .locator("option")
        .evaluateAll(
          (options, wanted) =>
            options.find((option) => (option.textContent ?? "").startsWith(wanted + " "))?.value ??
            "",
          code,
        );
      await select.selectOption(value);
    };
    await pickPosted(postedRows.nth(0).locator("select"), "1000");
    await postedRows.nth(0).locator('input[type="number"]').first().fill("5.00");
    await pickPosted(postedRows.nth(1).locator("select"), "3000");
    await postedRows.nth(1).locator('input[type="number"]').last().fill("5.00");
    await page.getByLabel("ترحيل فوري").check();
    await page.getByRole("button", { name: "حفظ القيد" }).click();
    await expect(page.getByTestId("journal-detail")).toContainText("JE-2026-");

    await page.getByLabel("سبب العكس").fill("تصحيح اختبار");
    await page.getByRole("button", { name: "عكس القيد" }).click();
    await expect(page.getByRole("status")).toContainText("تم عكس القيد");
  });
  test("downloads an invoice PDF from the invoice list", async ({ page }) => {
    await login(page);
    await expect(page.getByTestId("invoice-table")).toBeVisible();
    const download = page.waitForEvent("download");
    await page.getByTestId("invoice-table").locator("tbody tr").first().getByRole("button", { name: "PDF" }).click();
    expect((await download).suggestedFilename()).toContain("SI-2026-");
  });
  test("creates a warehouse and posts an inter-warehouse transfer", async ({ page }) => {
    await login(page);
    await page.getByRole("button", { name: "المخزون" }).click();
    await expect(page.getByRole("heading", { name: "المخزون", exact: true })).toBeVisible();
    await expect(page.getByTestId("warehouse-table")).toContainText("MAIN");
    await expect(page.getByTestId("warehouse-table")).toContainText("افتراضي");

    const suffix = Date.now().toString().slice(-6);
    const branchCode = `BR-${suffix}`;
    const branchName = `فرع الاختبار ${suffix}`;
    await page.getByTestId("warehouse-code").fill(branchCode);
    await page.getByTestId("warehouse-name").fill(branchName);
    await page.getByRole("button", { name: "إضافة المستودع" }).click();
    await expect(page.getByRole("status")).toContainText("تم إنشاء المستودع");
    const branchRow = page.getByTestId("warehouse-row").filter({ hasText: branchCode });
    await expect(branchRow).toBeVisible();

    await branchRow.getByRole("button", { name: "تعيين كافتراضي" }).click();
    await expect(page.getByTestId("warehouse-default")).toHaveCount(1);
    await expect(
      page.getByTestId("warehouse-row").filter({ hasText: branchCode }),
    ).toContainText("افتراضي");

    const balanceRows = page.getByTestId("balance-row");
    await expect(balanceRows.first()).toBeVisible();
    const available = await balanceRows.evaluateAll((rows) =>
      rows
        .map((row) => ({
          warehouse: (row.querySelectorAll("td")[0]?.textContent ?? "").trim(),
          item: (row.querySelectorAll("td")[1]?.textContent ?? "").split(" · ")[0] ?? "",
          quantity: Number.parseFloat(row.querySelectorAll("td")[2]?.textContent ?? "0"),
        }))
        .filter((row) => row.warehouse !== "" && row.item !== "" && row.quantity >= 1),
    );
    if (available.length > 0) {
      const { warehouse, item } = available[0];
      await page.getByTestId("transfer-from").selectOption({ label: warehouse });
      await page.getByTestId("transfer-to").selectOption({ label: branchName });
      await page
        .getByTestId("transfer-item")
        .selectOption(await page.getByTestId("transfer-item").locator("option").evaluateAll(
          (options, code) =>
            options.find((option) => (option.textContent ?? "").startsWith(String(code) + " "))?.value ?? "",
          item,
        ));
      await page.getByTestId("transfer-quantity").fill("1");
      await page.getByRole("button", { name: "ترحيل التحويل" }).click();
      await expect(page.getByRole("status")).toContainText("تم ترحيل التحويل");
      await expect(page.getByTestId("balance-table")).toContainText(branchName);

      const branchRow = page
        .getByTestId("balance-row")
        .filter({ hasText: branchName })
        .filter({ hasText: item })
        .first();
      await expect(branchRow).toBeVisible();
      const counted = ((await branchRow.locator("td").nth(2).textContent()) ?? "").trim();
      await page
        .getByTestId("count-form")
        .locator("select")
        .first()
        .selectOption(await page.getByTestId("count-warehouse").locator("option").evaluateAll(
          (options, name) =>
            options.find((option) => (option.textContent ?? "").includes(String(name)))?.value ?? "",
          branchName,
        ));
      await page.getByTestId("count-line").first().locator("select").selectOption(
        await page.getByTestId("count-line").first().locator("select").locator("option").evaluateAll(
          (options, code) =>
            options.find((option) => (option.textContent ?? "").startsWith(String(code) + " "))?.value ?? "",
          item,
        ),
      );
      await page
        .getByTestId("count-line")
        .first()
        .locator('input[type="number"]')
        .fill(counted);
      await page.getByRole("button", { name: "حفظ الجرد" }).click();
      await expect(page.getByRole("status")).toContainText("تم حفظ الجرد");
      const countRow = page.getByTestId("count-row").filter({ hasText: "مسودة" }).first();
      await expect(countRow).toBeVisible();
      await countRow.getByRole("button", { name: "ترحيل الجرد" }).click();
      await expect(page.getByRole("status")).toContainText("تم ترحيل الجرد");
      await expect(page.getByTestId("count-row").first()).toContainText("مُرحّل");
    }
  });
