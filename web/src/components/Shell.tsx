import type { ReactNode } from "react";

import type { Session } from "../App";
import { translations } from "../i18n";
import type { Lang } from "../i18n";

export type Page =
  | "invoices"
  | "purchases"
  | "receipts"
  | "payments"
  | "cheques"
  | "transfers"
  | "reconciliation"
  | "returns"
  | "journal"
  | "reports"
  | "trial"
  | "inventory"
  | "admin"
  | "tools";

interface ShellProps {
  lang: Lang;
  onLangChange: (lang: Lang) => void;
  session: Session;
  page: Page;
  onPageChange: (page: Page) => void;
  onCompanyChange: (companyId: string) => void;
  onLogout: () => void;
  children: ReactNode;
}

export function Shell({
  lang,
  onLangChange,
  session,
  page,
  onPageChange,
  onCompanyChange,
  onLogout,
  children,
}: ShellProps) {
  const t = (key: keyof (typeof translations)["ar"]): string => translations[lang][key];
  const companyName = lang === "ar" ? session.company.name_ar : session.company.name_en ?? session.company.name_ar;
  const canAdminister = ["admin.user.manage", "admin.role.manage", "admin.company.manage"].some(
    (permission) => session.company.permissions.includes(permission),
  );

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <span className="brand-mark small">المحمود</span>
          <span className="sidebar-tagline">{t("appTagline")}</span>
        </div>
        <nav className="nav" aria-label="Main">
          <button
            type="button"
            className={`nav-item ${page === "invoices" ? "active" : ""}`}
            onClick={() => onPageChange("invoices")}
          >
            <span className="nav-glyph" aria-hidden="true">▤</span>
            {t("invoices")}
          </button>
          <button
            type="button"
            className={`nav-item ${page === "purchases" ? "active" : ""}`}
            onClick={() => onPageChange("purchases")}
          >
            <span className="nav-glyph" aria-hidden="true">⇣</span>
            {t("bills")}
          </button>
          <button
            type="button"
            className={`nav-item ${page === "receipts" ? "active" : ""}`}
            onClick={() => onPageChange("receipts")}
          >
            <span className="nav-glyph" aria-hidden="true">↙</span>
            {t("receipts")}
          </button>
          <button
            type="button"
            className={`nav-item ${page === "payments" ? "active" : ""}`}
            onClick={() => onPageChange("payments")}
          >
            <span className="nav-glyph" aria-hidden="true">↗</span>
            {t("payments")}
          </button>
          <button
            type="button"
            className={`nav-item ${page === "cheques" ? "active" : ""}`}
            onClick={() => onPageChange("cheques")}
          >
            <span className="nav-glyph" aria-hidden="true">▭</span>
            {t("cheques")}
          </button>
          <button
            type="button"
            className={`nav-item ${page === "transfers" ? "active" : ""}`}
            onClick={() => onPageChange("transfers")}
          >
            <span className="nav-glyph" aria-hidden="true">⇄</span>
            {t("transfers")}
          </button>
          <button
            type="button"
            className={`nav-item ${page === "reconciliation" ? "active" : ""}`}
            onClick={() => onPageChange("reconciliation")}
          >
            <span className="nav-glyph" aria-hidden="true">☑</span>
            {t("reconciliation")}
          </button>
          <button
            type="button"
            className={`nav-item ${page === "returns" ? "active" : ""}`}
            onClick={() => onPageChange("returns")}
          >
            <span className="nav-glyph" aria-hidden="true">↩</span>
            {t("returns")}
          </button>
          <button
            type="button"
            className={`nav-item ${page === "journal" ? "active" : ""}`}
            onClick={() => onPageChange("journal")}
          >
            <span className="nav-glyph" aria-hidden="true">☰</span>
            {t("journalEntries")}
          </button>
          <button
            type="button"
            className={`nav-item ${page === "reports" ? "active" : ""}`}
            onClick={() => onPageChange("reports")}
          >
            <span className="nav-glyph" aria-hidden="true">▦</span>
            {t("reports")}
          </button>
          <button
            type="button"
            className={`nav-item ${page === "trial" ? "active" : ""}`}
            onClick={() => onPageChange("trial")}
          >
            <span className="nav-glyph" aria-hidden="true">∑</span>
            {t("trialBalance")}
          </button>
          <button
            type="button"
            className={`nav-item ${page === "inventory" ? "active" : ""}`}
            onClick={() => onPageChange("inventory")}
          >
            <span className="nav-glyph" aria-hidden="true">▥</span>
            {t("inventoryNav")}
          </button>
          {canAdminister ? (
            <button
              type="button"
              className={`nav-item ${page === "admin" ? "active" : ""}`}
              onClick={() => onPageChange("admin")}
            >
              <span className="nav-glyph" aria-hidden="true">☷</span>
              {t("administration")}
            </button>
          ) : null}
          <button
            type="button"
            className={`nav-item ${page === "tools" ? "active" : ""}`}
            onClick={() => onPageChange("tools")}
          >
            <span className="nav-glyph" aria-hidden="true">⚙</span>
            {t("tools")}
          </button>
        </nav>
        <div className="sidebar-foot">
          <div className="user-chip">
            <span className="avatar">{session.user.full_name.slice(0, 1)}</span>
            <span className="user-meta">
              <strong>{session.user.full_name}</strong>
              <small dir="ltr">{session.user.email}</small>
            </span>
          </div>
          <button type="button" className="link-button" onClick={onLogout}>
            {t("logout")}
          </button>
        </div>
      </aside>

      <div className="workspace">
        <header className="topbar">
          <div className="topbar-left">
            <span className="topbar-label">{t("company")}</span>
            <select
              className="company-select"
              value={session.company.id}
              onChange={(event) => onCompanyChange(event.target.value)}
              disabled={session.companies.length < 2}
              aria-label={t("company")}
            >
              {session.companies.map((company) => (
                <option key={company.id} value={company.id}>
                  {lang === "ar" ? company.name_ar : company.name_en ?? company.name_ar}
                </option>
              ))}
            </select>
            <span className="company-code" dir="ltr">{session.company.code}</span>
          </div>
          <div className="topbar-right">
            <span className="company-base">{companyName} · {session.company.base_currency}</span>
            <button type="button" className="button ghost" onClick={() => onLangChange(lang === "ar" ? "en" : "ar")}>
              {t("language")}
            </button>
          </div>
        </header>
        <main className="content">{children}</main>
      </div>
    </div>
  );
}
