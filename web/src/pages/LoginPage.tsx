import { useState } from "react";
import type { FormEvent } from "react";

import { ApiError } from "../api";
import { translations } from "../i18n";
import { errorText } from "./utils";
import type { Lang, MessageKey } from "../i18n";

interface LoginPageProps {
  lang: Lang;
  sessionExpired?: boolean;
  onLangChange: (lang: Lang) => void;
  onLogin: (
    email: string,
    password: string,
    tenant: string,
    totpCode: string | null,
  ) => Promise<void>;
}

export function LoginPage({ lang, sessionExpired = false, onLangChange, onLogin }: LoginPageProps) {
  const t = (key: MessageKey): string => translations[lang][key];
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [tenant, setTenant] = useState("");
  const [totpCode, setTotpCode] = useState("");
  const [mfaRequired, setMfaRequired] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (email.trim() === "" || password === "") {
      setError(t("requiredFields"));
      return;
    }
    if (mfaRequired && totpCode.trim() === "") {
      setError(t("mfaCodeRequired"));
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onLogin(email.trim(), password, tenant, mfaRequired ? totpCode.trim() : null);
    } catch (caught) {
      if (caught instanceof ApiError) {
        if (caught.code === "totp_required") {
          setMfaRequired(true);
          setError(t("mfaCodeRequired"));
          return;
        }
        if (mfaRequired && caught.code === "totp_invalid") {
          setError(t("mfaCodeInvalid"));
          return;
        }
        setError(
          caught.status === 401
            ? `${t("signIn")}: ${errorText(caught, lang)}`
            : errorText(caught, lang),
        );
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
          <h1>{t("appName")}</h1>
          <p>{t("appTagline")}</p>
          <ul>
            <li>قيود محاسبية متوازنة ومحمية</li>
            <li>فواتير مبيعات ومشتريات بالعربية</li>
            <li>تقارير ميزان المراجعة PDF و Excel</li>
          </ul>
        </div>
        <span className="login-aside-foot">Al-Mahmoud ERP</span>
      </aside>

      <main className="login-main">
        <div className="login-card">
          <div className="login-lang">
            <button type="button" className="link-button" onClick={() => onLangChange(lang === "ar" ? "en" : "ar")}>
              {t("language")}
            </button>
          </div>
          <h2>{t("signIn")}</h2>
          <p className="muted">{t("signInHint")}</p>
          <form onSubmit={(event) => void submit(event)} noValidate>
            <label className="field">
              <span>{t("email")}</span>
              <input
                type="email"
                name="email"
                autoComplete="username"
                dir="ltr"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
              />
            </label>
            <label className="field">
              <span>{t("password")}</span>
              <input
                type="password"
                name="password"
                autoComplete="current-password"
                dir="ltr"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
            </label>
            <label className="field">
              <span>{t("tenant")}</span>
              <input
                type="text"
                name="tenant"
                dir="ltr"
                value={tenant}
                onChange={(event) => setTenant(event.target.value)}
              />
            </label>
            {mfaRequired ? (
              <label className="field">
                <span>{t("mfaCodeOrRecovery")}</span>
                <input
                  type="text"
                  name="totp_code"
                  autoComplete="one-time-code"
                  dir="ltr"
                  maxLength={32}
                  value={totpCode}
                  onChange={(event) => setTotpCode(event.target.value)}
                />
              </label>
            ) : null}
            {error === null && sessionExpired ? (
              <div className="form-error" role="status">
                {t("sessionExpired")}
              </div>
            ) : null}
            {error !== null ? (
              <div className="form-error" role="alert">
                {error}
              </div>
            ) : null}
            <button className="button primary block" type="submit" disabled={submitting}>
              {submitting ? t("loggingIn") : t("login")}
            </button>
          </form>
        </div>
      </main>
    </div>
  );
}
