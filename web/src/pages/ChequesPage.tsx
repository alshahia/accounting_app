import { useCallback, useEffect, useMemo, useState } from "react";

import {
  ApiError,
  chequeAction,
  createCheque,
  listBills,
  listCashAccounts,
  listCheques,
  listCustomers,
  listInvoices,
  listSuppliers,
} from "../api";
import type { Bill, CashAccount, Cheque, Customer, Invoice } from "../api";
import { translations } from "../i18n";
import { ListError } from "../components/ListError";
import type { Lang } from "../i18n";
import { anyText, errorText, money, today } from "./utils";

interface ChequesPageProps {
  lang: Lang;
  company: { id: string; base_currency: string; permissions: string[] };
}

const STATUS_KEY = {
  in_hand: "inHand",
  deposited: "deposited",
  cleared: "cleared",
  issued: "issued",
  bounced: "bounced",
  cancelled: "cancelled",
} as const;

export function ChequesPage({ lang, company }: ChequesPageProps) {
  const t = (key: keyof (typeof translations)["ar"]): string => translations[lang][key];
  const [rows, setRows] = useState<Cheque[]>([]);
  const [accounts, setAccounts] = useState<CashAccount[]>([]);
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [suppliers, setSuppliers] = useState<Customer[]>([]);
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [bills, setBills] = useState<Bill[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [direction, setDirection] = useState<"received" | "issued">("received");
  const [number, setNumber] = useState("");
  const [accountId, setAccountId] = useState("");
  const [partyId, setPartyId] = useState("");
  const [issueDate, setIssueDate] = useState(today);
  const [dueDate, setDueDate] = useState(today);
  const [amount, setAmount] = useState("");
  const [documentId, setDocumentId] = useState("");

  const canCreate = company.permissions.includes("cashbank.cheque.create");
  const canPost = company.permissions.includes("cashbank.cheque.post");

  const reload = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const [chequeRows, accountRows, customerRows, supplierRows, invoiceRows, billRows] =
        await Promise.all([
          listCheques(undefined, signal),
          listCashAccounts(signal),
          listCustomers(signal),
          listSuppliers(signal),
          listInvoices(signal),
          listBills(signal),
        ]);
      setRows(chequeRows);
      setAccounts(accountRows);
      setCustomers(customerRows);
      setSuppliers(supplierRows);
      setInvoices(invoiceRows.filter((invoice) => invoice.status !== "draft"));
      setBills(billRows.filter((bill) => bill.status !== "draft"));
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

  const parties = direction === "received" ? customers : suppliers;
  const openDocuments =
    direction === "received"
      ? invoices.filter((invoice) => invoice.customer_id === partyId)
      : bills.filter((bill) => bill.supplier_id === partyId);

  useEffect(() => {
    setPartyId(parties[0]?.id ?? "");
    setDocumentId("");
  }, [direction, parties]);

  const partyNames = useMemo(() => {
    const map = new Map<string, string>();
    for (const party of [...customers, ...suppliers]) {
      map.set(party.id, anyText(lang, party.name_ar, party.name_en));
    }
    return map;
  }, [customers, suppliers, lang]);

  async function submit(post: boolean): Promise<void> {
    setError(null);
    setNotice(null);
    const documentTotal =
      direction === "received"
        ? invoices.find((invoice) => invoice.id === documentId)?.total_minor
        : bills.find((bill) => bill.id === documentId)?.total_minor;
    const amountMinor =
      amount.trim() === "" ? documentTotal ?? 0 : Math.round(Number(amount) * 100);
    if (number.trim() === "" || accountId === "" || partyId === "" || amountMinor <= 0) {
      setError(t("requiredFields"));
      return;
    }
    setSaving(true);
    try {
      await createCheque({
        direction,
        number: number.trim(),
        cash_bank_account_id: accountId,
        issue_date: issueDate,
        due_date: dueDate || null,
        amount_minor: amountMinor,
        customer_id: direction === "received" ? partyId : null,
        supplier_id: direction === "issued" ? partyId : null,
        currency: company.base_currency,
        post,
        allocations:
          documentId === "" ? [] : [{ document_id: documentId, amount_minor: amountMinor }],
      });
      setNotice(t("operationSaved"));
      setFormOpen(false);
      setNumber("");
      setAmount("");
      setDocumentId("");
      await reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setSaving(false);
    }
  }

  async function runAction(
    id: string,
    action: "deposit" | "clear" | "bounce" | "cancel",
  ): Promise<void> {
    setError(null);
    setNotice(null);
    try {
      await chequeAction(id, action, today());
      setNotice(t("operationSaved"));
      await reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    }
  }

  function actionsFor(row: Cheque): { label: string; action: "deposit" | "clear" | "bounce" | "cancel" }[] {
    if (row.direction === "received") {
      if (row.status === "in_hand") {
        return [
          { label: t("deposit"), action: "deposit" },
          { label: t("bounce"), action: "bounce" },
          { label: t("cancel"), action: "cancel" },
        ];
      }
      if (row.status === "deposited") {
        return [
          { label: t("clear"), action: "clear" },
          { label: t("bounce"), action: "bounce" },
        ];
      }
      return [];
    }
    if (row.status === "issued") {
      return [
        { label: t("clear"), action: "clear" },
        { label: t("cancel"), action: "cancel" },
      ];
    }
    return [];
  }

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h1>{t("cheques")}</h1>
          <p className="muted">{rows.length} {t("cheques")}</p>
        </div>
        {canCreate ? (
          <button type="button" className="button primary" onClick={() => setFormOpen((open) => !open)}>
            {formOpen ? t("cancel") : t("newCheque")}
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
              <span>{t("status")}</span>
              <select
                value={direction}
                onChange={(event) => setDirection(event.target.value as "received" | "issued")}
              >
                <option value="received">{t("received")}</option>
                <option value="issued">{t("issued")}</option>
              </select>
            </label>
            <label className="field">
              <span>{t("chequeNumber")}</span>
              <input value={number} onChange={(event) => setNumber(event.target.value)} />
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
              <span>{direction === "received" ? t("customer") : t("supplier")}</span>
              <select value={partyId} onChange={(event) => { setPartyId(event.target.value); setDocumentId(""); }}>
                {parties.map((party) => (
                  <option key={party.id} value={party.id}>
                    {anyText(lang, party.name_ar, party.name_en)}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>{t("documentDate")}</span>
              <input type="date" value={issueDate} onChange={(event) => setIssueDate(event.target.value)} />
            </label>
            <label className="field">
              <span>{t("dueDate")}</span>
              <input type="date" value={dueDate} onChange={(event) => setDueDate(event.target.value)} />
            </label>
            <label className="field">
              <span>{direction === "received" ? t("invoice") : t("bill")}</span>
              <select
                value={documentId}
                onChange={(event) => {
                  setDocumentId(event.target.value);
                  const document = openDocuments.find((candidate) => candidate.id === event.target.value);
                  setAmount(document === undefined ? "" : String(document.total_minor / 100));
                }}
              >
                <option value="">{t("noAllocations")}</option>
                {openDocuments.map((document) => (
                  <option key={document.id} value={document.id}>
                    {document.number} · {money(document.total_minor, lang)}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>{t("amount")} ({company.base_currency})</span>
              <input type="number" min="0" step="0.01" value={amount} onChange={(event) => setAmount(event.target.value)} />
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
          <div className="empty-state">{t("emptyCheques")}</div>
        ) : (
          <table className="data-table" data-testid="cheque-table">
            <thead>
              <tr>
                <th>{t("chequeNumber")}</th>
                <th>{t("status")}</th>
                <th>{t("customer")} / {t("supplier")}</th>
                <th>{t("documentDate")}</th>
                <th>{t("dueDate")}</th>
                <th className="numeric">{t("amount")}</th>
                <th>{t("actions")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const party = row.customer_id ?? row.supplier_id ?? "";
                return (
                  <tr key={row.id}>
                    <td className="mono" dir="ltr">{row.number}</td>
                    <td>
                      <span className={"badge " + row.status}>{t(STATUS_KEY[row.status])}</span>
                    </td>
                    <td>{partyNames.get(party) ?? party.slice(0, 8)}</td>
                    <td className="mono" dir="ltr">{row.issue_date}</td>
                    <td className="mono" dir="ltr">{row.due_date}</td>
                    <td className="numeric" dir="ltr">{money(row.amount_minor, lang)}</td>
                    <td>
                      {canPost ? (
                        <div className="row-actions">
                          {actionsFor(row).map((item) => (
                            <button
                              key={item.action}
                              type="button"
                              className="button ghost"
                              onClick={() => void runAction(row.id, item.action)}
                            >
                              {item.label}
                            </button>
                          ))}
                        </div>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
