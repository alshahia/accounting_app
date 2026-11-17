import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import {
  ApiError,
  changeOwnPassword,
  createCompany,
  createTenantUser,
  listRoles,
  listTenantUsers,
  resetTenantUserPassword,
  setTenantUserActive,
} from "../api";
import type { RoleInfo, TenantUser } from "../api";
import type { Session } from "../App";
import { translations } from "../i18n";
import type { Lang } from "../i18n";

interface AdminPageProps {
  lang: Lang;
  session: Session;
  onRefreshSession: () => Promise<void>;
}

type Tab = "users" | "roles" | "companies";

function copy(lang: Lang, ar: string, en: string): string {
  return lang === "ar" ? ar : en;
}

function errorText(lang: Lang, caught: unknown, fallback: string): string {
  if (caught instanceof ApiError) {
    if (caught.code === "permission_denied") {
      return copy(lang, "لا تملك صلاحية لهذا الإجراء.", "You do not have permission for that.");
    }
    return caught.message;
  }
  return fallback;
}

export function AdminPage({ lang, session, onRefreshSession }: AdminPageProps) {
  const t = (key: keyof (typeof translations)["ar"]): string => translations[lang][key];
  const permissions = session.company.permissions;
  const canManageUsers = permissions.includes("admin.user.manage");
  const canManageRoles = permissions.includes("admin.role.manage");
  const canManageCompanies = permissions.includes("admin.company.manage");

  const [tab, setTab] = useState<Tab>(canManageUsers ? "users" : canManageCompanies ? "companies" : "roles");
  const [users, setUsers] = useState<TenantUser[]>([]);
  const [roles, setRoles] = useState<RoleInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteName, setInviteName] = useState("");
  const [inviteCompany, setInviteCompany] = useState(session.company.id);
  const [inviteRole, setInviteRole] = useState("accountant");
  const [inviting, setInviting] = useState(false);
  const [inviteLink, setInviteLink] = useState<string | null>(null);

  const [newCompanyCode, setNewCompanyCode] = useState("");
  const [newCompanyNameAr, setNewCompanyNameAr] = useState("");
  const [newCompanyNameEn, setNewCompanyNameEn] = useState("");
  const [newCompanyCurrency, setNewCompanyCurrency] = useState(session.company.base_currency);
  const [newCompanyYear, setNewCompanyYear] = useState(String(new Date().getFullYear()));
  const [creatingCompany, setCreatingCompany] = useState(false);

  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [changingPassword, setChangingPassword] = useState(false);

  const reload = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      try {
        const [userRows, roleRows] = await Promise.all([
          canManageUsers ? listTenantUsers(signal) : Promise.resolve([]),
          canManageRoles ? listRoles(signal) : Promise.resolve([]),
        ]);
        setUsers(userRows);
        setRoles(roleRows);
        setError(null);
      } catch (caught) {
        if (caught instanceof ApiError && caught.status !== 401) {
          setError(errorText(lang, caught, t("genericError")));
        }
      } finally {
        setLoading(false);
      }
    },
    [canManageRoles, canManageUsers, lang, t],
  );

  useEffect(() => {
    const controller = new AbortController();
    void reload(controller.signal);
    return () => controller.abort();
  }, [reload, session.company.id]);

  async function invite(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setNotice(null);
    setInviteLink(null);
    if (inviteEmail.trim() === "" || inviteName.trim() === "") {
      setError(t("requiredFields"));
      return;
    }
    setInviting(true);
    try {
      const created = await createTenantUser({
        email: inviteEmail.trim(),
        full_name: inviteName.trim(),
        company_id: inviteCompany,
        role_codes: [inviteRole],
      });
      setInviteEmail("");
      setInviteName("");
      if (created.invitation_token !== null) {
        setInviteLink(
          window.location.origin + "/?invite=" + encodeURIComponent(created.invitation_token),
        );
        setNotice(
          copy(
            lang,
            "أُنشئ المستخدم. أرسل رابط الدعوة التالي إليه؛ يعمل مرة واحدة.",
            "User created. Send this one-time invitation link to the user.",
          ),
        );
      } else {
        setNotice(copy(lang, "أُنشئ المستخدم.", "User created."));
      }
      await reload();
    } catch (caught) {
      setError(errorText(lang, caught, t("genericError")));
    } finally {
      setInviting(false);
    }
  }

  async function toggleActive(user: TenantUser) {
    setError(null);
    try {
      await setTenantUserActive(user.user_id, !user.is_active);
      await reload();
    } catch (caught) {
      setError(errorText(lang, caught, t("genericError")));
    }
  }

  async function sendReset(user: TenantUser) {
    setError(null);
    setNotice(null);
    setInviteLink(null);
    try {
      const reset = await resetTenantUserPassword(user.user_id);
      setInviteLink(
        window.location.origin + "/?invite=" + encodeURIComponent(reset.reset_token),
      );
      setNotice(
        copy(
          lang,
          "أُنشئ رابط إعادة تعيين كلمة المرور؛ يعمل مرة واحدة.",
          "A one-time password-reset link was created.",
        ),
      );
    } catch (caught) {
      setError(errorText(lang, caught, t("genericError")));
    }
  }

  async function submitCompany(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setNotice(null);
    if (newCompanyCode.trim() === "" || newCompanyNameAr.trim() === "") {
      setError(t("requiredFields"));
      return;
    }
    setCreatingCompany(true);
    try {
      await createCompany({
        code: newCompanyCode.trim(),
        name_ar: newCompanyNameAr.trim(),
        name_en: newCompanyNameEn.trim() === "" ? null : newCompanyNameEn.trim(),
        base_currency: newCompanyCurrency.trim().toUpperCase(),
        fiscal_year: Number(newCompanyYear) > 0 ? Number(newCompanyYear) : null,
      });
      setNewCompanyCode("");
      setNewCompanyNameAr("");
      setNewCompanyNameEn("");
      setNotice(copy(lang, "أُنشئت الشركة مع دليل الحسابات.", "Company created with its chart of accounts."));
      await onRefreshSession();
    } catch (caught) {
      setError(errorText(lang, caught, t("genericError")));
    } finally {
      setCreatingCompany(false);
    }
  }

  async function submitPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setNotice(null);
    if (newPassword !== confirmPassword) {
      setError(copy(lang, "كلمتا المرور غير متطابقتين.", "The two passwords do not match."));
      return;
    }
    if (newPassword.length < 10 || !/[0-9]/.test(newPassword)) {
      setError(
        copy(
          lang,
          "كلمة المرور يجب أن تكون 10 أحرف على الأقل وتحتوي رقمًا.",
          "The password must be at least 10 characters and contain a digit.",
        ),
      );
      return;
    }
    setChangingPassword(true);
    try {
      await changeOwnPassword(currentPassword, newPassword);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setNotice(
        copy(
          lang,
          "تم تغيير كلمة المرور وإنهاء الجلسات الأخرى؛ سجّل الدخول من جديد.",
          "Password changed and other sessions revoked; sign in again.",
        ),
      );
    } catch (caught) {
      setError(errorText(lang, caught, t("genericError")));
    } finally {
      setChangingPassword(false);
    }
  }

  const tabs: Tab[] = [];
  if (canManageUsers) {
    tabs.push("users");
  }
  if (canManageRoles) {
    tabs.push("roles");
  }
  if (canManageCompanies) {
    tabs.push("companies");
  }

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h1>{copy(lang, "الإدارة", "Administration")}</h1>
          <p className="muted">
            {copy(
              lang,
              "المستخدمون والأدوار والشركات وتغيير كلمة المرور.",
              "Users, roles, companies and your own password.",
            )}
          </p>
        </div>
      </header>

      {error !== null ? (
        <div className="alert error" role="alert">
          {error}
        </div>
      ) : null}
      {notice !== null ? (
        <div className="alert success" role="status">
          {notice}
        </div>
      ) : null}
      {inviteLink !== null ? (
        <div className="card">
          <label className="field">
            <span>{copy(lang, "الرابط", "Link")}</span>
            <input
              type="text"
              dir="ltr"
              readOnly
              value={inviteLink}
              onFocus={(event) => event.target.select()}
            />
          </label>
        </div>
      ) : null}

      {tabs.length > 1 ? (
        <div className="tabs" role="tablist">
          {tabs.map((item) => (
            <button
              key={item}
              type="button"
              role="tab"
              aria-selected={tab === item}
              className={"tab" + (tab === item ? " active" : "")}
              onClick={() => setTab(item)}
            >
              {item === "users"
                ? copy(lang, "المستخدمون", "Users")
                : item === "roles"
                  ? copy(lang, "الأدوار", "Roles")
                  : copy(lang, "الشركات", "Companies")}
            </button>
          ))}
        </div>
      ) : null}

      {tab === "users" && canManageUsers ? (
        <>
          <form className="card" onSubmit={(event) => void invite(event)} noValidate>
            <h2>{copy(lang, "دعوة مستخدم", "Invite a user")}</h2>
            <div className="form-grid">
              <label className="field">
                <span>{t("email")}</span>
                <input
                  type="email"
                  dir="ltr"
                  value={inviteEmail}
                  onChange={(event) => setInviteEmail(event.target.value)}
                />
              </label>
              <label className="field">
                <span>{copy(lang, "الاسم الكامل", "Full name")}</span>
                <input
                  type="text"
                  value={inviteName}
                  onChange={(event) => setInviteName(event.target.value)}
                />
              </label>
              <label className="field">
                <span>{t("company")}</span>
                <select
                  value={inviteCompany}
                  onChange={(event) => setInviteCompany(event.target.value)}
                >
                  {session.companies.map((company) => (
                    <option key={company.id} value={company.id}>
                      {lang === "ar" ? company.name_ar : company.name_en ?? company.name_ar}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>{copy(lang, "الدور", "Role")}</span>
                <select value={inviteRole} onChange={(event) => setInviteRole(event.target.value)}>
                  {roles
                    .filter((role) => role.code !== "tenant_admin")
                    .map((role) => (
                      <option key={role.code} value={role.code}>
                        {role.name}
                      </option>
                    ))}
                </select>
              </label>
            </div>
            <div className="form-actions">
              <div className="spacer" />
              <button className="button primary" type="submit" disabled={inviting}>
                {inviting ? t("saving") : copy(lang, "إنشاء ودعوة", "Create and invite")}
              </button>
            </div>
          </form>

          <div className="card table-card">
            {loading ? (
              <div className="empty-state">{t("loading")}</div>
            ) : users.length === 0 ? (
              <div className="empty-state">
                {copy(lang, "لا يوجد مستخدمون بعد.", "No users yet.")}
              </div>
            ) : (
              <table className="data-table">
                <thead>
                  <tr>
                    <th>{copy(lang, "الاسم", "Name")}</th>
                    <th>{t("email")}</th>
                    <th>{copy(lang, "الشركات والأدوار", "Companies and roles")}</th>
                    <th>{t("status")}</th>
                    <th>{t("actions")}</th>
                  </tr>
                </thead>
                <tbody>
                  {users.map((user) => (
                    <tr key={user.user_id}>
                      <td>{user.full_name}</td>
                      <td className="mono" dir="ltr">
                        {user.email}
                      </td>
                      <td>
                        {user.companies.map((assignment) => (
                          <div key={assignment.company_id} className="muted">
                            {assignment.role_codes.join(", ")}
                          </div>
                        ))}
                      </td>
                      <td>
                        <span className={"badge " + (user.is_active ? "posted" : "draft")}>
                          {user.is_active ? copy(lang, "نشط", "Active") : copy(lang, "موقوف", "Inactive")}
                        </span>
                      </td>
                      <td className="row-actions">
                        <button
                          type="button"
                          className="button ghost"
                          onClick={() => void toggleActive(user)}
                        >
                          {user.is_active
                            ? copy(lang, "إيقاف", "Deactivate")
                            : copy(lang, "تفعيل", "Activate")}
                        </button>
                        <button
                          type="button"
                          className="button ghost"
                          onClick={() => void sendReset(user)}
                        >
                          {copy(lang, "إعادة تعيين كلمة المرور", "Reset password")}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </>
      ) : null}

      {tab === "roles" && canManageRoles ? (
        <div className="card table-card">
          <table className="data-table">
            <thead>
              <tr>
                <th>{copy(lang, "الرمز", "Code")}</th>
                <th>{copy(lang, "الاسم", "Name")}</th>
                <th>{copy(lang, "نظامي", "System")}</th>
                <th>{copy(lang, "عدد الصلاحيات", "Permissions")}</th>
              </tr>
            </thead>
            <tbody>
              {roles.map((role) => (
                <tr key={role.code}>
                  <td className="mono" dir="ltr">
                    {role.code}
                  </td>
                  <td>{role.name}</td>
                  <td>{role.is_system ? copy(lang, "نعم", "Yes") : copy(lang, "لا", "No")}</td>
                  <td className="numeric" dir="ltr">
                    {role.permission_count}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {tab === "companies" && canManageCompanies ? (
        <>
          <div className="card table-card">
            <table className="data-table">
              <thead>
                <tr>
                  <th>{copy(lang, "الرمز", "Code")}</th>
                  <th>{copy(lang, "الاسم", "Name")}</th>
                  <th>{copy(lang, "العملة", "Currency")}</th>
                </tr>
              </thead>
              <tbody>
                {session.companies.map((company) => (
                  <tr key={company.id}>
                    <td className="mono" dir="ltr">
                      {company.code}
                    </td>
                    <td>{lang === "ar" ? company.name_ar : company.name_en ?? company.name_ar}</td>
                    <td dir="ltr">{company.base_currency}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <form className="card" onSubmit={(event) => void submitCompany(event)} noValidate>
            <h2>{copy(lang, "شركة جديدة", "New company")}</h2>
            <div className="form-grid">
              <label className="field">
                <span>{copy(lang, "الرمز", "Code")}</span>
                <input
                  type="text"
                  dir="ltr"
                  value={newCompanyCode}
                  onChange={(event) => setNewCompanyCode(event.target.value)}
                />
              </label>
              <label className="field">
                <span>{copy(lang, "الاسم بالعربية", "Arabic name")}</span>
                <input
                  type="text"
                  value={newCompanyNameAr}
                  onChange={(event) => setNewCompanyNameAr(event.target.value)}
                />
              </label>
              <label className="field">
                <span>{copy(lang, "الاسم بالإنجليزية", "English name")}</span>
                <input
                  type="text"
                  dir="ltr"
                  value={newCompanyNameEn}
                  onChange={(event) => setNewCompanyNameEn(event.target.value)}
                />
              </label>
              <label className="field">
                <span>{copy(lang, "العملة الأساسية", "Base currency")}</span>
                <input
                  type="text"
                  dir="ltr"
                  maxLength={3}
                  value={newCompanyCurrency}
                  onChange={(event) => setNewCompanyCurrency(event.target.value)}
                />
              </label>
              <label className="field">
                <span>{copy(lang, "السنة المالية", "Fiscal year")}</span>
                <input
                  type="number"
                  dir="ltr"
                  min="2000"
                  max="2200"
                  value={newCompanyYear}
                  onChange={(event) => setNewCompanyYear(event.target.value)}
                />
              </label>
            </div>
            <div className="form-actions">
              <div className="spacer" />
              <button className="button primary" type="submit" disabled={creatingCompany}>
                {creatingCompany
                  ? t("saving")
                  : copy(lang, "إنشاء الشركة", "Create company")}
              </button>
            </div>
          </form>
        </>
      ) : null}

      <form className="card" onSubmit={(event) => void submitPassword(event)} noValidate>
        <h2>{copy(lang, "كلمة مروري", "My password")}</h2>
        <div className="form-grid">
          <label className="field">
            <span>{copy(lang, "كلمة المرور الحالية", "Current password")}</span>
            <input
              type="password"
              dir="ltr"
              autoComplete="current-password"
              value={currentPassword}
              onChange={(event) => setCurrentPassword(event.target.value)}
            />
          </label>
          <label className="field">
            <span>{copy(lang, "كلمة المرور الجديدة", "New password")}</span>
            <input
              type="password"
              dir="ltr"
              autoComplete="new-password"
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
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
        </div>
        <div className="form-actions">
          <div className="spacer" />
          <button className="button primary" type="submit" disabled={changingPassword}>
            {changingPassword ? t("saving") : copy(lang, "تغيير كلمة المرور", "Change password")}
          </button>
        </div>
      </form>
    </section>
  );
}
