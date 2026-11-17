import { useCallback, useEffect, useMemo, useState } from "react";

import {
  ApiError,
  createInvoice,
  downloadReportPath,
  listCustomers,
  listInvoices,
  listTaxCodes,
  listItems,
  postInvoice,
} from "../api";
import type { Customer, Invoice, Item, TaxCode } from "../api";
import { translations } from "../i18n";
import { ListError } from "../components/ListError";
import { errorText, today } from "./utils";
import type { Lang } from "../i18n";

interface InvoicesPageProps {
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

function money(minor: number, lang: Lang): string {
  return new Intl.NumberFormat(lang === "ar" ? "ar-IQ" : "en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(minor / 100);
}

export function InvoicesPage({ lang, company }: InvoicesPageProps) {
  const t = (key: keyof (typeof translations)["ar"]): string => translations[lang][key];
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [taxCodes, setTaxCodes] = useState<TaxCode[]>([]);
  const [items, setItems] = useState<Item[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [customerId, setCustomerId] = useState("");
  const [invoiceDate, setInvoiceDate] = useState(today);
  const [memo, setMemo] = useState("");
  const [lines, setLines] = useState<DraftLine[]>([newLine(1)]);
  const [nextKey, setNextKey] = useState(2);

  const canCreate = company.permissions.includes("sales.invoice.create");
  const canPost = company.permissions.includes("sales.invoice.post");

  const reload = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const [invoiceRows, customerRows, taxRows, itemRows] = await Promise.all([
        listInvoices(signal),
        listCustomers(signal),
        listTaxCodes(signal),
        listItems(signal),
      ]);
      setInvoices(invoiceRows);
      setCustomers(customerRows);
      setTaxCodes(taxRows);
      setItems(itemRows);
      setCustomerId((current) => current || customerRows[0]?.id || "");
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

  const customerNames = useMemo(() => {
    const map = new Map<string, string>();
    for (const customer of customers) {
      map.set(customer.id, lang === "ar" ? customer.name_ar : customer.name_en ?? customer.name_ar);
    }
    return map;
  }, [customers, lang]);

  function updateLine(key: number, patch: Partial<DraftLine>): void {
    setLines((current) => current.map((line) => (line.key === key ? { ...line, ...patch } : line)));
  }

  function chooseItem(key: number, itemId: string): void {
    const item = items.find((candidate) => candidate.id === itemId);
    updateLine(key, {
      itemId,
      description: item === undefined ? "" : lang === "ar" ? item.name_ar : item.name_en ?? item.name_ar,
    });
  }

  async function printPdf(id: string, number: string): Promise<void> {
    setError(null);
    try {
      const blob = await downloadReportPath(`/sales/invoices/${id}/pdf`);
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
      await postInvoice(id);
      setNotice(t("invoiceSaved"));
      await reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    }
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
    if (customerId === "" || prepared.length === 0 || prepared.some((line) => line.unit_price_minor <= 0)) {
      setError(t("requiredFields"));
      return;
    }
    setSaving(true);
    try {
      await createInvoice({
        customer_id: customerId,
        invoice_date: invoiceDate,
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
      setNotice(t("invoiceSaved"));
      await reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h1>{t("invoices")}</h1>
          <p className="muted">{company.base_currency} · {invoices.length} {t("lines")}</p>
        </div>
        {canCreate ? (
          <button type="button" className="button primary" onClick={() => setFormOpen((open) => !open)}>
            {formOpen ? t("cancel") : t("newInvoice")}
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
              <span>{t("customer")}</span>
              <select value={customerId} onChange={(event) => setCustomerId(event.target.value)}>
                {customers.map((customer) => (
                  <option key={customer.id} value={customer.id}>
                    {lang === "ar" ? customer.name_ar : customer.name_en ?? customer.name_ar}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>{t("invoiceDate")}</span>
              <input type="date" value={invoiceDate} onChange={(event) => setInvoiceDate(event.target.value)} />
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
                        list="items"
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
                            {item.code} · {lang === "ar" ? item.name_ar : item.name_en ?? item.name_ar}
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
            <datalist id="items">
              {items.map((item) => (
                <option key={item.id} value={lang === "ar" ? item.name_ar : item.name_en ?? item.name_ar} />
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
        ) : error !== null && invoices.length === 0 ? (
          <ListError message={t("loadFailed")} retryLabel={t("retry")} onRetry={() => void reload()} />
        ) : invoices.length === 0 ? (
          <div className="empty-state">{t("invoicesEmpty")}</div>
        ) : (
          <table className="data-table" data-testid="invoice-table">
            <thead>
              <tr>
                <th>{t("number")}</th>
                <th>{t("customer")}</th>
                <th>{t("invoiceDate")}</th>
                <th>{t("status")}</th>
                <th className="numeric">{t("total")}</th>
                <th className="numeric">{t("baseTotal")}</th>
                <th>{t("actions")}</th>
              </tr>
            </thead>
            <tbody>
              {invoices.map((invoice) => (
                <tr key={invoice.id}>
                  <td className="mono" dir="ltr">{invoice.number}</td>
                  <td>{customerNames.get(invoice.customer_id) ?? invoice.customer_id.slice(0, 8)}</td>
                  <td className="mono" dir="ltr">{invoice.invoice_date}</td>
                  <td>
                    <span className={`badge ${invoice.status}`}>
                      {invoice.status === "posted"
                        ? t("posted")
                        : invoice.status === "part_paid"
                          ? t("partPaid")
                          : invoice.status === "paid"
                            ? t("paid")
                            : t("draft")}
                    </span>
                  </td>
                  <td className="numeric" dir="ltr">{money(invoice.total_minor, lang)} {invoice.currency}</td>
                  <td className="numeric" dir="ltr">{money(invoice.base_total_minor, lang)} {company.base_currency}</td>
                  <td className="row-actions">
                    {invoice.status === "draft" && canPost ? (
                      <button type="button" className="button ghost" onClick={() => void postDraft(invoice.id)}>
                        {t("postDocument")}
                      </button>
                    ) : null}
                    <button
                      type="button"
                      className="button ghost"
                      onClick={() => void printPdf(invoice.id, invoice.number)}
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
