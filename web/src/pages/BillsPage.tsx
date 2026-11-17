import { useCallback, useEffect, useMemo, useState } from "react";

import {
  ApiError,
  createBill,
  downloadReportPath,
  listBills,
  listItems,
  listSuppliers,
  listTaxCodes,
  postBill,
} from "../api";
import type { Bill, Customer, Item, TaxCode } from "../api";
import { translations } from "../i18n";
import { ListError } from "../components/ListError";
import type { Lang } from "../i18n";
import { anyText, errorText, money, today } from "./utils";

interface BillsPageProps {
  lang: Lang;
  company: { id: string; base_currency: string; permissions: string[] };
}

interface DraftLine {
  key: number;
  itemId: string;
  taxCodeId: string;
  description: string;
  quantity: string;
  unitPrice: string;
}

function newLine(key: number): DraftLine {
  return { key, itemId: "", taxCodeId: "", description: "", quantity: "1", unitPrice: "" };
}

export function BillsPage({ lang, company }: BillsPageProps) {
  const t = (key: keyof (typeof translations)["ar"]): string => translations[lang][key];
  const [bills, setBills] = useState<Bill[]>([]);
  const [suppliers, setSuppliers] = useState<Customer[]>([]);
  const [taxCodes, setTaxCodes] = useState<TaxCode[]>([]);
  const [items, setItems] = useState<Item[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [supplierId, setSupplierId] = useState("");
  const [billDate, setBillDate] = useState(today);
  const [memo, setMemo] = useState("");
  const [lines, setLines] = useState<DraftLine[]>([newLine(1)]);
  const [nextKey, setNextKey] = useState(2);

  const canCreate = company.permissions.includes("purchasing.bill.create");
  const canPost = company.permissions.includes("purchasing.bill.post");

  const reload = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const [billRows, supplierRows, taxRows, itemRows] = await Promise.all([
        listBills(signal),
        listSuppliers(signal),
        listTaxCodes(signal),
        listItems(signal),
      ]);
      setBills(billRows);
      setSuppliers(supplierRows);
      setTaxCodes(taxRows.filter((code) => code.kind !== "withholding"));
      setItems(itemRows);
      setSupplierId((current) => current || supplierRows[0]?.id || "");
      setError(null);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status !== 401) {
        setError(errorText(caught, lang));
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void reload(controller.signal);
    return () => controller.abort();
  }, [reload, company.id]);

  const supplierNames = useMemo(() => {
    const map = new Map<string, string>();
    for (const supplier of suppliers) {
      map.set(supplier.id, anyText(lang, supplier.name_ar, supplier.name_en));
    }
    return map;
  }, [suppliers, lang]);

  function updateLine(key: number, patch: Partial<DraftLine>): void {
    setLines((current) => current.map((line) => (line.key === key ? { ...line, ...patch } : line)));
  }

  function chooseItem(key: number, itemId: string): void {
    const item = items.find((candidate) => candidate.id === itemId);
    updateLine(key, {
      itemId,
      description: item === undefined ? "" : anyText(lang, item.name_ar, item.name_en),
    });
  }

  async function submit(post: boolean): Promise<void> {
    setError(null);
    setNotice(null);
    const prepared = lines
      .filter((line) => line.description.trim() !== "" && Number(line.quantity) > 0 && Number(line.unitPrice) >= 0)
      .map((line) => ({
        description: line.description.trim(),
        quantity: line.quantity,
        unit_price_minor: Math.round(Number(line.unitPrice) * 100),
        item_id: line.itemId === "" ? null : line.itemId,
        tax_code_id: line.taxCodeId === "" ? null : line.taxCodeId,
      }));
    if (supplierId === "" || prepared.length === 0 || prepared.some((line) => line.unit_price_minor <= 0)) {
      setError(t("requiredFields"));
      return;
    }
    setSaving(true);
    try {
      await createBill({
        supplier_id: supplierId,
        bill_date: billDate,
        currency: company.base_currency,
        exchange_rate: "1",
        memo: memo.trim() === "" ? null : memo.trim(),
        post,
        lines: prepared,
      });
      setFormOpen(false);
      setLines([newLine(nextKey)]);
      setNextKey((value) => value + 1);
      setMemo("");
      setNotice(t("billSaved"));
      await reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setSaving(false);
    }
  }

  async function printPdf(id: string, number: string): Promise<void> {
    setError(null);
    try {
      const blob = await downloadReportPath(`/purchasing/bills/${id}/pdf`);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${number}.pdf`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    }
  }

  async function postDraft(id: string): Promise<void> {
    setError(null);
    setNotice(null);
    try {
      await postBill(id);
      setNotice(t("billSaved"));
      await reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    }
  }

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h1>{t("bills")}</h1>
          <p className="muted">{company.base_currency} · {bills.length} {t("bills")}</p>
        </div>
        {canCreate ? (
          <button type="button" className="button primary" onClick={() => setFormOpen((open) => !open)}>
            {formOpen ? t("cancel") : t("newBill")}
          </button>
        ) : null}
      </header>

      {error !== null ? <div className="alert error" role="alert">{error}</div> : null}
      {notice !== null ? <div className="alert success" role="status">{notice}</div> : null}

      {formOpen ? (
        <form
          className="card invoice-form"
          onSubmit={(event) => {
            event.preventDefault();
            void submit(false);
          }}
        >
          <div className="form-grid">
            <label className="field">
              <span>{t("supplier")}</span>
              <select value={supplierId} onChange={(event) => setSupplierId(event.target.value)}>
                {suppliers.map((supplier) => (
                  <option key={supplier.id} value={supplier.id}>
                    {anyText(lang, supplier.name_ar, supplier.name_en)}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>{t("billDate")}</span>
              <input type="date" value={billDate} onChange={(event) => setBillDate(event.target.value)} />
            </label>
            <label className="field wide">
              <span>{t("memo")}</span>
              <input type="text" value={memo} onChange={(event) => setMemo(event.target.value)} />
            </label>
          </div>

          <div className="line-table">
            <table>
              <thead>
                <tr>
                  <th>{t("description")}</th>
                  <th>{t("tax")}</th>
                  <th>{t("quantity")}</th>
                  <th>{t("unitPrice")}</th>
                  <th aria-label={t("removeLine")} />
                </tr>
              </thead>
              <tbody>
                {lines.map((line, index) => (
                  <tr key={line.key}>
                    <td>
                      <input
                        list="purchase-items"
                        value={line.description}
                        placeholder={t("description")}
                        onChange={(event) => updateLine(line.key, { description: event.target.value })}
                      />
                      <select
                        className="inline-select"
                        value={line.itemId}
                        onChange={(event) => chooseItem(line.key, event.target.value)}
                        aria-label={t("description")}
                      >
                        <option value="">—</option>
                        {items.map((item) => (
                          <option key={item.id} value={item.id}>
                            {item.code} · {anyText(lang, item.name_ar, item.name_en)}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td>
                      <select
                        value={line.taxCodeId}
                        onChange={(event) => updateLine(line.key, { taxCodeId: event.target.value })}
                        aria-label={t("tax")}
                      >
                        <option value="">—</option>
                        {taxCodes.map((code) => (
                          <option key={code.id} value={code.id}>
                            {code.code} · {(code.rate_basis_points / 100).toFixed(2)}%
                          </option>
                        ))}
                      </select>
                    </td>
                    <td>
                      <input
                        type="number"
                        aria-label={t("quantity")}
                        min="0.0001"
                        step="0.0001"
                        value={line.quantity}
                        onChange={(event) => updateLine(line.key, { quantity: event.target.value })}
                      />
                    </td>
                    <td>
                      <input
                        type="number"
                        aria-label={t("unitPrice")}
                        min="0"
                        step="0.01"
                        value={line.unitPrice}
                        onChange={(event) => updateLine(line.key, { unitPrice: event.target.value })}
                      />
                    </td>
                    <td>
                      <button
                        type="button"
                        className="icon-button"
                        disabled={lines.length === 1}
                        onClick={() => setLines((current) => current.filter((candidate) => candidate.key !== line.key))}
                        aria-label={`${t("removeLine")} ${index + 1}`}
                      >
                        ×
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <datalist id="purchase-items">
              {items.map((item) => (
                <option key={item.id} value={anyText(lang, item.name_ar, item.name_en)} />
              ))}
            </datalist>
          </div>

          <div className="form-actions">
            <button
              type="button"
              className="button ghost"
              onClick={() => {
                setLines((current) => [...current, newLine(nextKey)]);
                setNextKey((value) => value + 1);
              }}
            >
              {t("addLine")}
            </button>
            <div className="spacer" />
            <button type="submit" className="button ghost" disabled={saving}>
              {saving ? t("saving") : t("saveDraft")}
            </button>
            {canPost ? (
              <button type="button" className="button primary" disabled={saving} onClick={() => void submit(true)}>
                {saving ? t("saving") : t("saveAndPost")}
              </button>
            ) : null}
          </div>
        </form>
      ) : null}

      <div className="card table-card">
        {loading ? (
          <div className="empty-state">{t("loading")}</div>
        ) : error !== null && bills.length === 0 ? (
          <ListError message={t("loadFailed")} retryLabel={t("retry")} onRetry={() => void reload()} />
        ) : bills.length === 0 ? (
          <div className="empty-state">{t("billsEmpty")}</div>
        ) : (
          <table className="data-table" data-testid="bill-table">
            <thead>
              <tr>
                <th>{t("number")}</th>
                <th>{t("supplier")}</th>
                <th>{t("billDate")}</th>
                <th>{t("status")}</th>
                <th className="numeric">{t("total")}</th>
                <th className="numeric">{t("baseTotal")}</th>
                <th>{t("actions")}</th>
              </tr>
            </thead>
            <tbody>
              {bills.map((bill) => (
                <tr key={bill.id}>
                  <td className="mono" dir="ltr">{bill.number}</td>
                  <td>{supplierNames.get(bill.supplier_id) ?? bill.supplier_id.slice(0, 8)}</td>
                  <td className="mono" dir="ltr">{bill.bill_date}</td>
                  <td>
                    <span className={`badge ${bill.status}`}>
                      {bill.status === "posted"
                        ? t("posted")
                        : bill.status === "part_paid"
                          ? t("partPaid")
                          : bill.status === "paid"
                            ? t("paid")
                            : t("draft")}
                    </span>
                  </td>
                  <td className="numeric" dir="ltr">{money(bill.total_minor, lang)} {bill.currency}</td>
                  <td className="numeric" dir="ltr">{money(bill.base_total_minor, lang)} {company.base_currency}</td>
                  <td className="row-actions">
                    {bill.status === "draft" && canPost ? (
                      <button type="button" className="button ghost" onClick={() => void postDraft(bill.id)}>
                        {t("postDocument")}
                      </button>
                    ) : null}
                    <button
                      type="button"
                      className="button ghost"
                      onClick={() => void printPdf(bill.id, bill.number)}
                    >
                      {t("printPdf")}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
