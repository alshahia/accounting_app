import { useState } from "react";
import type { FormEvent } from "react";

import { ApiError, acceptInvitation } from "../api";
import { translations } from "../i18n";
import { errorText } from "./utils";
import type { Lang } from "../i18n";

interface AcceptInvitationPageProps {
  lang: Lang;
  token: string;
  onLangChange: (lang: Lang) => void;
  onComplete: () => void;
}

function copy(lang: Lang, ar: string, en: string): string {
  return lang === "ar" ? ar : en;
}

export function AcceptInvitationPage({
  lang,
  token,
  onLangChange,
  onComplete,
}: AcceptInvitationPageProps) {
  const t = (key: keyof (typeof translations)["ar"]): string => translations[lang][key];
  const [linkToken, setLinkToken] = useState(token);
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    if (linkToken.trim() === "") {
      setError(copy(lang, "الصق رابط الدعوة أو رمزه.", "Paste the invitation link or its token."));
      return;
    }
    if (password !== confirmPassword) {
      setError(copy(lang, "كلمتا المرور غير متطابقتين.", "The two passwords do not match."));
      return;
    }
    if (password.length < 10 || !/[0-9]/.test(password)) {
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
      await acceptInvitation(linkToken.trim(), password);
      setDone(true);
    } catch (caught) {
      if (caught instanceof ApiError) {
        if (caught.code === "credential_used") {
          setError(
            copy(
              lang,
              "تم استخدام هذا الرابط مسبقًا. اطلب رابطًا جديدًا من الإدارة.",
              "This link was already used. Ask an administrator for a new one.",
            ),
          );
        } else if (caught.status === 401) {
          setError(
            copy(
              lang,
              "الرابط غير صالح أو منتهي الصلاحية.",
              "The link is invalid or has expired.",
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
          <h1>{copy(lang, "تفعيل الحساب", "Activate your account")}</h1>
          <p>
            {copy(
              lang,
              "اختر كلمة مرور جديدة لتفعيل حسابك. الرابط يُستخدم مرة واحدة فقط.",
              "Choose a new password to activate your account. The link works once only.",
            )}
          </p>
        </div>
        <span className="login-aside-foot">Al-Mahmoud ERP</span>
      </aside>
      <main className="login-main">
        <div className="login-card">
          <div className="login-lang">
            <button
              type="button"
              className="link-button"
              onClick={() => onLangChange(lang === "ar" ? "en" : "ar")}
            >
              {t("language")}
            </button>
          </div>
          <h2>{copy(lang, "تعيين كلمة المرور", "Set your password")}</h2>
          {done ? (
            <>
              <div className="alert success" role="status">
                {copy(
                  lang,
                  "تم تفعيل الحساب. سجّل الدخول بكلمة المرور الجديدة.",
                  "Your account is active. Sign in with the new password.",
                )}
              </div>
              <button type="button" className="button primary block" onClick={onComplete}>
                {copy(lang, "الانتقال لتسجيل الدخول", "Go to sign-in")}
              </button>
            </>
          ) : (
            <form onSubmit={(event) => void submit(event)} noValidate>
              <label className="field">
                <span>{copy(lang, "رابط الدعوة", "Invitation token")}</span>
                <input
                  type="text"
                  dir="ltr"
                  value={linkToken}
                  onChange={(event) => setLinkToken(event.target.value)}
                />
              </label>
              <label className="field">
                <span>{copy(lang, "كلمة المرور الجديدة", "New password")}</span>
                <input
                  type="password"
                  dir="ltr"
                  autoComplete="new-password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
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
              {error !== null ? (
                <div className="form-error" role="alert">
                  {error}
                </div>
              ) : null}
              <button className="button primary block" type="submit" disabled={submitting}>
                {submitting
                  ? copy(lang, "جارٍ التفعيل…", "Activating…")
                  : copy(lang, "تفعيل الحساب", "Activate account")}
              </button>
            </form>
          )}
        </div>
      </main>
    </div>
  );
}
