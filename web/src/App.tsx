import { useCallback, useEffect, useState } from "react";

import { ApiError, hasAccessToken, login, logout, me, setCompanyId, setupStatus } from "./api";
import type { ApiUser, Company } from "./api";
import { direction } from "./i18n";
import type { Lang } from "./i18n";
import { AcceptInvitationPage } from "./pages/AcceptInvitationPage";
import { AdminPage } from "./pages/AdminPage";
import { BillsPage } from "./pages/BillsPage";
import { InvoicesPage } from "./pages/InvoicesPage";
import { InventoryPage } from "./pages/InventoryPage";
import { JournalPage } from "./pages/JournalPage";
import { LoginPage } from "./pages/LoginPage";
import { SetupPage } from "./pages/SetupPage";
import { ChequesPage } from "./pages/ChequesPage";
import { PaymentsPage } from "./pages/PaymentsPage";
import { ReceiptsPage } from "./pages/ReceiptsPage";
import { ReconciliationPage } from "./pages/ReconciliationPage";
import { TransfersPage } from "./pages/TransfersPage";
import { ReportsPage } from "./pages/ReportsPage";
import { ReturnsPage } from "./pages/ReturnsPage";
import { ToolsPage } from "./pages/ToolsPage";
import { TrialBalancePage } from "./pages/TrialBalancePage";
import { Shell } from "./components/Shell";
import type { Page } from "./components/Shell";

export interface Session {
  user: ApiUser;
  companies: Company[];
  company: Company;
}

const LANG_KEY = "almahmoud.lang";

function initialLang(): Lang {
  const stored = localStorage.getItem(LANG_KEY);
  return stored === "en" ? "en" : "ar";
}

function chooseCompany(companies: Company[]): Company | null {
  if (companies.length === 0) {
    return null;
  }
  const remembered = sessionStorage.getItem("almahmoud.company_id");
  return companies.find((company) => company.id === remembered) ?? companies[0];
}

