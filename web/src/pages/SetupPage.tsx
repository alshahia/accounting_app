import { useState } from "react";
import type { FormEvent } from "react";

import { ApiError, runSetup } from "../api";
import { translations } from "../i18n";
import { errorText } from "./utils";
import type { Lang } from "../i18n";

interface SetupPageProps {
  lang: Lang;
  onLangChange: (lang: Lang) => void;
  onComplete: () => void;
}

function copy(lang: Lang, ar: string, en: string): string {
  return lang === "ar" ? ar : en;
}

function fiscalYearNow(): string {
  return String(new Date().getFullYear());
}

export function SetupPage({ lang, onLangChange, onComplete }: SetupPageProps) {
  const t = (key: keyof (typeof translations)["ar"]): string => translations[lang][key];
  const [tenantName, setTenantName] = useState("");
  const [tenantSlug, setTenantSlug] = useState("");
  const [companyCode, setCompanyCode] = useState("MAIN");
  const [companyNameAr, setCompanyNameAr] = useState("");
  const [companyNameEn, setCompanyNameEn] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [adminEmail, setAdminEmail] = useState("");
  const [adminName, setAdminName] = useState("");
  const [adminPassword, setAdminPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [bootstrapToken, setBootstrapToken] = useState("");
  const [requireMfa, setRequireMfa] = useState(true);
  const [fiscalYear, setFiscalYear] = useState(fiscalYearNow);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    if (
      tenantName.trim() === "" ||
      tenantSlug.trim() === "" ||
      companyNameAr.trim() === "" ||
      adminEmail.trim() === "" ||
      adminName.trim() === "" ||
      adminPassword === "" ||
      bootstrapToken.trim() === ""
    ) {
      setError(copy(lang, "أكمل جميع الحقول المطلوبة.", "Fill in every required field."));
      return;
    }
    if (adminPassword !== confirmPassword) {
      setError(copy(lang, "كلمتا المرور غير متطابقتين.", "The two passwords do not match."));
      return;
    }
    if (adminPassword.length < 10 || !/[0-9]/.test(adminPassword)) {
      setError(
        copy(
          lang,
          "كلمة المرور يجب أن تكون 10 أحرف على الأقل وتحتوي رقمًا.",
          "The password must be at least 10 characters and contain a digit.",
        ),
      );
      return;
    }
    setSubmitting(true);
    try {
      await runSetup(
        {
          tenant_name: tenantName.trim(),
          tenant_slug: tenantSlug.trim(),
          company_code: companyCode.trim(),
          company_name_ar: companyNameAr.trim(),
          company_name_en: companyNameEn.trim() === "" ? null : companyNameEn.trim(),
          base_currency: currency.trim().toUpperCase(),
          admin_email: adminEmail.trim(),
          admin_full_name: adminName.trim(),
          admin_password: adminPassword,
          require_mfa: requireMfa,
          fiscal_year: Number(fiscalYear) > 0 ? Number(fiscalYear) : null,
        },
        bootstrapToken.trim(),
      );
      onComplete();
    } catch (caught) {
      if (caught instanceof ApiError) {
        if (caught.code === "setup_token_invalid" || caught.code === "setup_not_available") {
          setError(
            copy(
              lang,
              "رمز التثبيت غير صحيح أو غير مهيأ على الخادم.",
              "The bootstrap token is wrong or not configured on the server.",
            ),
          );
        } else if (caught.code === "setup_completed") {
          setError(
            copy(
              lang,
              "تمت تهيئة هذا التثبيت مسبقًا؛ سجّل الدخول للمتابعة.",
              "This installation is already provisioned; sign in to continue.",
            ),
          );
        } else {
          setError(errorText(caught, lang));
        }
      } else {
        setError(t("genericError"));
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="login-page">
      <aside className="login-aside">
        <div className="login-aside-glow" aria-hidden="true" />
        <div className="brand-lockup">
          <span className="brand-mark">المحمود</span>
          <span className="brand-sub">{t("appTagline")}</span>
        </div>
        <div className="login-aside-copy">
          <h1>{copy(lang, "تهيئة التثبيت", "Installation setup")}</h1>
          <p>
            {copy(
              lang,
              "أنشئ الشركة الأولى ومدير النظام. تُنشأ بقية الشركات والمستخدمين من شاشة الإدارة بعد الدخول.",
              "Create the first company and the administrator. Further companies and users are created from the admin screen after sign-in.",
            )}
          </p>
          <ul>
            <li>{copy(lang, "لا حاجة لتشغيل SQL أو بذور تجريبية", "No SQL scripts and no demo seed needed")}</li>
            <li>{copy(lang, "شجرة حسابات وسنة مالية تُنشأ تلقائيًا", "Chart of accounts and fiscal year are created automatically")}</li>
            <li>{copy(lang, "محمي برمز تثبيت يحتفظ به المشغّل", "Protected by an operator-held bootstrap token")}</li>
          </ul>
        </div>
        <span className="login-aside-foot">Al-Mahmoud ERP</span>
      </aside>

      <main className="login-main">
        <div className="login-card wide-card">
          <div className="login-lang">
            <button
              type="button"
              className="link-button"
              onClick={() => onLangChange(lang === "ar" ? "en" : "ar")}
            >
              {t("language")}
            </button>
          </div>
          <h2>{copy(lang, "أول تشغيل", "First run")}</h2>
          <p className="muted">
            {copy(
              lang,
              "هذه الشاشة تظهر فقط عندما لا يوجد أي مستأجر بعد.",
              "This screen appears only while the installation has no tenant.",
            )}
          </p>
          <form onSubmit={(event) => void submit(event)} noValidate>
            <div className="form-grid">
              <label className="field">
                <span>{copy(lang, "اسم المؤسسة", "Tenant name")}</span>
                <input
                  type="text"
                  value={tenantName}
                  onChange={(event) => setTenantName(event.target.value)}
                />
              </label>
              <label className="field">
                <span>{copy(lang, "المعرّف اللاتيني", "Tenant slug")}</span>
                <input
                  type="text"
                  dir="ltr"
                  placeholder="almahmoud-company"
                  value={tenantSlug}
                  onChange={(event) => setTenantSlug(event.target.value)}
                />
              </label>
              <label className="field">
                <span>{copy(lang, "رمز الشركة", "Company code")}</span>
                <input
                  type="text"
                  dir="ltr"
                  value={companyCode}
                  onChange={(event) => setCompanyCode(event.target.value)}
                />
              </label>
              <label className="field">
                <span>{copy(lang, "اسم الشركة (عربي)", "Company name (Arabic)")}</span>
                <input
                  type="text"
                  value={companyNameAr}
                  onChange={(event) => setCompanyNameAr(event.target.value)}
                />
              </label>
              <label className="field">
                <span>{copy(lang, "اسم الشركة (إنجليزي)", "Company name (English)")}</span>
                <input
                  type="text"
                  dir="ltr"
                  value={companyNameEn}
                  onChange={(event) => setCompanyNameEn(event.target.value)}
                />
              </label>
              <label className="field">
                <span>{copy(lang, "العملة الأساسية", "Base currency")}</span>
                <input
                  type="text"
                  dir="ltr"
                  maxLength={3}
                  value={currency}
                  onChange={(event) => setCurrency(event.target.value)}
                />
              </label>
              <label className="field">
                <span>{copy(lang, "السنة المالية", "Fiscal year")}</span>
                <input
                  type="number"
                  dir="ltr"
                  min="2000"
                  max="2200"
                  value={fiscalYear}
                  onChange={(event) => setFiscalYear(event.target.value)}
                />
              </label>
              <label className="field">
                <span>{copy(lang, "بريد مدير النظام", "Administrator email")}</span>
                <input
                  type="email"
                  dir="ltr"
                  autoComplete="username"
                  value={adminEmail}
                  onChange={(event) => setAdminEmail(event.target.value)}
                />
              </label>
              <label className="field">
                <span>{copy(lang, "اسم مدير النظام", "Administrator name")}</span>
                <input
                  type="text"
                  value={adminName}
                  onChange={(event) => setAdminName(event.target.value)}
                />
              </label>
              <label className="field">
                <span>{copy(lang, "كلمة مرور المدير", "Administrator password")}</span>
                <input
                  type="password"
                  dir="ltr"
                  autoComplete="new-password"
                  value={adminPassword}
                  onChange={(event) => setAdminPassword(event.target.value)}
                />
              </label>
              <label className="field">
                <span>{copy(lang, "تأكيد كلمة المرور", "Confirm password")}</span>
                <input
                  type="password"
                  dir="ltr"
                  autoComplete="new-password"
                  value={confirmPassword}
                  onChange={(event) => setConfirmPassword(event.target.value)}
                />
              </label>
              <label className="field">
                <span>{copy(lang, "رمز التثبيت", "Bootstrap token")}</span>
                <input
                  type="password"
                  dir="ltr"
                  autoComplete="off"
                  value={bootstrapToken}
                  onChange={(event) => setBootstrapToken(event.target.value)}
                />
              </label>
            </div>
            <label className="check-field">
              <input
                type="checkbox"
                checked={requireMfa}
                onChange={(event) => setRequireMfa(event.target.checked)}
              />
              <span>
                {copy(
                  lang,
                  "إلزام التحقق بخطوتين لعمليات الترحيل والإدارة",
                  "Require two-factor authentication for posting and admin operations",
                )}
              </span>
            </label>
            {error !== null ? (
              <div className="form-error" role="alert">
                {error}
              </div>
            ) : null}
            <button className="button primary block" type="submit" disabled={submitting}>
              {submitting
                ? copy(lang, "جارٍ التهيئة…", "Provisioning…")
                : copy(lang, "تهيئة التثبيت", "Set up this installation")}
            </button>
          </form>
        </div>
      </main>
    </div>
  );
}
