/** Small formatting helpers shared by the phase-1 screens. */

import { ApiError } from "../api";
import { translations } from "../i18n";
import type { Lang } from "../i18n";

export function money(minor: number, lang: Lang): string {
  return new Intl.NumberFormat(lang === "ar" ? "ar-IQ" : "en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(minor / 100);
}

export function today(): string {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return now.getFullYear() + "-" + month + "-" + day;
}

export function anyText(lang: Lang, nameAr: string, nameEn: string | null): string {
  return lang === "ar" ? nameAr : nameEn ?? nameAr;
}

const ERROR_TEXT: Record<string, { ar: string; en: string }> = {
  invalid_credentials: { ar: "بيانات الدخول غير صحيحة.", en: "The sign-in details are not correct." },
  unknown_email: { ar: "لا يوجد حساب نشط بهذا البريد في الشركة المحددة.", en: "No active account uses this email in the selected company." },
  account_locked: { ar: "الحساب مقفل مؤقتًا بعد محاولات فاشلة. حاول لاحقًا.", en: "The account is temporarily locked after failed attempts. Try again later." },
  tenant_required: { ar: "اختر الشركة قبل تسجيل الدخول.", en: "Choose the company before signing in." },
  rate_limited: { ar: "محاولات كثيرة في وقت قصير. انتظر دقيقة ثم أعد المحاولة.", en: "Too many attempts in a short time. Wait a minute and retry." },
  csrf_failed: { ar: "انتهت صلاحية الجلسة. أعد تحميل الصفحة.", en: "The session token expired. Reload the page." },
  not_authenticated: { ar: "انتهت الجلسة. سجّل الدخول من جديد.", en: "The session ended. Sign in again." },
  forbidden: { ar: "لا تملك صلاحية تنفيذ هذا الإجراء.", en: "You do not have permission for this action." },
  setup_completed: { ar: "تم تهيئة النظام مسبقًا.", en: "This installation is already provisioned." },
  setup_token_invalid: { ar: "رمز التهيئة غير صحيح.", en: "The bootstrap token is incorrect." },
  credential_used: { ar: "استُخدم رابط الدعوة مسبقًا. اطلب رابطًا جديدًا.", en: "This invitation link was already used. Request a new one." },
  weak_password: { ar: "كلمة المرور قصيرة أو لا تحتوي رقمًا.", en: "The password is too short or has no digit." },
  auth_required: { ar: "سجّل الدخول للمتابعة.", en: "Sign in to continue." },
  network_error: { ar: "تعذّر الوصول إلى الخادم. تحقّق من الاتصال ثم أعد المحاولة.", en: "The server is unreachable. Check the connection and try again." },
  internal_error: { ar: "حدث خطأ في الخادم. أعد المحاولة.", en: "The server returned an error. Try again." },
};

/** AC-24 / localized text: prefer the screen's language over the raw API detail. */
export function errorText(error: unknown, lang: Lang): string {
  if (error instanceof ApiError) {
    const mapped = ERROR_TEXT[error.code];
    if (mapped !== undefined) return mapped[lang];
    return error.message;
  }
  if (error instanceof Error && error.message !== "") return error.message;
  return translations[lang].loadFailed;
}