export function App() {
  const [lang, setLang] = useState<Lang>(initialLang);
  const [session, setSession] = useState<Session | null>(null);
  const [booting, setBooting] = useState(true);
  const [sessionExpired, setSessionExpired] = useState(false);
  const [setupRequired, setSetupRequired] = useState(false);
  const [inviteToken, setInviteToken] = useState<string | null>(() =>
    new URLSearchParams(window.location.search).get("invite"),
  );
  const [page, setPage] = useState<Page>("invoices");
  const [journalEntryId, setJournalEntryId] = useState<string | null>(null);

  useEffect(() => {
    document.documentElement.lang = lang;
    document.documentElement.dir = direction[lang];
    localStorage.setItem(LANG_KEY, lang);
  }, [lang]);

  useEffect(() => {
    const onUnauthorized = () => {
      setSessionExpired(true);
      setSession(null);
    };
    window.addEventListener("almahmoud:unauthorized", onUnauthorized);
    return () => window.removeEventListener("almahmoud:unauthorized", onUnauthorized);
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function restore() {
      // A brand-new installation has no tenant, so there is nobody to sign in
      // as: the operator goes through the first-run screen instead.
      let needsSetup = false;
      try {
        const status = await setupStatus();
        needsSetup = status.needs_setup;
      } catch {
        // An unreachable status endpoint falls through to the sign-in screen.
      }
      if (cancelled) {
        return;
      }
      if (needsSetup) {
        setSetupRequired(true);
        setBooting(false);
        return;
      }
      if (!hasAccessToken()) {
        setBooting(false);
        return;
      }
      try {
        const identity = await me();
        if (cancelled) {
          return;
        }
        const company = chooseCompany(identity.companies);
        if (company === null) {
          setBooting(false);
          return;
        }
        setCompanyId(company.id);
        setSession({ user: identity.user, companies: identity.companies, company });
      } catch {
        // A stale token simply returns the visitor to the login screen.
      } finally {
        if (!cancelled) {
          setBooting(false);
        }
      }
    }
    void restore();
    return () => {
      cancelled = true;
    };
  }, []);

  const handleLogin = useCallback(
    async (email: string, password: string, tenant: string, totpCode: string | null) => {
      await login(email, password, tenant, totpCode);
      setSessionExpired(false);
      const identity = await me();
      const company = chooseCompany(identity.companies);
      if (company === null) {
        throw new ApiError(403, "no_company", "This account has no company membership.");
      }
      setCompanyId(company.id);
      setSession({ user: identity.user, companies: identity.companies, company });
    },
    [],
  );

  const handleLogout = useCallback(async () => {
    await logout();
    setSession(null);
  }, []);

  const handleRefreshSession = useCallback(async () => {
    const identity = await me();
    const company = chooseCompany(identity.companies);
    if (company === null) {
      return;
    }
    setCompanyId(company.id);
    setSession((current) =>
      current === null
        ? current
        : { user: identity.user, companies: identity.companies, company },
    );
  }, []);

  const finishInvitation = useCallback(() => {
    const url = new URL(window.location.href);
    url.searchParams.delete("invite");
    window.history.replaceState({}, "", url.toString());
    setInviteToken(null);
  }, []);

  const handleCompanyChange = useCallback(
    (companyId: string) => {
      if (session === null) {
        return;
      }
      const company = session.companies.find((candidate) => candidate.id === companyId);
      if (company === undefined) {
        return;
      }
      setCompanyId(company.id);
      setSession({ ...session, company });
    },
    [session],
  );

  if (booting) {
    return (
      <div className="boot-screen">
        <div className="brand-mark">المحمود</div>
        <div className="boot-pulse" aria-hidden="true" />
      </div>
    );
  }

  if (setupRequired) {
    return (
      <SetupPage lang={lang} onLangChange={setLang} onComplete={() => setSetupRequired(false)} />
    );
  }

  if (inviteToken !== null) {
    return (
      <AcceptInvitationPage
        lang={lang}
        token={inviteToken}
        onLangChange={setLang}
        onComplete={finishInvitation}
      />
    );
  }

  if (session === null) {
    return (
      <LoginPage
        lang={lang}
        sessionExpired={sessionExpired}
        onLangChange={setLang}
        onLogin={handleLogin}
      />
    );
  }

  return (
    <Shell
      lang={lang}
      onLangChange={setLang}
      session={session}
      page={page}
      onPageChange={(next) => {
        setJournalEntryId(null);
        setPage(next);
      }}
      onCompanyChange={handleCompanyChange}
      onLogout={() => void handleLogout()}
    >
      {page === "invoices" ? (
        <InvoicesPage lang={lang} company={session.company} />
      ) : page === "purchases" ? (
        <BillsPage lang={lang} company={session.company} />
      ) : page === "receipts" ? (
        <ReceiptsPage lang={lang} company={session.company} />
      ) : page === "payments" ? (
        <PaymentsPage lang={lang} company={session.company} />
      ) : page === "cheques" ? (
        <ChequesPage lang={lang} company={session.company} />
      ) : page === "transfers" ? (
        <TransfersPage lang={lang} company={session.company} />
      ) : page === "reconciliation" ? (
        <ReconciliationPage lang={lang} company={session.company} />
      ) : page === "returns" ? (
        <ReturnsPage lang={lang} company={session.company} />
      ) : page === "journal" ? (
        <JournalPage
          lang={lang}
          company={session.company}
          initialEntryId={journalEntryId}
        />
      ) : page === "reports" ? (
        <ReportsPage
          lang={lang}
          company={session.company}
          onOpenEntry={(entryId) => {
            setJournalEntryId(entryId);
            setPage("journal");
          }}
        />
      ) : page === "trial" ? (
        <TrialBalancePage
          lang={lang}
          company={session.company}
          onOpenEntry={(entryId) => {
            setJournalEntryId(entryId);
            setPage("journal");
          }}
        />
      ) : page === "inventory" ? (
        <InventoryPage lang={lang} company={session.company} />
      ) : page === "admin" ? (
        <AdminPage lang={lang} session={session} onRefreshSession={handleRefreshSession} />
      ) : (
        <ToolsPage lang={lang} company={session.company} />
      )}
    </Shell>
  );
}