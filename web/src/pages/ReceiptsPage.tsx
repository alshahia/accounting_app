import { useCallback, useEffect, useMemo, useState } from "react";

import {
  ApiError,
  createReceipt,
  downloadReportPath,
  listCashAccounts,
  listCustomers,
  listInvoices,
  listReceipts,
  listTaxCodes,
  postReceipt,
} from "../api";
import type { CashAccount, Customer, Invoice, Receipt, TaxCode } from "../api";
import { translations } from "../i18n";
import { ListError } from "../components/ListError";
import type { Lang } from "../i18n";
import { anyText, errorText, money, today } from "./utils";

interface ReceiptsPageProps {
  lang: Lang;
  company: { id: string; base_currency: string; permissions: string[] };
}

export function ReceiptsPage({ lang, company }: ReceiptsPageProps) {
  const t = (key: keyof (typeof translations)["ar"]): string => translations[lang][key];
  const [rows, setRows] = useState<Receipt[]>([]);
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [accounts, setAccounts] = useState<CashAccount[]>([]);
  const [taxCodes, setTaxCodes] = useState<TaxCode[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [customerId, setCustomerId] = useState("");
  const [accountId, setAccountId] = useState("");
  const [invoiceId, setInvoiceId] = useState("");
  const [amount, setAmount] = useState("");
  const [withholdingId, setWithholdingId] = useState("");
  const [receiptDate, setReceiptDate] = useState(today);

  const canCreate = company.permissions.includes("cashbank.receipt.create");
  const canPost = company.permissions.includes("cashbank.receipt.post");

  const reload = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const [receiptRows, customerRows, invoiceRows, accountRows, taxRows] = await Promise.all([
        listReceipts(signal),
        listCustomers(signal),
        listInvoices(signal),
        listCashAccounts(signal),
        listTaxCodes(signal),
      ]);
      setRows(receiptRows);
      setCustomers(customerRows);
      setInvoices(invoiceRows.filter((invoice) => invoice.status !== "draft"));
      setAccounts(accountRows);
      setTaxCodes(taxRows.filter((code) => code.kind === "withholding"));
      setCustomerId((current) => current || customerRows[0]?.id || "");
      setAccountId((current) => current || accountRows[0]?.id || "");
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
      map.set(customer.id, anyText(lang, customer.name_ar, customer.name_en));
    }
    return map;
  }, [customers, lang]);
  const openInvoices = invoices.filter((invoice) => invoice.customer_id === customerId);

  async function submit(post: boolean): Promise<void> {
    setError(null);
    setNotice(null);
    const invoice = openInvoices.find((candidate) => candidate.id === invoiceId);
    const amountMinor =
      amount.trim() === ""
        ? invoice?.total_minor ?? 0
        : Math.round(Number(amount) * 100);
    if (customerId === "" || accountId === "" || amountMinor <= 0) {
      setError(t("requiredFields"));
      return;
    }
    if (invoice !== undefined && amountMinor > invoice.total_minor) {
      setError(t("requiredFields"));
      return;
    }
    setSaving(true);
    try {
      await createReceipt({
        customer_id: customerId,
        cash_bank_account_id: accountId,
        receipt_date: receiptDate,
        amount_minor: amountMinor,
        currency: company.base_currency,
        post,
        withholding_tax_code_id: withholdingId === "" ? null : withholdingId,
        allocations:
          invoice === undefined ? [] : [{ document_id: invoice.id, amount_minor: amountMinor }],
      });
      setNotice(t("operationSaved"));
      setFormOpen(false);
      setInvoiceId("");
      setAmount("");
      setWithholdingId("");
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
      const blob = await downloadReportPath(`/cashbank/receipts/${id}/pdf`);
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
    try {
      await postReceipt(id);
      setNotice(t("operationSaved"));
      await reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    }
  }

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h1>{t("receipts")}</h1>
          <p className="muted">{rows.length} {t("receipts")}</p>
        </div>
        {canCreate ? (
          <button type="button" className="button primary" onClick={() => setFormOpen((open) => !open)}>
            {formOpen ? t("cancel") : t("newReceipt")}
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
              <select value={customerId} onChange={(event) => { setCustomerId(event.target.value); setInvoiceId(""); }}>
                {customers.map((customer) => (
                  <option key={customer.id} value={customer.id}>
                    {anyText(lang, customer.name_ar, customer.name_en)}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>{t("cashAccount")}</span>
              <select value={accountId} onChange={(event) => setAccountId(event.target.value)}>
                {accounts.map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.code} · {anyText(lang, account.name_ar, account.name_en)}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>{t("depositDate")}</span>
              <input type="date" value={receiptDate} onChange={(event) => setReceiptDate(event.target.value)} />
            </label>
            <label className="field">
              <span>{t("invoice")}</span>
              <select
                value={invoiceId}
                onChange={(event) => {
                  setInvoiceId(event.target.value);
                  const invoice = openInvoices.find((candidate) => candidate.id === event.target.value);
                  setAmount(invoice === undefined ? "" : String(invoice.total_minor / 100));
                }}
              >
                <option value="">{t("noAllocations")}</option>
                {openInvoices.map((invoice) => (
                  <option key={invoice.id} value={invoice.id}>
                    {invoice.number} · {money(invoice.total_minor, lang)}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>{t("amount")} ({company.base_currency})</span>
              <input type="number" min="0" step="0.01" value={amount} onChange={(event) => setAmount(event.target.value)} />
            </label>
            <label className="field">
              <span>{t("withholdTax")}</span>
              <select value={withholdingId} onChange={(event) => setWithholdingId(event.target.value)}>
                <option value="">—</option>
                {taxCodes.map((code) => (
                  <option key={code.id} value={code.id}>
                    {code.code} · {(code.rate_basis_points / 100).toFixed(2)}%
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="form-actions">
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
        ) : error !== null && rows.length === 0 ? (
          <ListError message={t("loadFailed")} retryLabel={t("retry")} onRetry={() => void reload()} />
        ) : rows.length === 0 ? (
          <div className="empty-state">{t("emptyOperations")}</div>
        ) : (
          <table className="data-table" data-testid="receipt-table">
            <thead>
              <tr>
                <th>{t("number")}</th>
                <th>{t("customer")}</th>
                <th>{t("depositDate")}</th>
                <th>{t("status")}</th>
                <th className="numeric">{t("amount")}</th>
                <th className="numeric">{t("withheldAmount")}</th>
                <th>{t("actions")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td className="mono" dir="ltr">{row.number}</td>
                  <td>{customerNames.get(row.customer_id) ?? row.customer_id.slice(0, 8)}</td>
                  <td className="mono" dir="ltr">{row.receipt_date}</td>
                  <td>
                    <span className={"badge " + row.status}>
                      {row.status === "posted" ? t("posted") : t("draft")}
                    </span>
                  </td>
                  <td className="numeric" dir="ltr">{money(row.amount_minor, lang)}</td>
                  <td className="numeric" dir="ltr">{money(row.withholding_amount_minor, lang)}</td>
                  <td>
                    <button
                      type="button"
                      className="button ghost"
                      onClick={() => void printPdf(row.id, row.number)}
                    >
                      {t("printPdf")}
                    </button>
                    {row.status === "draft" && canPost ? (
                      <button type="button" className="button ghost" onClick={() => void postDraft(row.id)}>
                        {t("postDocument")}
                      </button>
                    ) : null}
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
