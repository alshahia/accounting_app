import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  createTransfer,
  listCashAccounts,
  listTransfers,
} from "../api";
import type { CashAccount, CashTransfer } from "../api";
import { translations } from "../i18n";
import { ListError } from "../components/ListError";
import type { Lang } from "../i18n";
import { anyText, errorText, money, today } from "./utils";

interface TransfersPageProps {
  lang: Lang;
  company: { id: string; base_currency: string; permissions: string[] };
}

export function TransfersPage({ lang, company }: TransfersPageProps) {
  const t = (key: keyof (typeof translations)["ar"]): string => translations[lang][key];
  const [rows, setRows] = useState<CashTransfer[]>([]);
  const [accounts, setAccounts] = useState<CashAccount[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [fromId, setFromId] = useState("");
  const [toId, setToId] = useState("");
  const [transferDate, setTransferDate] = useState(today);
  const [amount, setAmount] = useState("");

  const canPost = company.permissions.includes("cashbank.transfer.post");

  const reload = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const [transferRows, accountRows] = await Promise.all([
        listTransfers(signal),
        listCashAccounts(signal),
      ]);
      setRows(transferRows);
      setAccounts(accountRows);
      setFromId((current) => current || accountRows[0]?.id || "");
      setToId((current) => current || accountRows[1]?.id || "");
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

  async function submit(): Promise<void> {
    setError(null);
    setNotice(null);
    const amountMinor = Math.round(Number(amount) * 100);
    if (fromId === "" || toId === "" || fromId === toId || amountMinor <= 0) {
      setError(t("requiredFields"));
      return;
    }
    setSaving(true);
    try {
      await createTransfer({
        transfer_date: transferDate,
        from_account_id: fromId,
        to_account_id: toId,
        amount_minor: amountMinor,
        currency: company.base_currency,
      });
      setNotice(t("operationSaved"));
      setFormOpen(false);
      setAmount("");
      await reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setSaving(false);
    }
  }

  const names = new Map(
    accounts.map((account) => [account.id, `${account.code} · ${anyText(lang, account.name_ar, account.name_en)}`]),
  );

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h1>{t("transfers")}</h1>
          <p className="muted">{rows.length} {t("transfers")}</p>
        </div>
        {canPost ? (
          <button type="button" className="button primary" onClick={() => setFormOpen((open) => !open)}>
            {formOpen ? t("cancel") : t("newTransfer")}
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
            void submit();
          }}
        >
          <div className="form-grid">
            <label className="field">
              <span>{t("fromAccount")}</span>
              <select value={fromId} onChange={(event) => setFromId(event.target.value)}>
                {accounts.map((account) => (
                  <option key={account.id} value={account.id}>{names.get(account.id)}</option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>{t("toAccount")}</span>
              <select value={toId} onChange={(event) => setToId(event.target.value)}>
                {accounts.map((account) => (
                  <option key={account.id} value={account.id}>{names.get(account.id)}</option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>{t("documentDate")}</span>
              <input type="date" value={transferDate} onChange={(event) => setTransferDate(event.target.value)} />
            </label>
            <label className="field">
              <span>{t("amount")} ({company.base_currency})</span>
              <input type="number" min="0" step="0.01" value={amount} onChange={(event) => setAmount(event.target.value)} />
            </label>
          </div>
          <div className="form-actions">
            <div className="spacer" />
            <button type="submit" className="button primary" disabled={saving || accounts.length < 2}>
              {saving ? t("saving") : t("saveAndPost")}
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
          <div className="empty-state">{t("emptyTransfers")}</div>
        ) : (
          <table className="data-table" data-testid="transfer-table">
            <thead>
              <tr>
                <th>{t("number")}</th>
                <th>{t("fromAccount")}</th>
                <th>{t("toAccount")}</th>
                <th>{t("documentDate")}</th>
                <th>{t("status")}</th>
                <th className="numeric">{t("amount")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td className="mono" dir="ltr">{row.number}</td>
                  <td>{names.get(row.from_account_id) ?? row.from_account_id.slice(0, 8)}</td>
                  <td>{names.get(row.to_account_id) ?? row.to_account_id.slice(0, 8)}</td>
                  <td className="mono" dir="ltr">{row.transfer_date}</td>
                  <td><span className="badge posted">{t("posted")}</span></td>
                  <td className="numeric" dir="ltr">{money(row.amount_minor, lang)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
