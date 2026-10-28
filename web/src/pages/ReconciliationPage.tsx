import { useCallback, useEffect, useMemo, useState } from "react";

import {
  ApiError,
  importBankStatementLines,
  listBankStatementLines,
  listCashAccounts,
  listPayments,
  listReceipts,
  matchBankStatementLine,
  unmatchBankStatementLine,
} from "../api";
import type { BankStatementLine, CashAccount, Payment, Receipt } from "../api";
import { translations } from "../i18n";
import { ListError } from "../components/ListError";
import type { Lang } from "../i18n";
import { anyText, errorText, money, today } from "./utils";

interface ReconciliationPageProps {
  lang: Lang;
  company: { id: string; base_currency: string; permissions: string[] };
}

export function ReconciliationPage({ lang, company }: ReconciliationPageProps) {
  const t = (key: keyof (typeof translations)["ar"]): string => translations[lang][key];
  const [rows, setRows] = useState<BankStatementLine[]>([]);
  const [accounts, setAccounts] = useState<CashAccount[]>([]);
  const [receipts, setReceipts] = useState<Receipt[]>([]);
  const [payments, setPayments] = useState<Payment[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [accountId, setAccountId] = useState("");
  const [statementDate, setStatementDate] = useState(today);
  const [description, setDescription] = useState("");
  const [reference, setReference] = useState("");
  const [amount, setAmount] = useState("");
  const [selection, setSelection] = useState<Record<string, string>>({});

  const canReconcile = company.permissions.includes("cashbank.reconcile");

  const reload = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const [lineRows, accountRows, receiptRows, paymentRows] = await Promise.all([
        listBankStatementLines(undefined, signal),
        listCashAccounts(signal),
        listReceipts(signal),
        listPayments(signal),
      ]);
      setRows(lineRows);
      setAccounts(accountRows);
      setReceipts(receiptRows.filter((receipt) => receipt.status === "posted"));
      setPayments(paymentRows.filter((payment) => payment.status === "posted"));
      setAccountId((current) => current || accountRows.find((account) => account.kind === "bank")?.id || accountRows[0]?.id || "");
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

  const options = useMemo(() => {
    const list: { value: string; label: string }[] = [];
    for (const receipt of receipts) {
      list.push({
        value: `receipt:${receipt.id}`,
        label: `+${money(receipt.amount_minor, lang)} · ${receipt.number}`,
      });
    }
    for (const payment of payments) {
      list.push({
        value: `supplier_payment:${payment.id}`,
        label: `-${money(payment.amount_minor, lang)} · ${payment.number}`,
      });
    }
    return list;
  }, [receipts, payments, lang]);

  async function importLine(): Promise<void> {
    setError(null);
    setNotice(null);
    const amountMinor = Math.round(Number(amount) * 100);
    if (accountId === "" || description.trim() === "" || amountMinor === 0) {
      setError(t("requiredFields"));
      return;
    }
    setSaving(true);
    try {
      await importBankStatementLines({
        cash_bank_account_id: accountId,
        currency: company.base_currency,
        lines: [
          {
            statement_date: statementDate,
            description: description.trim(),
            amount_minor: amountMinor,
            reference: reference.trim() === "" ? null : reference.trim(),
          },
        ],
      });
      setNotice(t("operationSaved"));
      setDescription("");
      setReference("");
      setAmount("");
      setFormOpen(false);
      await reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setSaving(false);
    }
  }

  async function match(lineId: string): Promise<void> {
    const value = selection[lineId];
    if (value === undefined) {
      return;
    }
    const [sourceType, sourceId] = value.split(":");
    if (sourceType === undefined || sourceId === undefined) {
      return;
    }
    setError(null);
    try {
      await matchBankStatementLine(lineId, {
        source_type: sourceType as "receipt" | "supplier_payment",
        source_id: sourceId,
      });
      setNotice(t("operationSaved"));
      await reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    }
  }

  async function unmatch(lineId: string): Promise<void> {
    setError(null);
    try {
      await unmatchBankStatementLine(lineId);
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
          <h1>{t("reconciliation")}</h1>
          <p className="muted">{rows.filter((row) => row.status === "unmatched").length} {t("unmatched")}</p>
        </div>
        {canReconcile ? (
          <button type="button" className="button primary" onClick={() => setFormOpen((open) => !open)}>
            {formOpen ? t("cancel") : t("importLine")}
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
            void importLine();
          }}
        >
          <div className="form-grid">
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
              <span>{t("statementDate")}</span>
              <input type="date" value={statementDate} onChange={(event) => setStatementDate(event.target.value)} />
            </label>
            <label className="field">
              <span>{t("description")}</span>
              <input value={description} onChange={(event) => setDescription(event.target.value)} />
            </label>
            <label className="field">
              <span>{t("reference")}</span>
              <input value={reference} onChange={(event) => setReference(event.target.value)} />
            </label>
            <label className="field">
              <span>{t("amount")} ({company.base_currency})</span>
              <input type="number" step="0.01" value={amount} onChange={(event) => setAmount(event.target.value)} />
            </label>
          </div>
          <div className="form-actions">
            <div className="spacer" />
            <button type="submit" className="button primary" disabled={saving}>
              {saving ? t("saving") : t("importLine")}
            </button>
          </div>
        </form>
      ) : null}

      <div className="card table-card">
        {loading ? (
          <div className="empty-state">{t("loading")}</div>
        ) : error !== null && rows.length === 0 ? (
          <ListError message={t("loadFailed")} retryLabel={t("retry")} onRetry={() => void reload()} />
        ) : rows.length === 0 ? (
          <div className="empty-state">{t("emptyStatements")}</div>
        ) : (
          <table className="data-table" data-testid="statement-table">
            <thead>
              <tr>
                <th>{t("statementDate")}</th>
                <th>{t("description")}</th>
                <th>{t("reference")}</th>
                <th className="numeric">{t("amount")}</th>
                <th>{t("status")}</th>
                <th>{t("actions")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td className="mono" dir="ltr">{row.statement_date}</td>
                  <td>{row.description}</td>
                  <td className="mono" dir="ltr">{row.reference ?? "—"}</td>
                  <td className="numeric" dir="ltr">{money(row.amount_minor, lang)}</td>
                  <td>
                    <span className={"badge " + row.status}>{t(row.status === "matched" ? "matched" : "unmatched")}</span>
                  </td>
                  <td>
                    {canReconcile ? (
                      row.status === "matched" ? (
                        <button type="button" className="button ghost" onClick={() => void unmatch(row.id)}>
                          {t("unmatch")}
                        </button>
                      ) : (
                        <div className="row-actions">
                          <select
                            value={selection[row.id] ?? ""}
                            onChange={(event) => setSelection({ ...selection, [row.id]: event.target.value })}
                          >
                            <option value="">{t("selectDocument")}</option>
                            {options.map((option) => (
                              <option key={option.value} value={option.value}>{option.label}</option>
                            ))}
                          </select>
                          <button
                            type="button"
                            className="button ghost"
                            disabled={(selection[row.id] ?? "") === ""}
                            onClick={() => void match(row.id)}
                          >
                            {t("match")}
                          </button>
                        </div>
                      )
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
