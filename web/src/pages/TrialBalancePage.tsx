import { useCallback, useEffect, useState } from "react";

import { ApiError, downloadReport, generalLedger, trialBalance } from "../api";
import type { GeneralLedger, TrialBalance } from "../api";
import { translations } from "../i18n";
import { ListError } from "../components/ListError";
import { errorText, today } from "./utils";
import type { Lang } from "../i18n";

interface TrialBalancePageProps {
  lang: Lang;
  company: { id: string; base_currency: string };
  onOpenEntry: (entryId: string) => void;
}

function yearStart(): string {
  return `${new Date().getFullYear()}-01-01`;
}

function money(minor: number, lang: Lang): string {
  return new Intl.NumberFormat(lang === "ar" ? "ar-IQ" : "en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(minor / 100);
}

export function TrialBalancePage({ lang, company, onOpenEntry }: TrialBalancePageProps) {
  const t = (key: keyof (typeof translations)["ar"]): string => translations[lang][key];
  const [start, setStart] = useState(yearStart);
  const [end, setEnd] = useState(today);
  const [range, setRange] = useState({ start: yearStart(), end: today() });
  const [balance, setBalance] = useState<TrialBalance | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState<"pdf" | "xlsx" | null>(null);
  const [drill, setDrill] = useState<GeneralLedger | null>(null);
  const [drillLoading, setDrillLoading] = useState(false);
  const [compare, setCompare] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const result = await trialBalance(range.start, range.end, { compare, signal });
      setBalance(result);
      setError(null);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status !== 401) {
        setError(errorText(caught, lang));
      }
    } finally {
      setLoading(false);
    }
  }, [range, compare]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, company.id]);

  async function download(format: "pdf" | "xlsx"): Promise<void> {
    setDownloading(format);
    setError(null);
    try {
      const blob = await downloadReport(format, range.start, range.end, compare);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `trial-balance.${format}`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setDownloading(null);
    }
  }

  async function drillInto(accountId: string, code: string): Promise<void> {
    setDrillLoading(true);
    setError(null);
    try {
      setDrill(await generalLedger(accountId, range.start, range.end));
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
      setDrill(null);
    } finally {
      setDrillLoading(false);
    }
    // Keep the account identity visible even while the request is in flight.
    void code;
  }

  const rows = balance?.rows ?? [];
  const meta = balance?.meta ?? null;

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h1>{t("trialBalance")}</h1>
          <p className="muted">{company.base_currency} · {range.start} → {range.end}</p>
          {meta !== null ? (
            <p className="muted report-meta" data-testid="trial-balance-meta">
              {t("generatedAt")}: {new Date(meta.generated_at).toLocaleString()} · {t("generatedBy")}:{" "}
              {meta.generated_by} · {t("companyLabel")}: {meta.company_name_ar}
            </p>
          ) : null}
        </div>
        <div className="page-actions">
          <button
            type="button"
            className="button ghost"
            disabled={downloading !== null}
            onClick={() => void download("pdf")}
          >
            {downloading === "pdf" ? t("loading") : t("downloadPdf")}
          </button>
          <button
            type="button"
            className="button ghost"
            disabled={downloading !== null}
            onClick={() => void download("xlsx")}
          >
            {downloading === "xlsx" ? t("loading") : t("downloadExcel")}
          </button>
        </div>
      </header>

      <form
        className="card filter-bar"
        onSubmit={(event) => {
          event.preventDefault();
          setRange({ start, end });
        }}
      >
        <label className="field">
          <span>{t("from")}</span>
          <input type="date" value={start} onChange={(event) => setStart(event.target.value)} />
        </label>
        <label className="field">
          <span>{t("to")}</span>
          <input type="date" value={end} onChange={(event) => setEnd(event.target.value)} />
        </label>
        <label className="field checkbox-field">
          <input
            type="checkbox"
            checked={compare}
            onChange={(event) => setCompare(event.target.checked)}
          />
          <span>{t("comparePeriod")}</span>
        </label>
        <button type="submit" className="button primary">{t("show")}</button>
      </form>

      {error !== null ? <div className="alert error" role="alert">{error}</div> : null}

      {balance !== null ? (
        <div className="balance-banner" data-testid="balance-status">
          <span className={balance.balanced ? "dot ok" : "dot bad"} aria-hidden="true" />
          {balance.balanced ? t("balanced") : t("unbalanced")}
          <span className="spacer" />
          <span>
            {t("debitTotal")}: <strong data-testid="total-debit">{money(balance.total_debit_minor, lang)}</strong>
          </span>
          <span>
            {t("creditTotal")}: <strong data-testid="total-credit">{money(balance.total_credit_minor, lang)}</strong>
          </span>
        </div>
      ) : null}

      <div className="card table-card">
        {loading ? (
          <div className="empty-state">{t("loading")}</div>
        ) : error !== null && rows.length === 0 ? (
          <ListError message={t("loadFailed")} retryLabel={t("retry")} onRetry={() => void load()} />
        ) : rows.length === 0 ? (
          <div className="empty-state">{t("noData")}</div>
        ) : (
          <table className="data-table" data-testid="trial-balance-table">
            <thead>
              <tr>
                <th>{t("accountCode")}</th>
                <th>{t("accountName")}</th>
                <th className="numeric">{t("debit")}</th>
                <th className="numeric">{t("credit")}</th>
                <th className="numeric">{t("balance")}</th>
                {compare ? (
                  <>
                    <th className="numeric">{t("compareBalance")}</th>
                    <th className="numeric">{t("change")}</th>
                  </>
                ) : null}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.account_id}>
                  <td className="mono" dir="ltr">
                    <button
                      type="button"
                      className="link-button"
                      data-testid="tb-drill"
                      onClick={() => void drillInto(row.account_id, row.code)}
                      title={t("drillHint")}
                    >
                      {row.code}
                    </button>
                  </td>
                  <td>{lang === "ar" ? row.name_ar : row.name_en ?? row.name_ar}</td>
                  <td className="numeric" dir="ltr">{row.debit_minor === 0 ? "—" : money(row.debit_minor, lang)}</td>
                  <td className="numeric" dir="ltr">{row.credit_minor === 0 ? "—" : money(row.credit_minor, lang)}</td>
                  <td className="numeric" dir="ltr">{money(row.balance_minor, lang)}</td>
                  {compare ? (
                    <>
                      <td className="numeric" dir="ltr">
                        {money(row.compare_balance_minor ?? 0, lang)}
                      </td>
                      <td className="numeric" dir="ltr">
                        {money(row.balance_minor - (row.compare_balance_minor ?? 0), lang)}
                      </td>
                    </>
                  ) : null}
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <td colSpan={2}>{t("total")}</td>
                <td className="numeric" dir="ltr">{money(balance?.total_debit_minor ?? 0, lang)}</td>
                <td className="numeric" dir="ltr">{money(balance?.total_credit_minor ?? 0, lang)}</td>
                <td />
                {compare ? (
                  <>
                    <td />
                    <td />
                  </>
                ) : null}
              </tr>
            </tfoot>
          </table>
        )}
      </div>

      {drillLoading ? <p className="muted">{t("loading")}</p> : null}
      {drill !== null ? (
        <div className="card" data-testid="tb-drill-detail">
          <div className="page-head">
            <h2>
              {t("accountLedger")} · <span dir="ltr">{drill.account_code}</span> ·{" "}
              {drill.account_name_ar}
            </h2>
          </div>
          <p className="muted">
            {t("openingBalance")}: {money(drill.opening_balance_minor, lang)} · {range.start} →{" "}
            {range.end}
          </p>
          <table className="data-table" data-testid="tb-drill-table">
            <thead>
              <tr>
                <th>{t("date")}</th>
                <th>{t("entryNumber")}</th>
                <th>{t("description")}</th>
                <th className="numeric">{t("debit")}</th>
                <th className="numeric">{t("credit")}</th>
                <th className="numeric">{t("balance")}</th>
              </tr>
            </thead>
            <tbody>
              {drill.rows.map((row) => (
                <tr key={row.entry_id}>
                  <td>{row.entry_date}</td>
                  <td dir="ltr">
                    <button
                      type="button"
                      className="link-button"
                      data-testid="tb-entry-link"
                      title={t("openJournalEntry")}
                      onClick={() => onOpenEntry(row.entry_id)}
                    >
                      {row.entry_number}
                    </button>
                  </td>
                  <td>{row.description ?? "—"}</td>
                  <td className="numeric" dir="ltr">
                    {row.debit_minor === 0 ? "—" : money(row.debit_minor, lang)}
                  </td>
                  <td className="numeric" dir="ltr">
                    {row.credit_minor === 0 ? "—" : money(row.credit_minor, lang)}
                  </td>
                  <td className="numeric" dir="ltr">{money(row.balance_minor, lang)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted">
            {t("balance")}: {money(drill.closing_balance_minor, lang)}
          </p>
        </div>
      ) : null}
    </section>
  );
}