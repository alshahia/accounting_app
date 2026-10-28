import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  createJournalEntry,
  deleteJournalEntry,
  getJournalEntry,
  listAccounts,
  listJournalEntries,
  listPeriods,
  postJournalEntry,
  reverseJournalEntry,
  updateJournalEntry,
} from "../api";
import type { Account, AccountingPeriod, JournalEntry, JournalEntrySummary } from "../api";
import { translations } from "../i18n";
import { ListError } from "../components/ListError";
import type { Lang } from "../i18n";
import { errorText, money, today } from "./utils";

interface JournalPageProps {
  lang: Lang;
  company: { id: string; base_currency: string; permissions: string[] };
  initialEntryId?: string | null;
}

interface FormLine {
  account_id: string;
  debit: string;
  credit: string;
  description: string;
}

function emptyLine(): FormLine {
  return { account_id: "", debit: "", credit: "", description: "" };
}

function toMinor(value: string): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.round(parsed * 100) : 0;
}

export function JournalPage({ lang, company, initialEntryId }: JournalPageProps) {
  const t = (key: keyof (typeof translations)["ar"]): string => translations[lang][key];
  const [entries, setEntries] = useState<JournalEntrySummary[]>([]);
  const [periods, setPeriods] = useState<AccountingPeriod[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [periodId, setPeriodId] = useState("");
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<JournalEntry | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [formPeriod, setFormPeriod] = useState("");
  const [formDate, setFormDate] = useState(today);
  const [formMemo, setFormMemo] = useState("");
  const [formPost, setFormPost] = useState(false);
  const [formLines, setFormLines] = useState<FormLine[]>([emptyLine(), emptyLine()]);
  const [saving, setSaving] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [reverseMemo, setReverseMemo] = useState("");
  const [reversing, setReversing] = useState(false);

  const canCreate = company.permissions.includes("ledger.journal.create");
  const canPost = company.permissions.includes("ledger.journal.post");

  useEffect(() => {
    const controller = new AbortController();
    listPeriods(controller.signal)
      .then(setPeriods)
      .catch(() => undefined);
    listAccounts(controller.signal)
      .then(setAccounts)
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  const accountLabels = new Map(
    accounts.map((account) => [
      account.id,
      account.code + " · " + (lang === "ar" ? account.name_ar : account.name_en ?? account.name_ar),
    ]),
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const rows = await listJournalEntries({
        periodId: periodId === "" ? undefined : periodId,
        status: status === "" ? undefined : status,
        limit: 100,
      });
      setEntries(rows);
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setLoading(false);
    }
  }, [periodId, status, t]);

  useEffect(() => {
    void load();
  }, [load, company.id]);

  const openPeriods = periods.filter((period) => period.status === "open");

  useEffect(() => {
    if (formPeriod === "" && openPeriods.length > 0) {
      setFormPeriod(openPeriods[0].id);
      setFormDate(openPeriods[0].start_date);
    }
  }, [formPeriod, openPeriods]);

  function updateLine(index: number, patch: Partial<FormLine>): void {
    setFormLines((current) =>
      current.map((line, position) => (position === index ? { ...line, ...patch } : line)),
    );
  }

  async function reverseEntry(): Promise<void> {
    if (detail === null) {
      return;
    }
    setError(null);
    setNotice(null);
    setReversing(true);
    try {
      const reversal = await reverseJournalEntry(
        detail.id,
        reverseMemo.trim() === "" ? null : reverseMemo.trim(),
      );
      setNotice(t("entryReversed") + ": " + reversal.number);
      setReverseMemo("");
      setDetail(reversal);
      await load();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setReversing(false);
    }
  }

  async function submitEntry(): Promise<void> {
    setError(null);
    setNotice(null);
    if (formPeriod === "" || formDate === "") {
      setError(t("requiredFields"));
      return;
    }
    const lines = formLines
      .map((line) => ({
        account_id: line.account_id,
        debit_minor: toMinor(line.debit),
        credit_minor: toMinor(line.credit),
        description: line.description.trim() === "" ? null : line.description.trim(),
      }))
      .filter((line) => line.account_id !== "" && (line.debit_minor > 0 || line.credit_minor > 0));
    if (lines.length < 2 || lines.some((line) => line.debit_minor < 0 || line.credit_minor < 0)) {
      setError(t("requiredFields"));
      return;
    }
    const debit = lines.reduce((sum, line) => sum + line.debit_minor, 0);
    const credit = lines.reduce((sum, line) => sum + line.credit_minor, 0);
    if (debit === 0 || debit !== credit) {
      setError(t("balanceMismatch"));
      return;
    }
    setSaving(true);
    try {
      const payload = {
        period_id: formPeriod,
        entry_date: formDate,
        memo: formMemo.trim() === "" ? null : formMemo.trim(),
        lines,
      };
      const saved =
        editingId === null
          ? await createJournalEntry({ ...payload, post: formPost && canPost })
          : await updateJournalEntry(editingId, payload);
      setNotice(editingId === null ? t("entrySaved") : t("entryUpdated"));
      setShowForm(false);
      setEditingId(null);
      setFormMemo("");
      setFormPost(false);
      setFormLines([emptyLine(), emptyLine()]);
      setDetail(saved);
      await load();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setSaving(false);
    }
  }

  function startEdit(entry: JournalEntry): void {
    setEditingId(entry.id);
    setFormPeriod(entry.period_id);
    setFormDate(entry.entry_date);
    setFormMemo(entry.memo ?? "");
    setFormPost(false);
    setFormLines(
      entry.lines.map((line) => ({
        account_id: line.account_id,
        debit: line.debit_minor === 0 ? "" : (line.debit_minor / 100).toFixed(2),
        credit: line.credit_minor === 0 ? "" : (line.credit_minor / 100).toFixed(2),
        description: line.description ?? "",
      })),
    );
    setShowForm(true);
    setError(null);
    setNotice(null);
  }

  async function postDraft(): Promise<void> {
    if (detail === null) {
      return;
    }
    setError(null);
    setNotice(null);
    setSaving(true);
    try {
      const posted = await postJournalEntry(detail.id);
      setDetail(posted);
      setNotice(t("entryPosted") + ": " + posted.number);
      await load();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setSaving(false);
    }
  }

  async function removeDraft(): Promise<void> {
    if (detail === null) {
      return;
    }
    if (!window.confirm(t("confirmDeleteEntry"))) {
      return;
    }
    setError(null);
    setNotice(null);
    setSaving(true);
    try {
      await deleteJournalEntry(detail.id);
      setNotice(t("entryDeleted"));
      setDetail(null);
      await load();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setSaving(false);
    }
  }

  async function openEntry(id: string): Promise<void> {
    setError(null);
    try {
      setDetail(await getJournalEntry(id));
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    }
  }


  useEffect(() => {
    if (initialEntryId !== undefined && initialEntryId !== null) {
      void openEntry(initialEntryId);
    }
    // The drill-down target is a one-shot intent from the report screen.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialEntryId]);

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h1>{t("journalEntries")}</h1>
          <p className="muted">{company.base_currency}</p>
        </div>
        {canCreate ? (
          <button
            type="button"
            className="button primary"
            onClick={() => setShowForm(!showForm)}
          >
            {showForm ? t("cancel") : t("newJournalEntry")}
          </button>
        ) : null}
      </header>

      {notice !== null ? <div className="alert success" role="status">{notice}</div> : null}

      {showForm ? (
        <div className="card">
          <div className="card-head">
            <h2>{editingId === null ? t("newJournalEntry") : t("editDraft")}</h2>
          </div>
          <div className="form-grid">
            <label className="field">
              <span>{t("period")}</span>
              <select value={formPeriod} onChange={(event) => setFormPeriod(event.target.value)}>
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
              <input type="date" value={formDate} onChange={(event) => setFormDate(event.target.value)} />
            </label>
            <label className="field wide">
              <span>{t("memo")}</span>
              <input value={formMemo} onChange={(event) => setFormMemo(event.target.value)} />
            </label>
          </div>
          <table className="data-table" data-testid="journal-form">
            <thead>
              <tr>
                <th>{t("account")}</th>
                <th>{t("description")}</th>
                <th className="numeric">{t("debitAmount")}</th>
                <th className="numeric">{t("creditAmount")}</th>
                <th>{t("actions")}</th>
              </tr>
            </thead>
            <tbody>
              {formLines.map((line, index) => (
                <tr key={String(index)}>
                  <td>
                    <select
                      aria-label={t("journalLine") + " " + String(index + 1)}
                      value={line.account_id}
                      onChange={(event) => updateLine(index, { account_id: event.target.value })}
                    >
                      <option value="">{t("selectAccount")}</option>
                      {accounts
                        .filter((account) => account.is_postable && account.is_active)
                        .map((account) => (
                          <option key={account.id} value={account.id}>
                            {account.code} · {account.name_ar}
                          </option>
                        ))}
                    </select>
                  </td>
                  <td>
                    <input
                      value={line.description}
                      onChange={(event) => updateLine(index, { description: event.target.value })}
                    />
                  </td>
                  <td>
                    <input
                      type="number"
                      min="0"
                      step="0.01"
                      dir="ltr"
                      value={line.debit}
                      onChange={(event) => updateLine(index, { debit: event.target.value })}
                    />
                  </td>
                  <td>
                    <input
                      type="number"
                      min="0"
                      step="0.01"
                      dir="ltr"
                      value={line.credit}
                      onChange={(event) => updateLine(index, { credit: event.target.value })}
                    />
                  </td>
                  <td>
                    <button
                      type="button"
                      className="button ghost"
                      disabled={formLines.length <= 2}
                      onClick={() => setFormLines(formLines.filter((_line, position) => position !== index))}
                    >
                      {t("removeLine")}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="form-actions">
            <button
              type="button"
              className="button ghost"
              onClick={() => setFormLines([...formLines, emptyLine()])}
            >
              {t("addLine")}
            </button>
            <div className="spacer" />
            {canPost ? (
              <label className="check-field">
                <input
                  type="checkbox"
                  checked={formPost}
                  onChange={(event) => setFormPost(event.target.checked)}
                />
                <span>{t("postImmediately")}</span>
              </label>
            ) : null}
            <button
              type="button"
              className="button primary"
              disabled={saving}
              onClick={() => void submitEntry()}
            >
              {saving ? t("saving") : editingId === null ? t("saveEntry") : t("saveChanges")}
            </button>
          </div>
        </div>
      ) : null}

      <form
        className="card filter-bar"
        onSubmit={(event) => {
          event.preventDefault();
          void load();
        }}
      >
        <label className="field">
          <span>{t("period")}</span>
          <select value={periodId} onChange={(event) => setPeriodId(event.target.value)}>
            <option value="">{t("all")}</option>
            {periods.map((period) => (
              <option key={period.id} value={period.id}>
                {period.number} · {period.start_date} → {period.end_date}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>{t("status")}</span>
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">{t("all")}</option>
            <option value="draft">{t("draftDocument")}</option>
            <option value="posted">{t("postedDocument")}</option>
          </select>
        </label>
        <div className="spacer" />
        <button type="submit" className="button primary" disabled={loading}>
          {loading ? t("loading") : t("show")}
        </button>
      </form>

      {error !== null ? <div className="alert error" role="alert">{error}</div> : null}

      <div className="card table-card">
        {error !== null && entries.length === 0 ? (
          <ListError message={t("loadFailed")} retryLabel={t("retry")} onRetry={() => void load()} />
        ) : entries.length === 0 ? (
          <div className="empty-state">{t("journalEmpty")}</div>
        ) : (
          <table className="data-table" data-testid="journal-table">
            <thead>
              <tr>
                <th>{t("entryNumber")}</th>
                <th>{t("documentDate")}</th>
                <th>{t("status")}</th>
                <th>{t("memo")}</th>
                <th>{t("sourceDocument")}</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr
                  key={entry.id}
                  className="clickable-row"
                  onClick={() => void openEntry(entry.id)}
                >
                  <td className="mono" dir="ltr">{entry.number}</td>
                  <td className="mono" dir="ltr">{entry.entry_date}</td>
                  <td>
                    <span className={`badge ${entry.status}`}>
                      {entry.status === "posted" ? t("postedDocument") : t("draftDocument")}
                    </span>
                  </td>
                  <td>{entry.memo ?? "—"}</td>
                  <td>{entry.source_type ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {detail !== null ? (
        <div className="card table-card" data-testid="journal-detail">
          <div className="card-head">
            <h2>{t("entryDetail")} · {detail.number}</h2>
            <button type="button" className="button ghost" onClick={() => setDetail(null)}>
              {t("cancel")}
            </button>
          </div>
          <p className="muted">
            {detail.entry_date} · {detail.memo ?? "—"}
          </p>
          {detail.status === "draft" ? (
            <div className="form-actions">
              <button
                type="button"
                className="button ghost"
                disabled={!canCreate || saving}
                data-testid="edit-draft"
                onClick={() => startEdit(detail)}
              >
                {t("editDraft")}
              </button>
              <div className="spacer" />
              <button
                type="button"
                className="button ghost"
                disabled={!canPost || saving}
                data-testid="delete-draft"
                onClick={() => void removeDraft()}
              >
                {t("deleteDraft")}
              </button>
              <button
                type="button"
                className="button primary"
                disabled={!canPost || saving}
                data-testid="post-draft"
                onClick={() => void postDraft()}
              >
                {saving ? t("saving") : t("postEntry")}
              </button>
            </div>
          ) : null}
          {canPost && detail.status === "posted" && detail.reverses_entry_id === null ? (
            <div className="form-actions">
              <label className="field wide">
                <span>{t("reverseReason")}</span>
                <input
                  value={reverseMemo}
                  onChange={(event) => setReverseMemo(event.target.value)}
                />
              </label>
              <button
                type="button"
                className="button ghost"
                disabled={reversing}
                onClick={() => void reverseEntry()}
              >
                {reversing ? t("saving") : t("reverseEntry")}
              </button>
            </div>
          ) : null}
          <table className="data-table">
            <thead>
              <tr>
                <th>{t("account")}</th>
                <th>{t("description")}</th>
                <th className="numeric">{t("debit")}</th>
                <th className="numeric">{t("credit")}</th>
              </tr>
            </thead>
            <tbody>
              {detail.lines.map((line) => (
                <tr key={line.id}>
                  <td>{accountLabels.get(line.account_id) ?? line.account_id.slice(0, 8)}</td>
                  <td>{line.description ?? "—"}</td>
                  <td className="numeric" dir="ltr">
                    {line.debit_minor === 0 ? "" : money(line.debit_minor, lang)}
                  </td>
                  <td className="numeric" dir="ltr">
                    {line.credit_minor === 0 ? "" : money(line.credit_minor, lang)}
                  </td>
                </tr>
              ))}
              <tr>
                <td colSpan={2}><strong>{t("total")}</strong></td>
                <td className="numeric" dir="ltr"><strong>{money(detail.total_debit_minor, lang)}</strong></td>
                <td className="numeric" dir="ltr"><strong>{money(detail.total_credit_minor, lang)}</strong></td>
              </tr>
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}