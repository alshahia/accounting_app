import { useCallback, useEffect, useMemo, useState } from "react";

import {
  ApiError,
  apAging,
  auditTrail,
  arAging,
  balanceSheet,
  customerStatement,
  documentRegister,
  downloadReportPath,
  getCheque,
  getPayment,
  getPurchaseBill,
  getPurchaseDebitNote,
  getReceipt,
  getSalesCreditNote,
  getSalesInvoice,
  getTransfer,
  generalLedger,
  inventoryValuation,
  listAccounts,
  listCustomers,
  listSuppliers,
  profitAndLoss,
  stockMovement,
  supplierStatement,
  taxReturn,
} from "../api";
import type {
  Account,
  AuditTrailReport,
  AgingReport,
  BalanceSheet,
  Customer,
  DocumentRegister,
  GeneralLedger,
  InventoryValuation,
  PartyStatement,
  ProfitAndLoss,
  StockMovement,
  TaxReturn,
} from "../api";
import { translations } from "../i18n";
import type { Lang } from "../i18n";
import { anyText, errorText, money, today } from "./utils";

interface ReportsPageProps {
  lang: Lang;
  company: { id: string; base_currency: string; permissions: string[] };
  onOpenEntry: (entryId: string) => void;
}

type ReportKind =
  | "pnl"
  | "balance"
  | "ar"
  | "ap"
  | "statement"
  | "inventory"
  | "tax"
  | "gl"
  | "stock"
  | "register"
  | "audit";

type PartyKind = "customer" | "supplier";

type DocumentKind =
  | "sales_invoice"
  | "purchase_bill"
  | "sales_credit_note"
  | "purchase_debit_note"
  | "receipt"
  | "supplier_payment"
  | "cheque"
  | "cash_transfer";

interface DrillDocument {
  kind: DocumentKind;
  number: string;
  documentDate: string;
  status: string;
  currency: string;
  totalMinor: number;
  journalEntryId: string | null;
  party: string | null;
  lines: { label: string; quantity: string | null; amountMinor: number }[];
}

const DRILLABLE_DOCUMENTS: DocumentKind[] = [
  "sales_invoice",
  "purchase_bill",
  "sales_credit_note",
  "purchase_debit_note",
  "receipt",
  "supplier_payment",
  "cheque",
  "cash_transfer",
];

const STATEMENT_DOCUMENTS: Record<string, DocumentKind> = {
  invoice: "sales_invoice",
  credit_note: "sales_credit_note",
  receipt: "receipt",
  bill: "purchase_bill",
  debit_note: "purchase_debit_note",
  payment: "supplier_payment",
};

const DOCUMENT_LABELS: Record<DocumentKind, keyof (typeof translations)["ar"]> = {
  sales_invoice: "invoices",
  purchase_bill: "bill",
  sales_credit_note: "creditNotes",
  purchase_debit_note: "debitNotes",
  receipt: "receipts",
  supplier_payment: "payments",
  cheque: "cheques",
  cash_transfer: "transfers",
};

