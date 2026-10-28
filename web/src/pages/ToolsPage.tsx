import { Fragment, useCallback, useEffect, useMemo, useState } from "react";

import {
  ApiError,
  closeFiscalYear,
  confirmMfaSetup,
  createAccount,
  createTaxCode,
  closePeriod,
  disableMfa,
  downloadReportPath,
  importMasterData,
  importOpeningBalances,
  listAccounts,
  listAuthSessions,
  listPostingMappings,
  me,
  regenerateRecoveryCodes,
  revokeAuthSession,
  startMfaSetup,
  updatePostingMapping,
  updateTenantMfaPolicy,
  listAuditEvents,
  listFiscalYears,
  listPeriods,
  listTaxCodes,
  reopenPeriod,
  setTaxRate as updateTaxRate,
  updateAccount,
} from "../api";
import type {
  Account,
  AccountingPeriod,
  AuditEvent,
  AuthSessionInfo,
  FiscalYear,
  MfaSetup,
  OpeningBalanceReport,
  PostingMappingRow,
  TaxCode,
  TenantMfaPolicy,
} from "../api";
import { translations } from "../i18n";
import type { Lang } from "../i18n";
import { anyText, errorText, money, today } from "./utils";

type Tab = "imports" | "chart" | "audit" | "close" | "security" | "settings";

interface ToolsPageProps {
  lang: Lang;
  company: { id: string; base_currency: string; permissions: string[] };
}

interface ImportCardProps {
  kind: "customers" | "suppliers" | "items";
  label: string;
  hint: string;
  lang: Lang;
  disabled: boolean;
}

