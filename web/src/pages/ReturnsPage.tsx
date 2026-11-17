import { useCallback, useEffect, useMemo, useState } from "react";

import {
  ApiError,
  createCreditNote,
  createDebitNote,
  listBills,
  listCreditNotes,
  listCustomers,
  listDebitNotes,
  listInvoices,
  listSuppliers,
  listTaxCodes,
  postCreditNote,
  postDebitNote,
} from "../api";
import type { Bill, CreditNote, Customer, DebitNote, Invoice, TaxCode } from "../api";
import { translations } from "../i18n";
import { ListError } from "../components/ListError";
import type { Lang } from "../i18n";
import { anyText, errorText, money, today } from "./utils";

interface ReturnsPageProps {
  lang: Lang;
  company: { id: string; base_currency: string; permissions: string[] };
}

type ReturnKind = "credit" | "debit";
type ReturnDocument = CreditNote | DebitNote;

export function ReturnsPage({ lang, company }: ReturnsPageProps) {
  const t = (key: keyof (typeof translations)["ar"]): string => translations[lang][key];
  const [kind, setKind] = useState<ReturnKind>("credit");
  const [creditNotes, setCreditNotes] = useState<CreditNote[]>([]);
  const [debitNotes, setDebitNotes] = useState<DebitNote[]>([]);
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [bills, setBills] = useState<Bill[]>([]);
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [suppliers, setSuppliers] = useState<Customer[]>([]);
  const [taxCodes, setTaxCodes] = useState<TaxCode[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [documentId, setDocumentId] = useState("");
  const [lineId, setLineId] = useState("");
  const [quantity, setQuantity] = useState("1");
  const [returnDate, setReturnDate] = useState(today);

  const canCreate =
    kind === "credit"
      ? company.permissions.includes("sales.credit_note.create")
      : company.permissions.includes("purchasing.debit_note.create");
  const canPost =
    kind === "credit"
      ? company.permissions.includes("sales.credit_note.post")
      : company.permissions.includes("purchasing.debit_note.post");

  const reload = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const [creditRows, debitRows, invoiceRows, billRows, customerRows, supplierRows, taxRows] =
        await Promise.all([
          listCreditNotes(signal),
          listDebitNotes(signal),
          listInvoices(signal),
          listBills(signal),
          listCustomers(signal),
          listSuppliers(signal),
          listTaxCodes(signal),
        ]);
      setCreditNotes(creditRows);
      setDebitNotes(debitRows);
      setInvoices(invoiceRows.filter((invoice) => invoice.status !== "draft"));
      setBills(billRows.filter((bill) => bill.status !== "draft"));
      setCustomers(customerRows);
      setSuppliers(supplierRows);
      setTaxCodes(taxRows.filter((code) => code.kind !== "withholding"));
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

  const sourceDocuments = kind === "credit" ? invoices : bills;
  const selectedLines = useMemo(() => {
    const source = sourceDocuments.find((document) => document.id === documentId);
    return source?.lines ?? [];
  }, [documentId, sourceDocuments]);
  const customerNames = useMemo(() => {
    const map = new Map<string, string>();
    for (const customer of customers) {
      map.set(customer.id, anyText(lang, customer.name_ar, customer.name_en));
    }
    return map;
  }, [customers, lang]);
  const supplierNames = useMemo(() => {
    const map = new Map<string, string>();
    for (const supplier of suppliers) {
      map.set(supplier.id, anyText(lang, supplier.name_ar, supplier.name_en));
    }
    return map;
  }, [suppliers, lang]);
  const taxById = useMemo(() => {
    const map = new Map<string, TaxCode>();
    for (const code of taxCodes) {
      map.set(code.id, code);
    }
    return map;
  }, [taxCodes]);
  const rows: ReturnDocument[] = kind === "credit" ? creditNotes : debitNotes;

  function chooseDocument(id: string): void {
    setDocumentId(id);
    setLineId("");
  }

  function selectedSource() {
    return sourceDocuments.find((document) => document.id === documentId);
  }

  async function submit(post: boolean): Promise<void> {
    setError(null);
    setNotice(null);
    const source = selectedSource();
    const line = selectedLines.find((candidate) => candidate.id === lineId);
    if (source === undefined || line === undefined || Number(quantity) <= 0) {
      setError(t("requiredFields"));
      return;
    }
    setSaving(true);
    try {
      const commonLine = {
        description: line.description,
        quantity,
        unit_price_minor: line.unit_price_minor,
        item_id: line.item_id,
        tax_code_id: line.tax_code_id,
      };
      if (kind === "credit" && "customer_id" in source) {
        await createCreditNote({
          customer_id: source.customer_id,
          invoice_id: source.id,
          credit_note_date: returnDate,
          currency: company.base_currency,
          post,
          lines: [{ ...commonLine, invoice_line_id: line.id }],
        });
      } else if (kind === "debit" && "supplier_id" in source) {
        await createDebitNote({
          supplier_id: source.supplier_id,
          bill_id: source.id,
          debit_note_date: returnDate,
          currency: company.base_currency,
          post,
          lines: [{ ...commonLine, bill_line_id: line.id }],
        });
      } else {
        setError(t("requiredFields"));
        return;
      }
      setNotice(t("returnSaved"));
      setFormOpen(false);
      setDocumentId("");
      setLineId("");
      setQuantity("1");
      await reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setSaving(false);
    }
  }

  async function postDraft(id: string): Promise<void> {
    setError(null);
    try {
      if (kind === "credit") {
        await postCreditNote(id);
      } else {
        await postDebitNote(id);
      }
      setNotice(t("returnSaved"));
      await reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    }
  }

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h1>{t("returns")}</h1>
          <p className="muted">{creditNotes.length} {t("creditNotes")} · {debitNotes.length} {t("debitNotes")}</p>
        </div>
        {canCreate ? (
          <button type="button" className="button primary" onClick={() => setFormOpen((open) => !open)}>
            {formOpen ? t("cancel") : t("newReturn")}
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
              <span>{t("returns")}</span>
              <select
                value={kind}
                onChange={(event) => {
                  setKind(event.target.value === "debit" ? "debit" : "credit");
                  setDocumentId("");
                  setLineId("");
                }}
              >
                <option value="credit">{t("creditNotes")}</option>
                <option value="debit">{t("debitNotes")}</option>
              </select>
            </label>
            <label className="field wide">
              <span>{kind === "credit" ? t("invoice") : t("bill")}</span>
              <select value={documentId} onChange={(event) => chooseDocument(event.target.value)}>
                <option value="">{t("selectDocument")}</option>
                {sourceDocuments.map((document) => (
                  <option key={document.id} value={document.id}>
                    {document.number} · {money(document.total_minor, lang)}
                  </option>
                ))}
              </select>
            </label>
            <label className="field wide">
              <span>{t("description")}</span>
              <select value={lineId} onChange={(event) => setLineId(event.target.value)}>
                <option value="">{t("selectDocument")}</option>
                {selectedLines.map((line) => (
                  <option key={line.id} value={line.id}>
                    {line.description} · {line.quantity} · {money(line.unit_price_minor, lang)}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>{t("quantity")}</span>
              <input type="number" min="0.0001" step="0.0001" value={quantity} onChange={(event) => setQuantity(event.target.value)} />
            </label>
            <label className="field">
              <span>{t("creditNoteDate")}</span>
              <input type="date" value={returnDate} onChange={(event) => setReturnDate(event.target.value)} />
            </label>
            <label className="field">
              <span>{t("tax")}</span>
              <input
                value={
                  selectedLines.find((line) => line.id === lineId)?.tax_code_id === null ||
                  selectedLines.find((line) => line.id === lineId) === undefined
                    ? "—"
                    : taxById.get(
                        selectedLines.find((line) => line.id === lineId)?.tax_code_id ?? "",
                      )?.code ?? "—"
                }
                readOnly
              />
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
          <div className="empty-state">{t("emptyReturns")}</div>
        ) : (
          <table className="data-table" data-testid="return-table">
            <thead>
              <tr>
                <th>{t("number")}</th>
                <th>{kind === "credit" ? t("customer") : t("supplier")}</th>
                <th>{t("creditNoteDate")}</th>
                <th>{t("status")}</th>
                <th className="numeric">{t("total")}</th>
                <th>{t("actions")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const partyId =
                  "customer_id" in row ? row.customer_id : "supplier_id" in row ? row.supplier_id : "";
                const partyName =
                  "customer_id" in row
                    ? customerNames.get(partyId)
                    : supplierNames.get(partyId);
                return (
                  <tr key={row.id}>
                    <td className="mono" dir="ltr">{row.number}</td>
                    <td>{partyName ?? partyId.slice(0, 8)}</td>
                    <td className="mono" dir="ltr">
                      {"credit_note_date" in row ? row.credit_note_date : row.debit_note_date}
                    </td>
                    <td>
                      <span className={"badge " + row.status}>
                        {row.status === "posted" ? t("posted") : t("draft")}
                      </span>
                    </td>
                    <td className="numeric" dir="ltr">{money(row.total_minor, lang)}</td>
                    <td>
                      {row.status === "draft" && canPost ? (
                        <button type="button" className="button ghost" onClick={() => void postDraft(row.id)}>
                          {t("postDocument")}
                        </button>
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