export function ReportsPage({ lang, company, onOpenEntry }: ReportsPageProps) {
  const t = (key: keyof (typeof translations)["ar"]): string => translations[lang][key];
  const [kind, setKind] = useState<ReportKind>("pnl");
  const [start, setStart] = useState(() => today().slice(0, 8) + "01");
  const [end, setEnd] = useState(today);
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [suppliers, setSuppliers] = useState<Customer[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [partyKind, setPartyKind] = useState<PartyKind>("customer");
  const [partyId, setPartyId] = useState("");
  const [accountId, setAccountId] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pnl, setPnl] = useState<ProfitAndLoss | null>(null);
  const [sheet, setSheet] = useState<BalanceSheet | null>(null);
  const [aging, setAging] = useState<AgingReport | null>(null);
  const [statement, setStatement] = useState<PartyStatement | null>(null);
  const [inventory, setInventory] = useState<InventoryValuation | null>(null);
  const [tax, setTax] = useState<TaxReturn | null>(null);
  const [ledger, setLedger] = useState<GeneralLedger | null>(null);
  const [stock, setStock] = useState<StockMovement | null>(null);
  const [compare, setCompare] = useState(false);
  const [register, setRegister] = useState<DocumentRegister | null>(null);
  const [audit, setAudit] = useState<AuditTrailReport | null>(null);
  const [documentType, setDocumentType] = useState("");
  const [drill, setDrill] = useState<DrillDocument | null>(null);
  const [drillLoading, setDrillLoading] = useState(false);
  const [drillError, setDrillError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    listCustomers(controller.signal)
      .then((rows) => {
        setCustomers(rows);
        setPartyId((current) => current || rows[0]?.id || "");
      })
      .catch(() => undefined);
    listSuppliers(controller.signal)
      .then(setSuppliers)
      .catch(() => undefined);
    listAccounts(controller.signal)
      .then((rows) => {
        setAccounts(rows);
        setAccountId((current) => current || rows[0]?.id || "");
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  const parties = partyKind === "customer" ? customers : suppliers;

  const activeMeta =
    kind === "pnl"
      ? (pnl?.meta ?? null)
      : kind === "balance"
        ? (sheet?.meta ?? null)
        : kind === "ar" || kind === "ap"
          ? (aging?.meta ?? null)
          : kind === "statement"
            ? (statement?.meta ?? null)
            : kind === "inventory"
              ? (inventory?.meta ?? null)
              : kind === "gl"
                ? (ledger?.meta ?? null)
                : kind === "stock"
                  ? (stock?.meta ?? null)
                  : kind === "register"
                    ? (register?.meta ?? null)
                    : kind === "audit"
                      ? (audit?.meta ?? null)
                      : (tax?.meta ?? null);

  useEffect(() => {
    setPartyId(parties[0]?.id ?? "");
  }, [partyKind, parties]);


  const openDrill = useCallback(
    async (documentKind: DocumentKind, documentId: string) => {
      setDrillLoading(true);
      setDrillError(null);
      try {
        if (documentKind === "sales_invoice") {
          const invoice = await getSalesInvoice(documentId);
          setDrill({
            kind: documentKind,
            number: invoice.number,
            documentDate: invoice.invoice_date,
            status: invoice.status,
            currency: invoice.currency,
            totalMinor: invoice.total_minor,
            journalEntryId: invoice.journal_entry_id,
            party: null,
            lines: invoice.lines.map((line) => ({
              label: line.description,
              quantity: line.quantity,
              amountMinor: line.line_total_minor,
            })),
          });
        } else if (documentKind === "purchase_bill") {
          const bill = await getPurchaseBill(documentId);
          setDrill({
            kind: documentKind,
            number: bill.number,
            documentDate: bill.bill_date,
            status: bill.status,
            currency: bill.currency,
            totalMinor: bill.total_minor,
            journalEntryId: bill.journal_entry_id ?? null,
            party: null,
            lines: bill.lines.map((line) => ({
              label: line.description,
              quantity: line.quantity,
              amountMinor: line.line_total_minor,
            })),
          });
        } else if (documentKind === "sales_credit_note") {
          const note = await getSalesCreditNote(documentId);
          setDrill({
            kind: documentKind,
            number: note.number,
            documentDate: note.credit_note_date,
            status: note.status,
            currency: note.currency,
            totalMinor: note.total_minor,
            journalEntryId: note.journal_entry_id ?? null,
            party: null,
            lines: note.lines.map((line) => ({
              label: line.description,
              quantity: line.quantity,
              amountMinor: line.line_total_minor,
            })),
          });
        } else if (documentKind === "purchase_debit_note") {
          const note = await getPurchaseDebitNote(documentId);
          setDrill({
            kind: documentKind,
            number: note.number,
            documentDate: note.debit_note_date,
            status: note.status,
            currency: note.currency,
            totalMinor: note.total_minor,
            journalEntryId: note.journal_entry_id ?? null,
            party: null,
            lines: note.lines.map((line) => ({
              label: line.description,
              quantity: line.quantity,
              amountMinor: line.line_total_minor,
            })),
          });
        } else if (documentKind === "receipt") {
          const receipt = await getReceipt(documentId);
          setDrill({
            kind: documentKind,
            number: receipt.number,
            documentDate: receipt.receipt_date,
            status: receipt.status,
            currency: receipt.currency,
            totalMinor: receipt.amount_minor,
            journalEntryId: receipt.journal_entry_id ?? null,
            party: null,
            lines: receipt.allocations.map((allocation) => ({
              label: allocation.invoice_id ?? allocation.id,
              quantity: null,
              amountMinor: allocation.amount_minor,
            })),
          });
        } else if (documentKind === "supplier_payment") {
          const payment = await getPayment(documentId);
          setDrill({
            kind: documentKind,
            number: payment.number,
            documentDate: payment.payment_date,
            status: payment.status,
            currency: payment.currency,
            totalMinor: payment.amount_minor,
            journalEntryId: payment.journal_entry_id ?? null,
            party: null,
            lines: payment.allocations.map((allocation) => ({
              label: allocation.bill_id ?? allocation.id,
              quantity: null,
              amountMinor: allocation.amount_minor,
            })),
          });
        } else if (documentKind === "cheque") {
          const cheque = await getCheque(documentId);
          setDrill({
            kind: documentKind,
            number: cheque.number,
            documentDate: cheque.issue_date,
            status: cheque.status,
            currency: cheque.currency,
            totalMinor: cheque.amount_minor,
            journalEntryId: cheque.events.find((event) => event.journal_entry_id)?.journal_entry_id ?? null,
            party: cheque.party_name,
            lines: cheque.allocations.map((allocation) => ({
              label: allocation.invoice_id ?? allocation.bill_id ?? allocation.id,
              quantity: null,
              amountMinor: allocation.amount_minor,
            })),
          });
        } else {
          const transfer = await getTransfer(documentId);
          setDrill({
            kind: documentKind,
            number: transfer.number,
            documentDate: transfer.transfer_date,
            status: transfer.status,
            currency: transfer.currency,
            totalMinor: transfer.amount_minor,
            journalEntryId: transfer.journal_entry_id,
            party: `${transfer.from_account_name_ar} → ${transfer.to_account_name_ar}`,
            lines: [],
          });
        }
      } catch (caught) {
        setDrillError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
      } finally {
        setDrillLoading(false);
      }
    },
    [t],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      if (kind === "pnl") {
        setPnl(await profitAndLoss(start, end, compare));
      } else if (kind === "balance") {
        setSheet(await balanceSheet(end, compare));
      } else if (kind === "ar") {
        setAging(await arAging(end));
      } else if (kind === "ap") {
        setAging(await apAging(end));
      } else if (kind === "statement") {
        setStatement(
          partyKind === "customer"
            ? await customerStatement(partyId, start, end, compare)
            : await supplierStatement(partyId, start, end, compare),
        );
      } else if (kind === "inventory") {
        setInventory(await inventoryValuation());
      } else if (kind === "gl") {
        setLedger(await generalLedger(accountId, start, end));
      } else if (kind === "stock") {
        setStock(await stockMovement(start, end));
      } else if (kind === "register") {
        setRegister(await documentRegister(start, end, documentType || undefined));
      } else if (kind === "audit") {
        setAudit(await auditTrail(start, end));
      } else {
        setTax(await taxReturn(start, end));
      }
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setLoading(false);
    }
  }, [accountId, compare, documentType, end, kind, partyId, partyKind, start, t]);

  const tabs: { kind: ReportKind; label: string }[] = [
    { kind: "pnl", label: t("profitAndLoss") },
    { kind: "balance", label: t("balanceSheet") },
    { kind: "ar", label: t("arAging") },
    { kind: "ap", label: t("apAging") },
    { kind: "statement", label: t("statement") },
    { kind: "inventory", label: t("inventory") },
    { kind: "tax", label: t("taxReturn") },
    { kind: "gl", label: t("generalLedgerReport") },
    { kind: "stock", label: t("stockMovement") },
    { kind: "register", label: t("documentRegister") },
    { kind: "audit", label: t("auditLog") },
  ];

  const downloadPath = useMemo(() => {
    if (kind === "pnl") {
      return "/reports/profit-and-loss";
    }
    if (kind === "balance") {
      return "/reports/balance-sheet";
    }
    if (kind === "ar") {
      return "/reports/ar-aging";
    }
    if (kind === "ap") {
      return "/reports/ap-aging";
    }
    if (kind === "statement") {
      return partyKind === "customer" ? "/reports/customer-statement" : "/reports/supplier-statement";
    }
    if (kind === "gl") {
      return "/reports/general-ledger";
    }
    if (kind === "register") {
      return "/reports/document-register";
    }
    if (kind === "inventory") {
      return "/reports/inventory-valuation";
    }
    if (kind === "stock") {
      return "/reports/stock-movement";
    }
    if (kind === "tax") {
      return "/reports/tax-return";
    }
    if (kind === "audit") {
      return "/reports/audit-trail";
    }
    return null;
  }, [kind, partyKind]);

  function query(format: "pdf" | "xlsx"): string {
    if (kind === "inventory") {
      return `${downloadPath}.${format}`;
    }
    const params =
      kind === "balance" || kind === "ar" || kind === "ap"
        ? new URLSearchParams({ as_of: end })
        : kind === "statement"
          ? new URLSearchParams({
              [partyKind === "customer" ? "customer_id" : "supplier_id"]: partyId,
              start,
              end,
            })
          : kind === "gl"
            ? new URLSearchParams({ account_id: accountId, start, end })
            : kind === "register"
              ? new URLSearchParams({
                  start,
                  end,
                  ...(documentType === "" ? {} : { document_type: documentType }),
                })
              : new URLSearchParams({ start, end });
    if (compare && (kind === "pnl" || kind === "balance" || kind === "statement")) {
      params.set("compare", "true");
    }
    return `${downloadPath}.${format}?${params.toString()}`;
  }

  async function download(format: "pdf" | "xlsx"): Promise<void> {
    if (downloadPath === null) {
      return;
    }
    try {
      const blob = await downloadReportPath(query(format));
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${kind}.${format}`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    }
  }

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h1>{t("reports")}</h1>
          <p className="muted">{company.base_currency}</p>
          {activeMeta !== null ? (
            <p className="muted report-meta" data-testid="report-meta">
              {t("generatedAt")}: {new Date(activeMeta.generated_at).toLocaleString()} ·{" "}
              {t("generatedBy")}: {activeMeta.generated_by} · {t("companyLabel")}:{" "}
              {activeMeta.company_name_ar}
            </p>
          ) : null}
        </div>
      </header>
      <div className="tabs" role="tablist">
        {tabs.map((tab) => (
          <button
            key={tab.kind}
            type="button"
            role="tab"
            aria-selected={kind === tab.kind}
            className={"tab " + (kind === tab.kind ? "active" : "")}
            onClick={() => setKind(tab.kind)}
          >
            {tab.label}
          </button>
        ))}
      </div>
      <form
        className="card filter-bar"
        onSubmit={(event) => {
          event.preventDefault();
          void load();
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
        {kind === "pnl" || kind === "balance" || kind === "statement" ? (
          <label className="field checkbox-field">
            <input
              type="checkbox"
              checked={compare}
              onChange={(event) => setCompare(event.target.checked)}
            />
            <span>{t("comparePeriod")}</span>
          </label>
        ) : null}
        {kind === "statement" ? (
          <>
            <label className="field">
              <span>{t("partyType")}</span>
              <select
                value={partyKind}
                onChange={(event) => setPartyKind(event.target.value as PartyKind)}
              >
                <option value="customer">{t("customer")}</option>
                <option value="supplier">{t("supplier")}</option>
              </select>
            </label>
            <label className="field">
              <span>{partyKind === "customer" ? t("customer") : t("supplier")}</span>
              <select value={partyId} onChange={(event) => setPartyId(event.target.value)}>
                {parties.map((party) => (
                  <option key={party.id} value={party.id}>
                    {anyText(lang, party.name_ar, party.name_en)}
                  </option>
                ))}
              </select>
            </label>
          </>
        ) : null}
        {kind === "register" ? (
          <label className="field">
            <span>{t("documentType")}</span>
            <select value={documentType} onChange={(event) => setDocumentType(event.target.value)}>
              <option value="">{t("all")}</option>
              <option value="sales_invoice">{t("invoices")}</option>
              <option value="purchase_bill">{t("bill")}</option>
              <option value="sales_credit_note">{t("creditNotes")}</option>
              <option value="purchase_debit_note">{t("debitNotes")}</option>
              <option value="receipt">{t("receipts")}</option>
              <option value="supplier_payment">{t("payments")}</option>
              <option value="cash_transfer">{t("transfers")}</option>
              <option value="cheque">{t("cheques")}</option>
            </select>
          </label>
        ) : null}
        {kind === "gl" ? (
          <label className="field">
            <span>{t("account")}</span>
            <select value={accountId} onChange={(event) => setAccountId(event.target.value)}>
              {accounts.map((account) => (
                <option key={account.id} value={account.id}>
                  {account.code} · {anyText(lang, account.name_ar, account.name_en)}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <div className="spacer" />
        <button type="submit" className="button primary" disabled={loading}>
          {loading ? t("loading") : t("show")}
        </button>
        {downloadPath !== null ? (
          <>
            <button type="button" className="button ghost" onClick={() => void download("pdf")}>
              {t("printPdf")}
            </button>
            <button type="button" className="button ghost" onClick={() => void download("xlsx")}>
              {t("printExcel")}
            </button>
          </>
        ) : null}
      </form>
      {error !== null ? <div className="alert error" role="alert">{error}</div> : null}

      {drill !== null ? (
        <div className="card drill-card" data-testid="report-drill">
          <header className="drill-head">
            <div>
              <strong className="mono" dir="ltr">{drill.number}</strong>{" "}
              <span className="muted">{t(DOCUMENT_LABELS[drill.kind])}</span>
            </div>
            <div className="spacer" />
            {drill.journalEntryId !== null ? (
              <button
                type="button"
                className="button ghost"
                onClick={() => onOpenEntry(drill.journalEntryId as string)}
              >
                {t("openJournalEntry")}
              </button>
            ) : null}
            <button type="button" className="button ghost" onClick={() => setDrill(null)}>
              {t("close")}
            </button>
          </header>
          <p className="muted">
            {t("documentDate")}: <span className="mono" dir="ltr">{drill.documentDate}</span> ·{" "}
            {t("status")}: {drill.status} · {t("currency")}: <span dir="ltr">{drill.currency}</span>
            {drill.party !== null ? <> · {drill.party}</> : null}
          </p>
          {drill.lines.length > 0 ? (
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t("description")}</th>
                  <th className="numeric">{t("quantity")}</th>
                  <th className="numeric">{t("amount")}</th>
                </tr>
              </thead>
              <tbody>
                {drill.lines.map((line, index) => (
                  <tr key={line.label + String(index)}>
                    <td className="mono" dir="ltr">{line.label}</td>
                    <td className="numeric" dir="ltr">{line.quantity ?? ""}</td>
                    <td className="numeric" dir="ltr">{money(line.amountMinor, lang)}</td>
                  </tr>
                ))}
                <tr>
                  <td colSpan={2}><strong>{t("total")}</strong></td>
                  <td className="numeric" dir="ltr"><strong>{money(drill.totalMinor, lang)}</strong></td>
                </tr>
              </tbody>
            </table>
          ) : (
            <p className="muted">
              {t("total")}: <strong>{money(drill.totalMinor, lang)}</strong>
            </p>
          )}
        </div>
      ) : null}

      {drillLoading ? <p className="muted">{t("loading")}</p> : null}
      {drillError !== null ? (
        <div className="alert error" role="alert">{drillError}</div>
      ) : null}

      <div className="card table-card">
        {kind === "pnl" && pnl !== null ? (
          <table className="data-table">
            <thead>
              <tr>
                <th>{t("description")}</th>
                <th className="numeric">{t("amount")}</th>
                {compare ? <th className="numeric">{t("compareAmount")}</th> : null}
                {compare ? <th className="numeric">{t("change")}</th> : null}
              </tr>
            </thead>
            <tbody>
              <tr><td colSpan={compare ? 4 : 2}><strong>{t("revenue")}</strong></td></tr>
              {pnl.revenue.map((row) => (
                <tr key={row.account_id}>
                  <td>{row.code} · {anyText(lang, row.name_ar, row.name_en)}</td>
                  <td className="numeric" dir="ltr">{money(row.amount_minor, lang)}</td>
                  {compare ? (
                    <>
                      <td className="numeric" dir="ltr">{money(row.compare_amount_minor ?? 0, lang)}</td>
                      <td className="numeric" dir="ltr">
                        {money(row.amount_minor - (row.compare_amount_minor ?? 0), lang)}
                      </td>
                    </>
                  ) : null}
                </tr>
              ))}
              <tr>
                <td><strong>{t("total")}</strong></td>
                <td className="numeric" dir="ltr"><strong>{money(pnl.total_revenue_minor, lang)}</strong></td>
                {compare ? (
                  <>
                    <td className="numeric" dir="ltr">
                      <strong>{money(pnl.compare_total_revenue_minor ?? 0, lang)}</strong>
                    </td>
                    <td className="numeric" dir="ltr">
                      <strong>
                        {money(
                          pnl.total_revenue_minor - (pnl.compare_total_revenue_minor ?? 0),
                          lang,
                        )}
                      </strong>
                    </td>
                  </>
                ) : null}
              </tr>
              <tr><td colSpan={compare ? 4 : 2}><strong>{t("expenses")}</strong></td></tr>
              {pnl.expenses.map((row) => (
                <tr key={row.account_id}>
                  <td>{row.code} · {anyText(lang, row.name_ar, row.name_en)}</td>
                  <td className="numeric" dir="ltr">{money(row.amount_minor, lang)}</td>
                  {compare ? (
                    <>
                      <td className="numeric" dir="ltr">{money(row.compare_amount_minor ?? 0, lang)}</td>
                      <td className="numeric" dir="ltr">
                        {money(row.amount_minor - (row.compare_amount_minor ?? 0), lang)}
                      </td>
                    </>
                  ) : null}
                </tr>
              ))}
              <tr>
                <td><strong>{t("netProfit")}</strong></td>
                <td className="numeric" dir="ltr"><strong>{money(pnl.net_profit_minor, lang)}</strong></td>
                {compare ? (
                  <>
                    <td className="numeric" dir="ltr">
                      <strong>{money(pnl.compare_net_profit_minor ?? 0, lang)}</strong>
                    </td>
                    <td className="numeric" dir="ltr">
                      <strong>
                        {money(pnl.net_profit_minor - (pnl.compare_net_profit_minor ?? 0), lang)}
                      </strong>
                    </td>
                  </>
                ) : null}
              </tr>
            </tbody>
          </table>
        ) : null}

        {kind === "balance" && sheet !== null ? (
          <table className="data-table">
            <thead>
              <tr>
                <th>{t("description")}</th>
                <th className="numeric">{t("amount")}</th>
                {compare ? <th className="numeric">{t("compareAmount")}</th> : null}
                {compare ? <th className="numeric">{t("change")}</th> : null}
              </tr>
            </thead>
            <tbody>
              <tr><td colSpan={compare ? 4 : 2}><strong>{t("assets")}</strong></td></tr>
              {sheet.assets.map((row) => (
                <tr key={row.account_id}>
                  <td>{row.code} · {anyText(lang, row.name_ar, row.name_en)}</td>
                  <td className="numeric" dir="ltr">{money(row.amount_minor, lang)}</td>
                  {compare ? (
                    <>
                      <td className="numeric" dir="ltr">{money(row.compare_amount_minor ?? 0, lang)}</td>
                      <td className="numeric" dir="ltr">
                        {money(row.amount_minor - (row.compare_amount_minor ?? 0), lang)}
                      </td>
                    </>
                  ) : null}
                </tr>
              ))}
              <tr>
                <td><strong>{t("total")} {t("assets")}</strong></td>
                <td className="numeric" dir="ltr"><strong>{money(sheet.total_assets_minor, lang)}</strong></td>
                {compare ? (
                  <>
                    <td className="numeric" dir="ltr">
                      <strong>{money(sheet.compare_total_assets_minor ?? 0, lang)}</strong>
                    </td>
                    <td className="numeric" dir="ltr">
                      <strong>
                        {money(
                          sheet.total_assets_minor - (sheet.compare_total_assets_minor ?? 0),
                          lang,
                        )}
                      </strong>
                    </td>
                  </>
                ) : null}
              </tr>
              <tr><td colSpan={compare ? 4 : 2}><strong>{t("liabilities")}</strong></td></tr>
              {sheet.liabilities.map((row) => (
                <tr key={row.account_id}>
                  <td>{row.code} · {anyText(lang, row.name_ar, row.name_en)}</td>
                  <td className="numeric" dir="ltr">{money(row.amount_minor, lang)}</td>
                  {compare ? (
                    <>
                      <td className="numeric" dir="ltr">{money(row.compare_amount_minor ?? 0, lang)}</td>
                      <td className="numeric" dir="ltr">
                        {money(row.amount_minor - (row.compare_amount_minor ?? 0), lang)}
                      </td>
                    </>
                  ) : null}
                </tr>
              ))}
              <tr>
                <td><strong>{t("total")} {t("liabilities")}</strong></td>
                <td className="numeric" dir="ltr"><strong>{money(sheet.total_liabilities_minor, lang)}</strong></td>
                {compare ? (
                  <>
                    <td className="numeric" dir="ltr">
                      <strong>{money(sheet.compare_total_liabilities_minor ?? 0, lang)}</strong>
                    </td>
                    <td className="numeric" dir="ltr">
                      <strong>
                        {money(
                          sheet.total_liabilities_minor -
                            (sheet.compare_total_liabilities_minor ?? 0),
                          lang,
                        )}
                      </strong>
                    </td>
                  </>
                ) : null}
              </tr>
              <tr><td colSpan={compare ? 4 : 2}><strong>{t("equity")}</strong></td></tr>
              {sheet.equity.map((row) => (
                <tr key={row.account_id}>
                  <td>{row.code} · {anyText(lang, row.name_ar, row.name_en)}</td>
                  <td className="numeric" dir="ltr">{money(row.amount_minor, lang)}</td>
                  {compare ? (
                    <>
                      <td className="numeric" dir="ltr">{money(row.compare_amount_minor ?? 0, lang)}</td>
                      <td className="numeric" dir="ltr">
                        {money(row.amount_minor - (row.compare_amount_minor ?? 0), lang)}
                      </td>
                    </>
                  ) : null}
                </tr>
              ))}
              <tr>
                <td><strong>{t("equity")}</strong></td>
                <td className="numeric" dir="ltr"><strong>{money(sheet.total_equity_minor, lang)}</strong></td>
                {compare ? (
                  <>
                    <td className="numeric" dir="ltr">
                      <strong>{money(sheet.compare_total_equity_minor ?? 0, lang)}</strong>
                    </td>
                    <td className="numeric" dir="ltr">
                      <strong>
                        {money(
                          sheet.total_equity_minor - (sheet.compare_total_equity_minor ?? 0),
                          lang,
                        )}
                      </strong>
                    </td>
                  </>
                ) : null}
              </tr>
              <tr>
                <td colSpan={compare ? 4 : 2}>
                  {sheet.balanced ? t("balancedSheet") : t("unbalancedSheet")}
                </td>
              </tr>
            </tbody>
          </table>
        ) : null}

        {(kind === "ar" || kind === "ap") && aging !== null ? (
          <table className="data-table">
            <thead>
              <tr>
                <th>{kind === "ar" ? t("customer") : t("supplier")}</th>
                <th>{t("number")}</th>
                <th>{t("documentDate")}</th>
                <th className="numeric">{t("daysOverdue")}</th>
                <th className="numeric">{t("outstanding")}</th>
              </tr>
            </thead>
            <tbody>
              {aging.rows.map((row) => (
                <tr key={row.document_number}>
                  <td>{row.party_name_ar}</td>
                  <td className="mono" dir="ltr">
                    <button
                      type="button"
                      className="link-button"
                      data-testid="drill-document"
                      onClick={() =>
                        void openDrill(
                          kind === "ar" ? "sales_invoice" : "purchase_bill",
                          row.document_id,
                        )
                      }
                    >
                      {row.document_number}
                    </button>
                  </td>
                  <td className="mono" dir="ltr">{row.document_date}</td>
                  <td className="numeric">{row.days_overdue}</td>
                  <td className="numeric" dir="ltr">{money(row.base_outstanding_minor, lang)}</td>
                </tr>
              ))}
              <tr><td colSpan={4}><strong>{t("total")}</strong></td><td className="numeric" dir="ltr"><strong>{money(aging.total_base_outstanding_minor, lang)}</strong></td></tr>
            </tbody>
          </table>
        ) : null}

        {kind === "statement" && statement !== null ? (
          <table className="data-table">
            <thead>
              <tr>
                <th>{t("documentDate")}</th>
                <th>{t("number")}</th>
                <th>{t("description")}</th>
                <th className="numeric">{t("debit")}</th>
                <th className="numeric">{t("credit")}</th>
                <th className="numeric">{t("balance")}</th>
              </tr>
            </thead>
            <tbody>
              {compare && statement.compare_start_date ? (
                <>
                  <tr>
                    <td className="mono" dir="ltr">{statement.compare_start_date}</td>
                    <td />
                    <td>
                      {t("previousPeriod")} · {t("openingBalance")}
                    </td>
                    <td />
                    <td />
                    <td className="numeric" dir="ltr">
                      {money(statement.compare_opening_balance_minor ?? 0, lang)}
                    </td>
                  </tr>
                  <tr data-testid="statement-comparison">
                    <td className="mono" dir="ltr">{statement.compare_end_date}</td>
                    <td />
                    <td>{t("previousClosingBalance")}</td>
                    <td className="numeric" dir="ltr">
                      {money(statement.compare_total_debit_minor ?? 0, lang)}
                    </td>
                    <td className="numeric" dir="ltr">
                      {money(statement.compare_total_credit_minor ?? 0, lang)}
                    </td>
                    <td className="numeric" dir="ltr">
                      {money(statement.compare_closing_balance_minor ?? 0, lang)}
                    </td>
                  </tr>
                  <tr>
                    <td colSpan={6}>
                      <strong>
                        {t("currentPeriod")} · {statement.start_date} → {statement.end_date}
                      </strong>
                    </td>
                  </tr>
                </>
              ) : null}
              <tr>
                <td className="mono" dir="ltr">{statement.start_date}</td>
                <td />
                <td>{t("openingBalance")}</td>
                <td />
                <td />
                <td className="numeric" dir="ltr">{money(statement.opening_balance_minor, lang)}</td>
              </tr>
              {statement.rows.map((row) => (
                <tr key={row.number + row.entry_date}>
                  <td className="mono" dir="ltr">{row.entry_date}</td>
                  <td className="mono" dir="ltr">
                    {STATEMENT_DOCUMENTS[row.document_type] !== undefined ? (
                      <button
                        type="button"
                        className="link-button"
                        data-testid="drill-document"
                        onClick={() =>
                          void openDrill(STATEMENT_DOCUMENTS[row.document_type], row.document_id)
                        }
                      >
                        {row.number}
                      </button>
                    ) : (
                      row.number
                    )}
                  </td>
                  <td>{row.description ?? row.document_type}</td>
                  <td className="numeric" dir="ltr">{row.debit_minor === 0 ? "" : money(row.debit_minor, lang)}</td>
                  <td className="numeric" dir="ltr">{row.credit_minor === 0 ? "" : money(row.credit_minor, lang)}</td>
                  <td className="numeric" dir="ltr">{money(row.balance_minor, lang)}</td>
                </tr>
              ))}
              <tr>
                <td colSpan={5}><strong>{t("closingBalance")}</strong></td>
                <td className="numeric" dir="ltr"><strong>{money(statement.closing_balance_minor, lang)}</strong></td>
              </tr>
            </tbody>
          </table>
        ) : null}

        {kind === "inventory" && inventory !== null ? (
          <table className="data-table">
            <thead>
              <tr><th>{t("description")}</th><th className="numeric">{t("quantity")}</th><th className="numeric">{t("amount")}</th></tr>
            </thead>
            <tbody>
              {inventory.rows.map((row) => (
                <tr key={row.item_id}>
                  <td>{row.code} · {row.name_ar}</td>
                  <td className="numeric" dir="ltr">{row.quantity_on_hand}</td>
                  <td className="numeric" dir="ltr">{money(row.stock_value_minor, lang)}</td>
                </tr>
              ))}
              <tr><td colSpan={2}><strong>{t("total")}</strong></td><td className="numeric" dir="ltr"><strong>{money(inventory.total_value_minor, lang)}</strong></td></tr>
            </tbody>
          </table>
        ) : null}

        {kind === "gl" && ledger !== null ? (
          <table className="data-table" data-testid="gl-table">
            <thead>
              <tr>
                <th>{t("documentDate")}</th>
                <th>{t("entryNumber")}</th>
                <th>{t("description")}</th>
                <th className="numeric">{t("debit")}</th>
                <th className="numeric">{t("credit")}</th>
                <th className="numeric">{t("balance")}</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td className="mono" dir="ltr">{ledger.start_date}</td>
                <td />
                <td>{t("openingBalance")}</td>
                <td />
                <td />
                <td className="numeric" dir="ltr">{money(ledger.opening_balance_minor, lang)}</td>
              </tr>
              {ledger.rows.map((row, index) => (
                <tr key={row.entry_number + String(index)}>
                  <td className="mono" dir="ltr">{row.entry_date}</td>
                  <td className="mono" dir="ltr">
                    <button
                      type="button"
                      className="link-button"
                      data-testid="gl-entry-link"
                      title={t("openJournalEntry")}
                      onClick={() => onOpenEntry(row.entry_id)}
                    >
                      {row.entry_number}
                    </button>
                  </td>
                  <td>{row.description ?? "—"}</td>
                  <td className="numeric" dir="ltr">{row.debit_minor === 0 ? "" : money(row.debit_minor, lang)}</td>
                  <td className="numeric" dir="ltr">{row.credit_minor === 0 ? "" : money(row.credit_minor, lang)}</td>
                  <td className="numeric" dir="ltr">{money(row.balance_minor, lang)}</td>
                </tr>
              ))}
              <tr>
                <td colSpan={5}><strong>{t("closingBalance")}</strong></td>
                <td className="numeric" dir="ltr"><strong>{money(ledger.closing_balance_minor, lang)}</strong></td>
              </tr>
            </tbody>
          </table>
        ) : null}

        {kind === "stock" && stock !== null ? (
          <table className="data-table" data-testid="stock-table">
            <thead>
              <tr>
                <th>{t("documentDate")}</th>
                <th>{t("moveType")}</th>
                <th>{t("warehouses")}</th>
                <th>{t("itemLabel")}</th>
                <th className="numeric">{t("quantity")}</th>
                <th className="numeric">{t("unitCost")}</th>
                <th className="numeric">{t("value")}</th>
                <th>{t("documentType")}</th>
              </tr>
            </thead>
            <tbody>
              {stock.rows.map((row, index) => (
                <tr key={row.document_id + String(index)}>
                  <td className="mono" dir="ltr">{row.occurred_on}</td>
                  <td>{row.move_type}</td>
                  <td dir="ltr">{row.warehouse_code}</td>
                  <td>{row.item_code} · {row.item_name_ar}</td>
                  <td className="numeric" dir="ltr">{row.quantity}</td>
                  <td className="numeric" dir="ltr">{money(row.unit_cost_minor, lang)}</td>
                  <td className="numeric" dir="ltr">{money(row.value_minor, lang)}</td>
                  <td>{row.document_type}</td>
                </tr>
              ))}
              <tr>
                <td colSpan={6}><strong>{t("total")}</strong></td>
                <td className="numeric" dir="ltr">
                  <strong>
                    {money(stock.total_receipt_value_minor, lang)} / {money(stock.total_issue_value_minor, lang)}
                  </strong>
                </td>
                <td />
              </tr>
            </tbody>
          </table>
        ) : null}

        {kind === "register" && register !== null ? (
          <table className="data-table" data-testid="register-table">
            <thead>
              <tr>
                <th>{t("documentType")}</th>
                <th>{t("number")}</th>
                <th>{t("documentDate")}</th>
                <th>{t("status")}</th>
                <th>{t("currency")}</th>
                <th className="numeric">{t("amount")}</th>
                <th className="numeric">{t("baseTotal")}</th>
              </tr>
            </thead>
            <tbody>
              {register.rows.map((row, index) => (
                <tr key={row.number + String(index)}>
                  <td>{row.document_type}</td>
                  <td className="mono" dir="ltr">
                    {DRILLABLE_DOCUMENTS.includes(row.document_type as DocumentKind) ? (
                      <button
                        type="button"
                        className="link-button"
                        data-testid="drill-document"
                        onClick={() =>
                          void openDrill(row.document_type as DocumentKind, row.document_id)
                        }
                      >
                        {row.number}
                      </button>
                    ) : (
                      row.number
                    )}
                  </td>
                  <td className="mono" dir="ltr">{row.document_date}</td>
                  <td>{row.status}</td>
                  <td className="mono" dir="ltr">{row.currency}</td>
                  <td className="numeric" dir="ltr">{money(row.amount_minor, lang)}</td>
                  <td className="numeric" dir="ltr">{money(row.base_amount_minor, lang)}</td>
                </tr>
              ))}
              <tr>
                <td colSpan={6}><strong>{t("total")}</strong></td>
                <td className="numeric" dir="ltr"><strong>{money(register.total_amount_minor, lang)}</strong></td>
              </tr>
            </tbody>
          </table>
        ) : null}


        {kind === "audit" && audit !== null ? (
          <table className="data-table" data-testid="audit-table">
            <thead>
              <tr>
                <th>{t("auditTime")}</th>
                <th>{t("auditAction")}</th>
                <th>{t("auditEntity")}</th>
                <th>{t("entityId")}</th>
                <th>{t("auditActor")}</th>
                <th>{t("requestId")}</th>
                <th>{t("ipAddress")}</th>
              </tr>
            </thead>
            <tbody>
              {audit.rows.map((row) => (
                <tr key={row.id}>
                  <td className="mono" dir="ltr">{new Date(row.created_at).toLocaleString()}</td>
                  <td>{row.action}</td>
                  <td>{row.entity_type ?? ""}</td>
                  <td className="mono" dir="ltr">{row.entity_id ?? ""}</td>
                  <td className="mono" dir="ltr">{row.actor_user_id ?? ""}</td>
                  <td className="mono" dir="ltr">{row.request_id ?? ""}</td>
                  <td className="mono" dir="ltr">{row.ip_address ?? ""}</td>
                </tr>
              ))}
              <tr>
                <td colSpan={6}><strong>{t("total")}</strong></td>
                <td className="numeric" dir="ltr"><strong>{audit.total}</strong></td>
              </tr>
            </tbody>
          </table>
        ) : null}

        {kind === "tax" && tax !== null ? (
          <table className="data-table">
            <thead>
              <tr><th>{t("description")}</th><th className="numeric">{t("amount")}</th></tr>
            </thead>
            <tbody>
              <tr><td>{t("vat")} · {t("invoices")}</td><td className="numeric" dir="ltr">{money(tax.output_vat_minor, lang)}</td></tr>
              <tr><td>{t("vat")} · {t("creditNotes")}</td><td className="numeric" dir="ltr">{money(tax.output_credit_notes_minor, lang)}</td></tr>
              <tr><td>{t("vat")} · {t("bill")}</td><td className="numeric" dir="ltr">{money(tax.input_vat_minor, lang)}</td></tr>
              <tr><td>{t("vat")} · {t("debitNotes")}</td><td className="numeric" dir="ltr">{money(tax.input_debit_notes_minor, lang)}</td></tr>
              <tr><td><strong>{t("total")}</strong></td><td className="numeric" dir="ltr"><strong>{money(tax.payable_minor, lang)}</strong></td></tr>
            </tbody>
          </table>
        ) : null}

        {!loading &&
        ((kind === "pnl" && pnl === null) ||
          (kind === "balance" && sheet === null) ||
          ((kind === "ar" || kind === "ap") && aging === null) ||
          (kind === "statement" && statement === null) ||
          (kind === "inventory" && inventory === null) ||
          (kind === "gl" && ledger === null) ||
          (kind === "stock" && stock === null) ||
          (kind === "register" && register === null) ||
          (kind === "audit" && audit === null) ||
          (kind === "tax" && tax === null)) ? (
          <div className="empty-state">{t("noData")}</div>
        ) : null}
      </div>
    </section>
  );
}