function ImportCard({ kind, label, hint, lang, disabled }: ImportCardProps) {
  const t = (key: keyof (typeof translations)["ar"]): string => translations[lang][key];
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  async function run(): Promise<void> {
    if (file === null) {
      return;
    }
    setBusy(true);
    setMessage(null);
    setFailed(null);
    try {
      const result = await importMasterData(kind, file);
      setMessage(t("createdRecords") + ": " + String(result.created));
    } catch (caught) {
      setFailed(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card import-card">
      <h3>{label}</h3>
      <p className="muted">{hint}</p>
      <input
        type="file"
        accept=".csv,.xlsx,.xlsm,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        aria-label={label}
        onChange={(event) => setFile(event.target.files?.[0] ?? null)}
      />
      <div className="form-actions">
        <div className="spacer" />
        <button
          type="button"
          className="button primary"
          disabled={disabled || file === null || busy}
          onClick={() => void run()}
        >
          {busy ? t("saving") : t("runImport")}
        </button>
      </div>
      {message !== null ? <div className="alert success" role="status">{message}</div> : null}
      {failed !== null ? <div className="alert error" role="alert">{failed}</div> : null}
    </div>
  );
}

export function ToolsPage({ lang, company }: ToolsPageProps) {
  const t = (key: keyof (typeof translations)["ar"]): string => translations[lang][key];
  const [tab, setTab] = useState<Tab>("imports");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [periods, setPeriods] = useState<AccountingPeriod[]>([]);
  const [fiscalYears, setFiscalYears] = useState<FiscalYear[]>([]);
  const [closeReason, setCloseReason] = useState(t("periodClose"));

  const [periodId, setPeriodId] = useState("");
  const [entryDate, setEntryDate] = useState(today);
  const [openingFile, setOpeningFile] = useState<File | null>(null);
  const [openingReport, setOpeningReport] = useState<OpeningBalanceReport | null>(null);
  const [openingBusy, setOpeningBusy] = useState(false);
  const [openingMessage, setOpeningMessage] = useState<string | null>(null);

  const [accounts, setAccounts] = useState<Account[]>([]);
  const [accountBusy, setAccountBusy] = useState(false);
  const [accountCode, setAccountCode] = useState("");
  const [accountNameAr, setAccountNameAr] = useState("");
  const [accountNameEn, setAccountNameEn] = useState("");
  const [accountType, setAccountType] = useState<Account["account_type"]>("asset");

  const [mappings, setMappings] = useState<PostingMappingRow[]>([]);
  const [mappingDate, setMappingDate] = useState(today);
  const [mappingChoice, setMappingChoice] = useState<Record<string, string>>({});
  const [mappingBusy, setMappingBusy] = useState<string | null>(null);
  const [taxCodes, setTaxCodes] = useState<TaxCode[]>([]);
  const [taxCode, setTaxCode] = useState("");
  const [taxNameAr, setTaxNameAr] = useState("");
  const [taxRate, setTaxRate] = useState("");
  const [taxKind, setTaxKind] = useState<TaxCode["kind"]>("sales");
  const [taxJurisdiction, setTaxJurisdiction] = useState("");
  const [taxBusy, setTaxBusy] = useState(false);
  const [rateCodeId, setRateCodeId] = useState<string | null>(null);
  const [newRate, setNewRate] = useState("");
  const [newRateFrom, setNewRateFrom] = useState(today);
  const [rateBusy, setRateBusy] = useState(false);

  const [sessions, setSessions] = useState<AuthSessionInfo[]>([]);
  const [mfaEnabled, setMfaEnabled] = useState(false);
  const [mfaSetup, setMfaSetup] = useState<MfaSetup | null>(null);
  const [mfaCode, setMfaCode] = useState("");
  const [mfaPassword, setMfaPassword] = useState("");
  const [mfaBusy, setMfaBusy] = useState(false);
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([]);
  const [recoveryPassword, setRecoveryPassword] = useState("");
  const [recoveryCode, setRecoveryCode] = useState("");
  const [recoveryBusy, setRecoveryBusy] = useState(false);
  const [mfaPolicy, setMfaPolicy] = useState<TenantMfaPolicy>({
    mfa_sensitive_required: true,
    mfa_posting_required: true,
  });
  const [policyBusy, setPolicyBusy] = useState(false);

  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditAction, setAuditAction] = useState("");
  const [auditOffset, setAuditOffset] = useState(0);
  const [auditTotal, setAuditTotal] = useState(0);

  const canSettings = company.permissions.includes("settings.manage");
  const canInventory = company.permissions.includes("inventory.manage");
  const canJournal = company.permissions.includes("ledger.journal.create");
  const canClose = company.permissions.includes("ledger.period.close");
  const canAudit = company.permissions.includes("audit.view");
  const canMfaPolicy = company.permissions.includes("admin.role.manage");

  const openPeriods = useMemo(() => periods.filter((period) => period.status === "open"), [periods]);

  const reloadLedger = useCallback(async (signal?: AbortSignal) => {
    try {
      const [periodRows, yearRows] = await Promise.all([
        listPeriods(signal),
        listFiscalYears(signal),
      ]);
      setPeriods(periodRows);
      setFiscalYears(yearRows);
      const firstOpen = periodRows.find((period) => period.status === "open") ?? periodRows[0];
      setPeriodId((current) => current || firstOpen?.id || "");
      setEntryDate((current) => (current === today() ? firstOpen?.start_date ?? current : current));
      setError(null);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status !== 401) {
        setError(errorText(caught, lang));
      }
    }
  }, []);

  const loadAccounts = useCallback(async (signal?: AbortSignal) => {
    try {
      const rows = await listAccounts(signal);
      setAccounts(rows);
      setError(null);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status !== 401) {
        setError(errorText(caught, lang));
      }
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void reloadLedger(controller.signal);
    return () => controller.abort();
  }, [reloadLedger]);

  useEffect(() => {
    if (tab !== "chart") {
      return undefined;
    }
    const controller = new AbortController();
    void loadAccounts(controller.signal);
    return () => controller.abort();
  }, [tab, loadAccounts]);

  async function addAccount(): Promise<void> {
    setError(null);
    setNotice(null);
    if (accountCode.trim() === "" || accountNameAr.trim() === "") {
      setError(t("requiredFields"));
      return;
    }
    setAccountBusy(true);
    try {
      await createAccount({
        code: accountCode.trim(),
        name_ar: accountNameAr.trim(),
        name_en: accountNameEn.trim() === "" ? null : accountNameEn.trim(),
        account_type: accountType,
        is_postable: true,
        is_active: true,
      });
      setNotice(t("accountSaved"));
      setAccountCode("");
      setAccountNameAr("");
      setAccountNameEn("");
      await loadAccounts();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setAccountBusy(false);
    }
  }

  async function toggleAccount(account: Account, field: "is_active" | "is_postable"): Promise<void> {
    setError(null);
    setNotice(null);
    try {
      await updateAccount(account.id, { [field]: !account[field] });
      setNotice(t("accountSaved"));
      await loadAccounts();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    }
  }

  const loadSecurity = useCallback(async (signal?: AbortSignal) => {
    try {
      const [rows, identity] = await Promise.all([listAuthSessions(), me(signal)]);
      setSessions(rows);
      setMfaEnabled(identity.user.mfa_enabled);
      setMfaPolicy(identity.tenant_mfa_policy);
      setError(null);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status !== 401) {
        setError(errorText(caught, lang));
      }
    }
  }, []);

  const loadMappings = useCallback(async (signal?: AbortSignal) => {
    try {
      const [rows, accountRows, taxRows] = await Promise.all([
        listPostingMappings(mappingDate, signal),
        listAccounts(signal),
        listTaxCodes(signal),
      ]);
      setMappings(rows);
      setAccounts(accountRows);
      setTaxCodes(taxRows);
      setMappingChoice((current) => {
        const next: Record<string, string> = {};
        for (const row of rows) {
          next[row.key] = current[row.key] ?? row.account_id ?? "";
        }
        return next;
      });
      setError(null);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status !== 401) {
        setError(errorText(caught, lang));
      }
    }
  }, [mappingDate]);

  const loadAudit = useCallback(
    async (offset: number, action: string, signal?: AbortSignal) => {
      setAuditLoading(true);
      try {
        const page = await listAuditEvents(
          { limit: 50, offset, action, entity_type: "" },
          signal,
        );
        setEvents(page.items);
        setAuditTotal(page.offset + page.items.length);
        setError(null);
      } catch (caught) {
        if (caught instanceof ApiError && caught.status !== 401) {
          setError(errorText(caught, lang));
        }
      } finally {
        setAuditLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (tab !== "audit") {
      return undefined;
    }
    const controller = new AbortController();
    void loadAudit(auditOffset, auditAction, controller.signal);
    return () => controller.abort();
  }, [tab, auditOffset, auditAction, loadAudit]);

  useEffect(() => {
    if (tab !== "security") {
      return undefined;
    }
    const controller = new AbortController();
    void loadSecurity(controller.signal);
    return () => controller.abort();
  }, [tab, loadSecurity]);

  useEffect(() => {
    if (tab !== "settings") {
      return undefined;
    }
    const controller = new AbortController();
    void loadMappings(controller.signal);
    return () => controller.abort();
  }, [tab, loadMappings]);

  async function addTaxCode(): Promise<void> {
    setError(null);
    setNotice(null);
    const rate = Number(taxRate);
    if (taxCode.trim() === "" || taxNameAr.trim() === "" || !Number.isFinite(rate)) {
      setError(t("requiredFields"));
      return;
    }
    if (rate < 0 || rate > 100) {
      setError(t("taxRateRange"));
      return;
    }
    setTaxBusy(true);
    try {
      await createTaxCode({
        code: taxCode.trim(),
        name_ar: taxNameAr.trim(),
        rate_basis_points: Math.round(rate * 100),
        kind: taxKind,
        jurisdiction: taxJurisdiction.trim() || undefined,
      });
      setNotice(t("taxCodeSaved"));
      setTaxCode("");
      setTaxNameAr("");
      setTaxRate("");
      setTaxJurisdiction("");
      await loadMappings();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setTaxBusy(false);
    }
  }

  async function saveTaxRate(code: TaxCode): Promise<void> {
    setError(null);
    setNotice(null);
    const rate = Number(newRate);
    if (!Number.isFinite(rate) || newRateFrom === "") {
      setError(t("requiredFields"));
      return;
    }
    if (rate < 0 || rate > 100) {
      setError(t("taxRateRange"));
      return;
    }
    setRateBusy(true);
    try {
      await updateTaxRate(code.id, Math.round(rate * 100), newRateFrom);
      setNotice(t("rateSaved"));
      setRateCodeId(null);
      setNewRate("");
      await loadMappings();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setRateBusy(false);
    }
  }

  async function saveMapping(row: PostingMappingRow): Promise<void> {
    const accountId = mappingChoice[row.key] ?? "";
    if (accountId === "") {
      setError(t("requiredFields"));
      return;
    }
    setError(null);
    setNotice(null);
    setMappingBusy(row.key);
    try {
      setMappings(await updatePostingMapping(row.key, accountId, mappingDate));
      setNotice(t("mappingSaved"));
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setMappingBusy(null);
    }
  }

  async function beginMfaSetup(): Promise<void> {
    setError(null);
    setNotice(null);
    setMfaBusy(true);
    try {
      setMfaSetup(await startMfaSetup());
      setMfaCode("");
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setMfaBusy(false);
    }
  }

  async function confirmMfa(): Promise<void> {
    setError(null);
    setNotice(null);
    setMfaBusy(true);
    try {
      const issued = await confirmMfaSetup(mfaCode.trim());
      setMfaSetup(null);
      setMfaCode("");
      setRecoveryCodes(issued.codes);
      setNotice(t("mfaEnabled"));
      await loadSecurity();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setMfaBusy(false);
    }
  }

  async function removeMfa(): Promise<void> {
    setError(null);
    setNotice(null);
    setMfaBusy(true);
    try {
      await disableMfa(mfaPassword, mfaCode.trim());
      setMfaPassword("");
      setMfaCode("");
      setRecoveryCodes([]);
      setNotice(t("mfaDisabled"));
      await loadSecurity();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setMfaBusy(false);
    }
  }

  async function rotateRecoveryCodes(): Promise<void> {
    setError(null);
    setNotice(null);
    setRecoveryBusy(true);
    try {
      const issued = await regenerateRecoveryCodes(recoveryPassword, recoveryCode.trim());
      setRecoveryCodes(issued.codes);
      setRecoveryPassword("");
      setRecoveryCode("");
      setNotice(t("recoveryCodesRegenerated"));
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setRecoveryBusy(false);
    }
  }

  async function saveMfaPolicy(): Promise<void> {
    setError(null);
    setNotice(null);
    setPolicyBusy(true);
    try {
      setMfaPolicy(await updateTenantMfaPolicy(mfaPolicy));
      setNotice(t("mfaPolicySaved"));
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setPolicyBusy(false);
    }
  }

  async function revokeSession(id: string): Promise<void> {
    setError(null);
    setNotice(null);
    try {
      await revokeAuthSession(id);
      await loadSecurity();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    }
  }

  function chooseTab(next: Tab): void {
    setTab(next);
    setError(null);
    setNotice(null);
    setAuditOffset(0);
  }

  async function runOpening(dryRun: boolean): Promise<void> {
    setError(null);
    setNotice(null);
    setOpeningMessage(null);
    if (openingFile === null || periodId === "") {
      setError(t("requiredFields"));
      return;
    }
    setOpeningBusy(true);
    try {
      const report = await importOpeningBalances(openingFile, {
        period_id: periodId,
        entry_date: entryDate,
        post: true,
        dry_run: dryRun,
      });
      setOpeningReport(report);
      setOpeningMessage(
        report.entry_number === null
          ? t("importReport")
          : t("imported") + ": " + report.entry_number,
      );
      await reloadLedger();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setOpeningBusy(false);
    }
  }

  async function periodAction(period: AccountingPeriod): Promise<void> {
    setError(null);
    setNotice(null);
    if (closeReason.trim() === "") {
      setError(t("requiredFields"));
      return;
    }
    try {
      if (period.status === "open") {
        await closePeriod(period.id, closeReason.trim());
      } else {
        await reopenPeriod(period.id, closeReason.trim());
      }
      setNotice(t("operationSaved"));
      await reloadLedger();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    }
  }

  async function closeYear(fiscalYear: FiscalYear): Promise<void> {
    setError(null);
    setNotice(null);
    if (closeReason.trim() === "") {
      setError(t("requiredFields"));
      return;
    }
    try {
      const result = await closeFiscalYear(fiscalYear.id, closeReason.trim());
      setNotice(
        t("closeYear") +
          ": " +
          String(result.periods_closed) +
          (result.closing_entry === null ? "" : " · " + result.closing_entry.number),
      );
      await reloadLedger();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    }
  }

  async function downloadAudit(): Promise<void> {
    setError(null);
    try {
      const query = new URLSearchParams({ limit: "10000" });
      if (auditAction.trim() !== "") {
        query.set("action", auditAction.trim());
      }
      const blob = await downloadReportPath(`/audit/events.csv?${query.toString()}`);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "audit-trail.csv";
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    }
  }

  function accountTypeLabel(type: Account["account_type"]): string {
    switch (type) {
      case "asset":
        return t("assetAccount");
      case "liability":
        return t("liabilityAccount");
      case "equity":
        return t("equityAccount");
      case "revenue":
        return t("revenueAccount");
      default:
        return t("expenseAccount");
    }
  }

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h1>{t("tools")}</h1>
          <p className="muted">{t("company")} · {company.base_currency}</p>
        </div>
        <div className="tabs" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "imports"}
            className={`tab ${tab === "imports" ? "active" : ""}`}
            onClick={() => chooseTab("imports")}
          >
            {t("dataImport")}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "chart"}
            className={`tab ${tab === "chart" ? "active" : ""}`}
            onClick={() => chooseTab("chart")}
          >
            {t("chartOfAccounts")}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "audit"}
            className={`tab ${tab === "audit" ? "active" : ""}`}
            onClick={() => chooseTab("audit")}
          >
            {t("auditLog")}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "close"}
            className={`tab ${tab === "close" ? "active" : ""}`}
            onClick={() => chooseTab("close")}
          >
            {t("periodClose")}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "security"}
            className={`tab ${tab === "security" ? "active" : ""}`}
            onClick={() => chooseTab("security")}
          >
            {t("security")}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "settings"}
            className={`tab ${tab === "settings" ? "active" : ""}`}
            onClick={() => chooseTab("settings")}
          >
            {t("postingSettings")}
          </button>
        </div>
      </header>

      {error !== null ? <div className="alert error" role="alert">{error}</div> : null}
      {notice !== null ? <div className="alert success" role="status">{notice}</div> : null}

      {tab === "imports" ? (
        <div className="tools-grid">
          <ImportCard
            kind="customers"
            label={t("customers")}
            hint={t("importMasterHint")}
            lang={lang}
            disabled={!canSettings}
          />
          <ImportCard
            kind="suppliers"
            label={t("suppliers")}
            hint={t("importMasterHint")}
            lang={lang}
            disabled={!canSettings}
          />
          <ImportCard
            kind="items"
            label={t("items")}
            hint={t("importMasterHint")}
            lang={lang}
            disabled={!canInventory}
          />

          <div className="card import-card wide">
            <h3>{t("openingBalances")}</h3>
            <p className="muted">{t("openingHint")}</p>
            <div className="form-grid">
              <label className="field">
                <span>{t("period")}</span>
                <select value={periodId} onChange={(event) => setPeriodId(event.target.value)}>
                  <option value="">{t("selectPeriod")}</option>
                  {periods.map((period) => (
                    <option key={period.id} value={period.id}>
                      {period.number} · {period.start_date} → {period.end_date}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>{t("documentDate")}</span>
                <input type="date" value={entryDate} onChange={(event) => setEntryDate(event.target.value)} />
              </label>
              <label className="field">
                <span>{t("chooseCsv")}</span>
                <input
                  type="file"
                  accept=".csv,.xlsx,.xlsm,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                  aria-label={t("openingBalances")}
                  onChange={(event) => setOpeningFile(event.target.files?.[0] ?? null)}
                />
              </label>
            </div>
            <div className="form-actions">
              <div className="spacer" />
              <button
                type="button"
                className="button ghost"
                disabled={!canJournal || openingBusy}
                onClick={() => void runOpening(true)}
              >
                {openingBusy ? t("saving") : t("dryRun")}
              </button>
              <button
                type="button"
                className="button primary"
                disabled={!canJournal || openingBusy}
                onClick={() => void runOpening(false)}
              >
                {openingBusy ? t("saving") : t("importAndPost")}
              </button>
            </div>
            {openingMessage !== null ? (
              <div className="alert success" role="status">{openingMessage}</div>
            ) : null}
            {openingReport !== null ? (
              <div className="table-card">
                <table className="data-table" data-testid="opening-balance-table">
                  <thead>
                    <tr>
                      <th>{t("number")}</th>
                      <th>{t("description")}</th>
                      <th className="numeric">{t("debit")}</th>
                      <th className="numeric">{t("credit")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {openingReport.lines.map((line) => (
                      <tr key={line.account_code}>
                        <td className="mono" dir="ltr">{line.account_code}</td>
                        <td>{anyText(lang, line.account_name_ar, line.account_name_en)}</td>
                        <td className="numeric" dir="ltr">
                          {line.debit_minor === 0 ? "—" : money(line.debit_minor, lang)}
                        </td>
                        <td className="numeric" dir="ltr">
                          {line.credit_minor === 0 ? "—" : money(line.credit_minor, lang)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
          </div>
        </div>
      ) : null}

      {tab === "chart" ? (
        <div className="card">
          <h3>{t("newAccount")}</h3>
          <div className="form-grid">
            <label className="field">
              <span>{t("accountCode")}</span>
              <input
                type="text"
                dir="ltr"
                value={accountCode}
                maxLength={20}
                onChange={(event) => setAccountCode(event.target.value)}
              />
            </label>
            <label className="field">
              <span>{t("accountName")}</span>
              <input
                type="text"
                value={accountNameAr}
                onChange={(event) => setAccountNameAr(event.target.value)}
              />
            </label>
            <label className="field">
              <span>{t("accountNameEn")}</span>
              <input
                type="text"
                dir="ltr"
                value={accountNameEn}
                onChange={(event) => setAccountNameEn(event.target.value)}
              />
            </label>
            <label className="field">
              <span>{t("accountType")}</span>
              <select
                value={accountType}
                onChange={(event) => setAccountType(event.target.value as Account["account_type"])}
              >
                <option value="asset">{t("assetAccount")}</option>
                <option value="liability">{t("liabilityAccount")}</option>
                <option value="equity">{t("equityAccount")}</option>
                <option value="revenue">{t("revenueAccount")}</option>
                <option value="expense">{t("expenseAccount")}</option>
              </select>
            </label>
          </div>
          <div className="form-actions">
            <div className="spacer" />
            <button
              type="button"
              className="button primary"
              disabled={!canSettings || accountBusy}
              onClick={() => void addAccount()}
            >
              {accountBusy ? t("saving") : t("createAccount")}
            </button>
          </div>

          {accounts.length === 0 ? (
            <div className="empty-state">{t("accountsEmpty")}</div>
          ) : (
            <div className="table-card">
              <table className="data-table" data-testid="account-table">
                <thead>
                  <tr>
                    <th>{t("accountCode")}</th>
                    <th>{t("accountName")}</th>
                    <th>{t("accountType")}</th>
                    <th>{t("isPostable")}</th>
                    <th>{t("isActive")}</th>
                  </tr>
                </thead>
                <tbody>
                  {accounts.map((account) => (
                    <tr key={account.id}>
                      <td className="mono" dir="ltr">{account.code}</td>
                      <td>
                        {anyText(lang, account.name_ar, account.name_en)}
                        {account.is_active ? null : (
                          <span className="badge closed"> {t("accountInactive")}</span>
                        )}
                      </td>
                      <td>{accountTypeLabel(account.account_type)}</td>
                      <td>
                        <button
                          type="button"
                          className="button ghost"
                          disabled={!canSettings}
                          onClick={() => void toggleAccount(account, "is_postable")}
                        >
                          {account.is_postable ? "✓" : "—"}
                        </button>
                      </td>
                      <td>
                        <button
                          type="button"
                          className="button ghost"
                          disabled={!canSettings}
                          onClick={() => void toggleAccount(account, "is_active")}
                        >
                          {account.is_active ? "✓" : "—"}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      ) : null}

      {tab === "audit" ? (
        <div className="card">
          <div className="filter-bar">
            <label className="field">
              <span>{t("auditAction")}</span>
              <input value={auditAction} onChange={(event) => setAuditAction(event.target.value)} />
            </label>
            <button type="button" className="button ghost" onClick={() => void loadAudit(0, auditAction)}>
              {t("search")}
            </button>
            <div className="spacer" />
            <button type="button" className="button primary" disabled={!canAudit} onClick={() => void downloadAudit()}>
              {t("downloadCsv")}
            </button>
          </div>
          {auditLoading ? (
            <div className="empty-state">{t("loading")}</div>
          ) : events.length === 0 ? (
            <div className="empty-state">{t("noData")}</div>
          ) : (
            <table className="data-table" data-testid="audit-table">
              <thead>
                <tr>
                  <th>{t("auditTime")}</th>
                  <th>{t("auditAction")}</th>
                  <th>{t("auditEntity")}</th>
                  <th>{t("auditActor")}</th>
                </tr>
              </thead>
              <tbody>
                {events.map((event) => (
                  <tr key={event.id}>
                    <td className="mono" dir="ltr">{event.created_at.slice(0, 19).replace("T", " ")}</td>
                    <td className="mono" dir="ltr">{event.action}</td>
                    <td className="mono" dir="ltr">
                      {event.entity_type ?? "—"}
                      {event.entity_id === null ? "" : " · " + event.entity_id.slice(0, 8)}
                    </td>
                    <td className="mono" dir="ltr">
                      {event.actor_user_id === null ? "—" : event.actor_user_id.slice(0, 8)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <div className="form-actions">
            <button
              type="button"
              className="button ghost"
              disabled={auditOffset === 0}
              onClick={() => setAuditOffset(Math.max(0, auditOffset - 50))}
            >
              ‹
            </button>
            <span className="muted">{auditOffset + 1} – {auditTotal}</span>
            <button
              type="button"
              className="button ghost"
              disabled={auditTotal - auditOffset < 50}
              onClick={() => setAuditOffset(auditOffset + 50)}
            >
              ›
            </button>
          </div>
        </div>
      ) : null}

      {tab === "close" ? (
        <div className="card">
          <p className="muted">{t("closeHint")}</p>
          <label className="field wide">
            <span>{t("reason")}</span>
            <input value={closeReason} onChange={(event) => setCloseReason(event.target.value)} />
          </label>
          {fiscalYears.map((year) => (
            <div key={year.id} className="fiscal-year-block">
              <div className="filter-bar">
                <h3>{t("fiscalYear")} {year.code} · {year.start_date} → {year.end_date}</h3>
                <span className={`badge ${year.status}`}>
                  {year.status === "open" ? t("openStatus") : t("closedStatus")}
                </span>
                <div className="spacer" />
                {year.status === "open" && canClose ? (
                  <button type="button" className="button primary" onClick={() => void closeYear(year)}>
                    {t("closeYear")}
                  </button>
                ) : null}
              </div>
              {year.periods.length === 0 ? (
                <p className="muted">{t("noData")}</p>
              ) : (
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>{t("period")}</th>
                      <th>{t("documentDate")}</th>
                      <th>{t("status")}</th>
                      <th>{t("actions")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {year.periods.map((period) => (
                      <tr key={period.id}>
                        <td className="mono" dir="ltr">{period.number}</td>
                        <td className="mono" dir="ltr">{period.start_date} → {period.end_date}</td>
                        <td>
                          <span className={`badge ${period.status}`}>
                            {period.status === "open" ? t("openStatus") : t("closedStatus")}
                          </span>
                        </td>
                        <td>
                          {canClose ? (
                            <button
                              type="button"
                              className="button ghost"
                              onClick={() => void periodAction(period)}
                            >
                              {period.status === "open" ? t("closePeriod") : t("reopenPeriod")}
                            </button>
                          ) : null}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          ))}
          {openPeriods.length === 0 && fiscalYears.length === 0 ? (
            <div className="empty-state">{t("noData")}</div>
          ) : null}
        </div>
      ) : null}

      {tab === "security" ? (
        <div className="tools-grid">
          <div className="card">
            <h3>{t("twoFactor")}</h3>
            {mfaEnabled ? (
              <>
                <p className="muted">{t("twoFactor")}</p>
                <div className="form-grid">
                  <label className="field">
                    <span>{t("password")}</span>
                    <input
                      type="password"
                      dir="ltr"
                      value={mfaPassword}
                      onChange={(event) => setMfaPassword(event.target.value)}
                    />
                  </label>
                  <label className="field">
                    <span>{t("mfaCode")}</span>
                    <input
                      type="text"
                      dir="ltr"
                      inputMode="numeric"
                      maxLength={6}
                      value={mfaCode}
                      onChange={(event) => setMfaCode(event.target.value)}
                    />
                  </label>
                </div>
                <div className="form-actions">
                  <div className="spacer" />
                  <button
                    type="button"
                    className="button"
                    disabled={mfaBusy || mfaPassword === "" || mfaCode.trim().length !== 6}
                    onClick={() => void removeMfa()}
                  >
                    {t("disableTwoFactor")}
                  </button>
                </div>
              </>
            ) : mfaSetup === null ? (
              <>
                <p className="muted">{t("mfaSecretHint")}</p>
                <div className="form-actions">
                  <div className="spacer" />
                  <button
                    type="button"
                    className="button primary"
                    disabled={mfaBusy}
                    onClick={() => void beginMfaSetup()}
                  >
                    {t("enableTwoFactor")}
                  </button>
                </div>
              </>
            ) : (
              <>
                <p className="muted">{t("mfaSecretHint")}</p>
                <label className="field wide">
                  <span>{t("twoFactor")}</span>
                  <input className="mono" dir="ltr" readOnly value={mfaSetup.secret} data-testid="mfa-secret" />
                </label>
                <p className="muted mono" dir="ltr">{mfaSetup.provisioning_uri}</p>
                <div className="form-grid">
                  <label className="field">
                    <span>{t("mfaCode")}</span>
                    <input
                      type="text"
                      dir="ltr"
                      inputMode="numeric"
                      maxLength={6}
                      value={mfaCode}
                      onChange={(event) => setMfaCode(event.target.value)}
                    />
                  </label>
                </div>
                <div className="form-actions">
                  <div className="spacer" />
                  <button
                    type="button"
                    className="button ghost"
                    onClick={() => {
                      setMfaSetup(null);
                      setMfaCode("");
                    }}
                  >
                    {t("cancel")}
                  </button>
                  <button
                    type="button"
                    className="button primary"
                    disabled={mfaBusy || mfaCode.trim().length !== 6}
                    onClick={() => void confirmMfa()}
                  >
                    {t("confirmCode")}
                  </button>
                </div>
              </>
            )}
          </div>

          {mfaEnabled || recoveryCodes.length > 0 ? (
            <div className="card wide" data-testid="recovery-codes-card">
              <h3>{t("recoveryCodes")}</h3>
              {recoveryCodes.length > 0 ? (
                <>
                  <p className="muted">{t("recoveryCodesHint")}</p>
                  <ol className="mono recovery-list" dir="ltr" data-testid="recovery-code-list">
                    {recoveryCodes.map((code) => (
                      <li key={code}>{code}</li>
                    ))}
                  </ol>
                  <div className="form-actions">
                    <div className="spacer" />
                    <button
                      type="button"
                      className="button ghost"
                      data-testid="dismiss-recovery-codes"
                      onClick={() => setRecoveryCodes([])}
                    >
                      {t("done")}
                    </button>
                  </div>
                </>
              ) : (
                <p className="muted">{t("recoveryCodesEmptyHint")}</p>
              )}
              {mfaEnabled ? (
                <>
                  <p className="muted">{t("regenerateCodesHint")}</p>
                  <div className="form-grid">
                    <label className="field">
                      <span>{t("password")}</span>
                      <input
                        type="password"
                        dir="ltr"
                        autoComplete="current-password"
                        value={recoveryPassword}
                        onChange={(event) => setRecoveryPassword(event.target.value)}
                      />
                    </label>
                    <label className="field">
                      <span>{t("mfaCode")}</span>
                      <input
                        type="text"
                        dir="ltr"
                        inputMode="numeric"
                        maxLength={6}
                        value={recoveryCode}
                        onChange={(event) => setRecoveryCode(event.target.value)}
                      />
                    </label>
                  </div>
                  <div className="form-actions">
                    <div className="spacer" />
                    <button
                      type="button"
                      className="button"
                      data-testid="regenerate-recovery-codes"
                      disabled={
                        recoveryBusy ||
                        recoveryPassword === "" ||
                        recoveryCode.trim().length !== 6
                      }
                      onClick={() => void rotateRecoveryCodes()}
                    >
                      {t("regenerateCodes")}
                    </button>
                  </div>
                </>
              ) : null}
            </div>
          ) : null}

          <div className="card" data-testid="mfa-policy-card">
            <h3>{t("mfaPolicy")}</h3>
            <p className="muted">{t("mfaPolicyHint")}</p>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={mfaPolicy.mfa_sensitive_required}
                disabled={!canMfaPolicy}
                onChange={(event) =>
                  setMfaPolicy({ ...mfaPolicy, mfa_sensitive_required: event.target.checked })
                }
              />
              <span>{t("mfaSensitiveRequired")}</span>
            </label>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={mfaPolicy.mfa_posting_required}
                disabled={!canMfaPolicy}
                onChange={(event) =>
                  setMfaPolicy({ ...mfaPolicy, mfa_posting_required: event.target.checked })
                }
              />
              <span>{t("mfaPostingRequired")}</span>
            </label>
            {canMfaPolicy ? (
              <div className="form-actions">
                <div className="spacer" />
                <button
                  type="button"
                  className="button primary"
                  data-testid="save-mfa-policy"
                  disabled={policyBusy}
                  onClick={() => void saveMfaPolicy()}
                >
                  {policyBusy ? t("saving") : t("saveMapping")}
                </button>
              </div>
            ) : null}
          </div>

          <div className="card wide">
            <h3>{t("sessions")}</h3>
            {sessions.length === 0 ? (
              <p className="muted">{t("noSessions")}</p>
            ) : (
              <table className="data-table" data-testid="session-table">
                <thead>
                  <tr>
                    <th>{t("date")}</th>
                    <th>{t("lastUsed")}</th>
                    <th>{t("expires")}</th>
                    <th>{t("status")}</th>
                    <th>{t("actions")}</th>
                  </tr>
                </thead>
                <tbody>
                  {sessions.map((row) => (
                    <tr key={row.id}>
                      <td className="mono" dir="ltr">{row.created_at.slice(0, 19).replace("T", " ")}</td>
                      <td className="mono" dir="ltr">
                        {row.last_used_at === null ? "—" : row.last_used_at.slice(0, 19).replace("T", " ")}
                      </td>
                      <td className="mono" dir="ltr">{row.expires_at.slice(0, 19).replace("T", " ")}</td>
                      <td>{row.revoked_at === null ? t("active") : t("revoked")}</td>
                      <td>
                        {row.revoked_at === null ? (
                          <button
                            type="button"
                            className="button ghost"
                            onClick={() => void revokeSession(row.id)}
                          >
                            {t("revoke")}
                          </button>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      ) : null}

      {tab === "settings" ? (
        <div className="card wide">
          <h3>{t("postingSettings")}</h3>
          <p className="muted">{t("postingSettingsHint")}</p>
          <label className="field">
            <span>{t("effectiveFrom")}</span>
            <input
              type="date"
              value={mappingDate}
              onChange={(event) => setMappingDate(event.target.value)}
            />
          </label>
          <table className="data-table" data-testid="posting-mapping-table">
            <thead>
              <tr>
                <th>{t("postingRole")}</th>
                <th>{t("accountType")}</th>
                <th>{t("account")}</th>
                <th>{t("mappedAccount")}</th>
                <th>{t("actions")}</th>
              </tr>
            </thead>
            <tbody>
              {mappings.map((row) => (
                <tr key={row.key}>
                  <td>{lang === "ar" ? row.label_ar : row.label_en}</td>
                  <td>{accountTypeLabel(row.account_type)}</td>
                  <td className="mono" dir="ltr">
                    {row.account_code === null
                      ? "—"
                      : `${row.account_code} · ${row.account_name_ar ?? ""}`}
                  </td>
                  <td>
                    <select
                      aria-label={row.label_ar}
                      value={mappingChoice[row.key] ?? ""}
                      onChange={(event) =>
                        setMappingChoice({
                          ...mappingChoice,
                          [row.key]: event.target.value,
                        })
                      }
                    >
                      <option value="">{t("selectAccount")}</option>
                      {accounts
                        .filter(
                          (account) =>
                            account.account_type === row.account_type &&
                            account.is_postable &&
                            account.is_active,
                        )
                        .map((account) => (
                          <option key={account.id} value={account.id}>
                            {account.code} · {account.name_ar}
                          </option>
                        ))}
                    </select>
                  </td>
                  <td>
                    <button
                      type="button"
                      className="button ghost"
                      disabled={
                        mappingBusy !== null ||
                        (mappingChoice[row.key] ?? "") === "" ||
                        mappingChoice[row.key] === row.account_id
                      }
                      onClick={() => void saveMapping(row)}
                    >
                      {mappingBusy === row.key ? t("saving") : t("saveMapping")}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <h3>{t("taxCodes")}</h3>
          <p className="muted">{t("taxCodesHint")}</p>
          <table className="data-table" data-testid="tax-code-table">
            <thead>
              <tr>
                <th>{t("taxCodeShort")}</th>
                <th>{t("taxName")}</th>
                <th>{t("taxKind")}</th>
                <th>{t("jurisdiction")}</th>
                <th className="numeric">{t("ratePercent")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {taxCodes.map((code) => (
                <Fragment key={code.id}>
                  <tr>
                    <td className="mono" dir="ltr">{code.code}</td>
                    <td>{code.name_ar}</td>
                    <td>
                      {code.kind === "withholding"
                        ? t("withholdTax")
                        : code.kind === "purchase"
                          ? t("kindPurchase")
                          : t("kindSales")}
                    </td>
                    <td className="mono" dir="ltr">{code.jurisdiction ?? "—"}</td>
                    <td className="numeric" dir="ltr">
                      {(code.rate_basis_points / 100).toFixed(2)}%
                    </td>
                    <td>
                      <button
                        type="button"
                        className="button"
                        data-testid={`change-rate-${code.code}`}
                        onClick={() => {
                          setRateCodeId(rateCodeId === code.id ? null : code.id);
                          setNewRate((code.rate_basis_points / 100).toFixed(2));
                          setNewRateFrom(today);
                        }}
                      >
                        {t("changeRate")}
                      </button>
                    </td>
                  </tr>
                  {rateCodeId === code.id ? (
                    <tr data-testid={`rate-editor-${code.code}`}>
                      <td colSpan={6}>
                        <div className="form-grid">
                          <label className="field">
                            <span>{t("newRate")}</span>
                            <input
                              type="number"
                              min="0"
                              max="100"
                              step="0.01"
                              dir="ltr"
                              data-testid="new-rate-value"
                              value={newRate}
                              onChange={(event) => setNewRate(event.target.value)}
                            />
                          </label>
                          <label className="field">
                            <span>{t("effectiveFrom")}</span>
                            <input
                              type="date"
                              dir="ltr"
                              data-testid="new-rate-from"
                              value={newRateFrom}
                              onChange={(event) => setNewRateFrom(event.target.value)}
                            />
                          </label>
                        </div>
                        <p className="muted">{t("rateHistoryHint")}</p>
                        <div className="form-actions">
                          <div className="spacer" />
                          <button
                            type="button"
                            className="button primary"
                            data-testid="save-rate"
                            disabled={rateBusy}
                            onClick={() => void saveTaxRate(code)}
                          >
                            {rateBusy ? t("saving") : t("saveMapping")}
                          </button>
                        </div>
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              ))}
            </tbody>
          </table>
          <div className="form-grid">
            <label className="field">
              <span>{t("taxCodeShort")}</span>
              <input value={taxCode} onChange={(event) => setTaxCode(event.target.value)} />
            </label>
            <label className="field">
              <span>{t("taxName")}</span>
              <input value={taxNameAr} onChange={(event) => setTaxNameAr(event.target.value)} />
            </label>
            <label className="field">
              <span>{t("taxKind")}</span>
              <select
                value={taxKind}
                onChange={(event) => setTaxKind(event.target.value as TaxCode["kind"])}
              >
                <option value="sales">{t("kindSales")}</option>
                <option value="purchase">{t("kindPurchase")}</option>
                <option value="withholding">{t("withholdTax")}</option>
              </select>
            </label>
            <label className="field">
              <span>{t("jurisdiction")}</span>
              <input
                dir="ltr"
                maxLength={50}
                placeholder="IQ"
                value={taxJurisdiction}
                onChange={(event) => setTaxJurisdiction(event.target.value)}
              />
            </label>
            <label className="field">
              <span>{t("ratePercent")}</span>
              <input
                type="number"
                min="0"
                max="100"
                step="0.01"
                dir="ltr"
                value={taxRate}
                onChange={(event) => setTaxRate(event.target.value)}
              />
            </label>
          </div>
          <div className="form-actions">
            <div className="spacer" />
            <button
              type="button"
              className="button primary"
              disabled={taxBusy}
              onClick={() => void addTaxCode()}
            >
              {taxBusy ? t("saving") : t("addTaxCode")}
            </button>
          </div>
        </div>
      ) : null}
    </section>
  );
}
