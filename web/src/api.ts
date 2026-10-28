/** Typed client for the Al-Mahmoud API plus the small session the shell keeps. */

const API_BASE = "/api/v1";
const TOKEN_KEY = "almahmoud.access_token";
const COMPANY_KEY = "almahmoud.company_id";

export interface ApiUser {
  id: string;
  tenant_id: string;
  email: string;
  full_name: string;
  mfa_enabled: boolean;
}

export interface Company {
  id: string;
  code: string;
  name_ar: string;
  name_en: string | null;
  base_currency: string;
  role_codes: string[];
  permissions: string[];
}

export interface TenantMfaPolicy {
  mfa_sensitive_required: boolean;
  mfa_posting_required: boolean;
}

export interface MeResponse {
  user: ApiUser;
  companies: Company[];
  tenant_mfa_policy: TenantMfaPolicy;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_at: string;
  user: ApiUser;
}

export interface Customer {
  id: string;
  code: string;
  name_ar: string;
  name_en: string | null;
  tax_number: string | null;
  is_active: boolean;
}

export interface TaxCode {
  id: string;
  code: string;
  name_ar: string;
  rate_basis_points: number;
  kind: "sales" | "purchase" | "withholding";
  jurisdiction: string | null;
  is_active: boolean;
}

export interface Item {
  id: string;
  code: string;
  name_ar: string;
  name_en: string | null;
  unit_of_measure: string;
  quantity_on_hand: string;
  stock_value_minor: number;
}

export interface InvoiceLine {
  id: string;
  line_number: number;
  item_id: string | null;
  tax_code_id: string | null;
  description: string;
  quantity: string;
  unit_price_minor: number;
  line_subtotal_minor: number;
  line_tax_minor: number;
  line_total_minor: number;
  base_line_subtotal_minor: number;
  base_line_tax_minor: number;
}

export interface Invoice {
  id: string;
  number: string;
  customer_id: string;
  invoice_date: string;
  due_date: string | null;
  currency: string;
  exchange_rate: string;
  status: "draft" | "posted" | "part_paid" | "paid";
  memo: string | null;
  subtotal_minor: number;
  tax_minor: number;
  total_minor: number;
  base_subtotal_minor: number;
  base_tax_minor: number;
  base_total_minor: number;
  journal_entry_id: string | null;
  cogs_entry_id: string | null;
  posted_at: string | null;
  lines: InvoiceLine[];
}

export interface InvoiceLinePayload {
  description: string;
  quantity: string;
  unit_price_minor: number;
  item_id?: string | null;
  tax_code_id?: string | null;
}

export interface InvoicePayload {
  customer_id: string;
  invoice_date: string;
  currency: string;
  exchange_rate: string;
  memo?: string | null;
  post: boolean;
  lines: InvoiceLinePayload[];
}

export interface ReportMeta {
  generated_at: string;
  generated_by: string;
  company_id: string;
  company_name_ar: string;
  company_name_en: string | null;
  filters: Record<string, string>;
}

export interface TrialBalanceRow {
  account_id: string;
  code: string;
  name_ar: string;
  name_en: string | null;
  account_type: string;
  debit_minor: number;
  credit_minor: number;
  balance_minor: number;
  compare_debit_minor?: number | null;
  compare_credit_minor?: number | null;
  compare_balance_minor?: number | null;
}

export interface TrialBalance {
  meta: ReportMeta;
  start_date: string;
  end_date: string;
  compare_start_date?: string | null;
  compare_end_date?: string | null;
  rows: TrialBalanceRow[];
  total_debit_minor: number;
  total_credit_minor: number;
  compare_total_debit_minor?: number | null;
  compare_total_credit_minor?: number | null;
  balanced: boolean;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

let accessToken: string | null = sessionStorage.getItem(TOKEN_KEY);
let companyId: string | null = sessionStorage.getItem(COMPANY_KEY);

// Refresh this long before the access token expires. A request that still
// meets an expired token also retries after a refresh, so a tab left open
// across the fifteen-minute boundary keeps its draft instead of bouncing the
// user to the login screen (ruling AC-5).
const REFRESH_LEAD_MS = 90_000;
let refreshInFlight: Promise<boolean> | null = null;
let refreshTimer: ReturnType<typeof setTimeout> | null = null;

function readCookie(name: string): string | null {
  const prefix = `${name}=`;
  for (const part of document.cookie.split(";")) {
    const entry = part.trim();
    if (entry.startsWith(prefix)) {
      return decodeURIComponent(entry.slice(prefix.length));
    }
  }
  return null;
}

export function currentCompanyId(): string | null {
  return companyId;
}

export function hasAccessToken(): boolean {
  return accessToken !== null;
}

export function setCompanyId(value: string | null): void {
  companyId = value;
  if (value === null) {
    sessionStorage.removeItem(COMPANY_KEY);
  } else {
    sessionStorage.setItem(COMPANY_KEY, value);
  }
}

export function setAccessToken(value: string | null): void {
  accessToken = value;
  if (value === null) {
    sessionStorage.removeItem(TOKEN_KEY);
  } else {
    sessionStorage.setItem(TOKEN_KEY, value);
  }
}

function applyAccessToken(token: string, expiresAt: string | undefined): void {
  setAccessToken(token);
  if (expiresAt !== undefined) {
    scheduleSessionRefresh(expiresAt);
  }
}

function scheduleSessionRefresh(expiresAt: string): void {
  if (refreshTimer !== null) {
    clearTimeout(refreshTimer);
    refreshTimer = null;
  }
  const expiry = Date.parse(expiresAt);
  if (Number.isNaN(expiry)) {
    return;
  }
  const delay = Math.max(expiry - Date.now() - REFRESH_LEAD_MS, 5_000);
  refreshTimer = setTimeout(() => {
    void refreshSession();
  }, delay);
}

function clearSession(): void {
  if (refreshTimer !== null) {
    clearTimeout(refreshTimer);
    refreshTimer = null;
  }
  setAccessToken(null);
  setCompanyId(null);
}

async function rotateSession(): Promise<boolean> {
  const csrf = readCookie("csrf_token");
  if (csrf === null) {
    return false;
  }
  const response = await fetch(`${API_BASE}/auth/refresh`, {
    method: "POST",
    headers: { Accept: "application/json", "X-CSRF-Token": csrf },
  });
  if (!response.ok) {
    return false;
  }
  const body = (await response.json()) as LoginResponse;
  applyAccessToken(body.access_token, body.expires_at);
  return true;
}

export async function refreshSession(): Promise<boolean> {
  // One in-flight rotation for the whole tab: two concurrent rotations of the
  // same cookie are indistinguishable from a replay and burn the family.
  if (refreshInFlight === null) {
    refreshInFlight = rotateSession().finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

async function parseError(response: Response): Promise<ApiError> {
  const fallback = `HTTP ${response.status}`;
  try {
    const body: unknown = await response.json();
    if (
      typeof body === "object" &&
      body !== null &&
      "detail" in body &&
      typeof body.detail === "string"
    ) {
      const code =
        "code" in body && typeof body.code === "string" ? body.code : "request_failed";
      return new ApiError(response.status, code, body.detail);
    }
  } catch {
    // Fall through to the status fallback.
  }
  return new ApiError(response.status, "request_failed", fallback);
}

interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  company?: boolean;
  headers?: Record<string, string>;
  // Set by the retry after a refresh so a second 401 cannot loop forever.
  retryAuth?: boolean;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const send = async (): Promise<Response> => {
    const headers = new Headers({ Accept: "application/json" });
    if (options.body !== undefined) {
      headers.set("Content-Type", "application/json");
    }
    if (options.headers !== undefined) {
      for (const [name, value] of Object.entries(options.headers)) {
        headers.set(name, value);
      }
    }
    if (accessToken !== null) {
      headers.set("Authorization", `Bearer ${accessToken}`);
    }
    if (options.company !== false && companyId !== null) {
      headers.set("X-Company-Id", companyId);
    }
    return fetch(`${API_BASE}${path}`, {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    });
  };

  let response: Response;
  try {
    response = await send();
  } catch (caught) {
    // A stopped API or a dropped connection is not an empty list: surface it as
    // an ApiError so every caller can show its error state (ruling AC-24).
    if (caught instanceof DOMException && caught.name === "AbortError") {
      throw caught;
    }
    if (options.signal?.aborted === true) {
      throw caught;
    }
    throw new ApiError(0, "network_error", caught instanceof Error ? caught.message : "network error");
  }
  if (response.status === 401 && accessToken !== null && options.retryAuth !== false) {
    // The token outlived its access window, not the session: rotate silently and
    // replay the request once so an open form keeps its draft (ruling AC-5).
    if (await refreshSession()) {
      return request<T>(path, { ...options, retryAuth: false });
    }
    clearSession();
    window.dispatchEvent(new Event("almahmoud:unauthorized"));
    throw await parseError(response);
  }
  if (!response.ok) {
    throw await parseError(response);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export async function login(
  email: string,
  password: string,
  tenant: string | null,
  totpCode: string | null = null,
  signal?: AbortSignal,
): Promise<LoginResponse> {
  const body: Record<string, string> = { email, password };
  if (tenant !== null && tenant.trim() !== "") {
    body.tenant = tenant.trim();
  }
  if (totpCode !== null && totpCode.trim() !== "") {
    const secondFactor = totpCode.trim();
    // A six-digit value is an authenticator code; anything longer is a recovery code.
    if (/^\d{6}$/.test(secondFactor)) {
      body.totp_code = secondFactor;
    } else {
      body.recovery_code = secondFactor;
    }
  }
  const result = await request<LoginResponse>("/auth/login", {
    method: "POST",
    body,
    signal,
    company: false,
  });
  applyAccessToken(result.access_token, result.expires_at);
  return result;
}

export async function me(signal?: AbortSignal): Promise<MeResponse> {
  return request<MeResponse>("/auth/me", { signal, company: false });
}

export async function updateTenantMfaPolicy(
  payload: TenantMfaPolicy,
): Promise<TenantMfaPolicy> {
  return request<TenantMfaPolicy>("/auth/tenant-settings/mfa-policy", {
    method: "PUT",
    body: payload,
    company: false,
  });
}

export async function logout(): Promise<void> {
  // Logout is cookie-authenticated, so it carries the same CSRF proof as a
  // refresh; the server refuses the call without it (ruling AC-6).
  const csrf = readCookie("csrf_token");
  try {
    await request<void>("/auth/logout", {
      method: "POST",
      company: false,
      headers: csrf === null ? {} : { "X-CSRF-Token": csrf },
      retryAuth: false,
    });
  } catch {
    // Clearing the local session is what matters for the shell.
  } finally {
    clearSession();
  }
}

export interface MfaSetup {
  secret: string;
  provisioning_uri: string;
}

export interface MfaRecoveryCodes {
  codes: string[];
}

export interface AuthSessionInfo {
  id: string;
  created_at: string;
  last_used_at: string | null;
  expires_at: string;
  revoked_at: string | null;
  user_agent: string | null;
  ip_address: string | null;
}

export async function startMfaSetup(): Promise<MfaSetup> {
  return request<MfaSetup>("/auth/mfa/setup", { method: "POST", company: false });
}

export async function confirmMfaSetup(code: string): Promise<MfaRecoveryCodes> {
  return request<MfaRecoveryCodes>("/auth/mfa/confirm", {
    method: "POST",
    body: { code },
    company: false,
  });
}

export async function regenerateRecoveryCodes(
  password: string,
  code: string,
): Promise<MfaRecoveryCodes> {
  return request<MfaRecoveryCodes>("/auth/mfa/recovery-codes", {
    method: "POST",
    body: { password, code },
    company: false,
  });
}

export async function disableMfa(password: string, code: string): Promise<void> {
  await request<void>("/auth/mfa/disable", {
    method: "POST",
    body: { password, code },
    company: false,
  });
}

export async function listAuthSessions(): Promise<AuthSessionInfo[]> {
  return request<AuthSessionInfo[]>("/auth/sessions", { company: false });
}

export async function revokeAuthSession(id: string): Promise<void> {
  await request<void>(`/auth/sessions/${id}`, { method: "DELETE", company: false });
}

export async function listInvoices(signal?: AbortSignal): Promise<Invoice[]> {
  return request<Invoice[]>("/sales/invoices", { signal });
}

export async function listCustomers(signal?: AbortSignal): Promise<Customer[]> {
  return request<Customer[]>("/catalog/customers", { signal });
}

export async function listTaxCodes(signal?: AbortSignal): Promise<TaxCode[]> {
  return request<TaxCode[]>("/catalog/tax-codes", { signal });
}

export interface TaxCodePayload {
  code: string;
  name_ar: string;
  rate_basis_points: number;
  kind: "sales" | "purchase" | "withholding";
  jurisdiction?: string;
  valid_from?: string;
}

export async function createTaxCode(payload: TaxCodePayload): Promise<TaxCode> {
  return request<TaxCode>("/catalog/tax-codes", { method: "POST", body: payload });
}

export interface TaxRate {
  id: string;
  rate_basis_points: number;
  valid_from: string;
  valid_to: string | null;
}

export async function listTaxRates(taxCodeId: string, signal?: AbortSignal): Promise<TaxRate[]> {
  return request<TaxRate[]>(`/catalog/tax-codes/${taxCodeId}/rates`, { signal });
}

export async function setTaxRate(
  taxCodeId: string,
  rateBasisPoints: number,
  validFrom: string,
): Promise<TaxCode> {
  return request<TaxCode>(`/catalog/tax-codes/${taxCodeId}/rate`, {
    method: "PUT",
    body: { rate_basis_points: rateBasisPoints, valid_from: validFrom },
  });
}

export async function listItems(signal?: AbortSignal): Promise<Item[]> {
  return request<Item[]>("/catalog/items", { signal });
}

export interface Warehouse {
  id: string;
  code: string;
  name_ar: string;
  name_en: string | null;
  is_default: boolean;
  is_active: boolean;
}

export interface WarehousePayload {
  code: string;
  name_ar: string;
  name_en: string | null;
  is_default: boolean;
}

export interface WarehouseUpdatePayload {
  name_ar: string;
  name_en: string | null;
  is_active: boolean;
  is_default: boolean;
}

export interface StockBalance {
  warehouse_id: string;
  item_id: string;
  item_code: string;
  item_name_ar: string;
  quantity: string;
  value_minor: number;
}

export interface StockTransfer {
  id: string;
  number: string;
  transfer_date: string;
  from_warehouse_id: string;
  to_warehouse_id: string;
  item_id: string;
  quantity: string;
  value_minor: number;
  memo: string | null;
  posted_at: string;
}

export interface StockTransferPayload {
  transfer_date: string;
  from_warehouse_id: string;
  to_warehouse_id: string;
  item_id: string;
  quantity: string;
  memo?: string | null;
}

export async function listWarehouses(signal?: AbortSignal): Promise<Warehouse[]> {
  return request<Warehouse[]>("/inventory/warehouses", { signal });
}

export async function createWarehouse(payload: WarehousePayload): Promise<Warehouse> {
  return request<Warehouse>("/inventory/warehouses", { method: "POST", body: payload });
}

export async function updateWarehouse(
  id: string,
  payload: WarehouseUpdatePayload,
): Promise<Warehouse> {
  return request<Warehouse>(`/inventory/warehouses/${id}`, { method: "PUT", body: payload });
}

export async function listStockBalances(
  warehouseId: string | null,
  signal?: AbortSignal,
): Promise<StockBalance[]> {
  const query = warehouseId === null ? "" : `?warehouse_id=${encodeURIComponent(warehouseId)}`;
  return request<StockBalance[]>(`/inventory/balances${query}`, { signal });
}

export async function createStockTransfer(
  payload: StockTransferPayload,
): Promise<StockTransfer> {
  return request<StockTransfer>("/inventory/transfers", { method: "POST", body: payload });
}
export interface StockCountLine {
  id: string;
  item_id: string;
  counted_quantity: string;
  system_quantity: string;
  variance_quantity: string;
  variance_value_minor: number;
}

export interface StockCount {
  id: string;
  number: string;
  count_date: string;
  warehouse_id: string;
  status: "draft" | "posted";
  memo: string | null;
  journal_entry_id: string | null;
  posted_at: string | null;
  lines?: StockCountLine[];
}

export interface StockCountPayload {
  count_date: string;
  warehouse_id: string;
  memo?: string | null;
  lines: { item_id: string; counted_quantity: string }[];
}

export async function listStockCounts(signal?: AbortSignal): Promise<StockCount[]> {
  return request<StockCount[]>("/inventory/counts", { signal });
}

export async function createStockCount(payload: StockCountPayload): Promise<StockCount> {
  return request<StockCount>("/inventory/counts", { method: "POST", body: payload });
}

export async function postStockCount(id: string): Promise<StockCount> {
  return request<StockCount>(`/inventory/counts/${id}/post`, { method: "POST" });
}

export async function deleteStockCount(id: string): Promise<void> {
  return request<void>(`/inventory/counts/${id}`, { method: "DELETE" });
}

export async function createInvoice(payload: InvoicePayload): Promise<Invoice> {
  return request<Invoice>("/sales/invoices", { method: "POST", body: payload });
}

export async function postInvoice(id: string): Promise<Invoice> {
  return request<Invoice>(`/sales/invoices/${id}/post`, { method: "POST" });
}

export interface TrialBalanceOptions {
  compare?: boolean;
  signal?: AbortSignal;
}

export async function trialBalance(
  start: string,
  end: string,
  options: TrialBalanceOptions = {},
): Promise<TrialBalance> {
  const query = new URLSearchParams({ start, end });
  if (options.compare === true) {
    query.set("compare", "true");
  }
  return request<TrialBalance>(`/reports/trial-balance?${query.toString()}`, {
    signal: options.signal,
  });
}

export async function downloadReport(
  format: "pdf" | "xlsx",
  start: string,
  end: string,
  compare = false,
): Promise<Blob> {
  const query = new URLSearchParams({ start, end });
  if (compare) {
    query.set("compare", "true");
  }
  const headers = new Headers();
  if (accessToken !== null) {
    headers.set("Authorization", `Bearer ${accessToken}`);
  }
  if (companyId !== null) {
    headers.set("X-Company-Id", companyId);
  }
  const response = await fetch(`${API_BASE}/reports/trial-balance.${format}?${query.toString()}`, {
    headers,
  });
  if (!response.ok) {
    throw await parseError(response);
  }
  return response.blob();
}

export interface CashAccount {
  id: string;
  code: string;
  name_ar: string;
  name_en: string | null;
  kind: "cash" | "bank";
  currency: string;
  ledger_account_id: string;
  is_active: boolean;
}

export interface BillLine {
  id: string;
  line_number: number;
  item_id: string | null;
  tax_code_id: string | null;
  description: string;
  quantity: string;
  unit_price_minor: number;
  line_total_minor: number;
}

export interface Bill {
  id: string;
  number: string;
  supplier_id: string;
  bill_date: string;
  due_date: string | null;
  currency: string;
  status: "draft" | "posted" | "part_paid" | "paid";
  total_minor: number;
  base_total_minor: number;
  journal_entry_id?: string | null;
  lines: BillLine[];
}

export interface BillPayload {
  supplier_id: string;
  bill_date: string;
  due_date?: string | null;
  currency: string;
  exchange_rate?: string;
  memo?: string | null;
  number?: string | null;
  post: boolean;
  lines: {
    description: string;
    quantity: string;
    unit_price_minor: number;
    item_id: string | null;
    tax_code_id: string | null;
  }[];
}

export interface Allocation {
  id: string;
  invoice_id?: string;
  bill_id?: string;
  amount_minor: number;
  base_amount_minor: number;
  settlement_base_minor: number;
}

export interface Receipt {
  id: string;
  number: string;
  customer_id: string;
  cash_bank_account_id: string;
  receipt_date: string;
  currency: string;
  amount_minor: number;
  base_amount_minor: number;
  unallocated_minor: number;
  withholding_tax_code_id: string | null;
  withholding_base_minor: number;
  withholding_amount_minor: number;
  base_withholding_minor: number;
  status: "draft" | "posted";
  memo: string | null;
  journal_entry_id?: string | null;
  allocations: Allocation[];
}

export interface Payment {
  id: string;
  number: string;
  supplier_id: string;
  cash_bank_account_id: string;
  payment_date: string;
  currency: string;
  amount_minor: number;
  base_amount_minor: number;
  unallocated_minor: number;
  withholding_tax_code_id: string | null;
  withholding_base_minor: number;
  withholding_amount_minor: number;
  base_withholding_minor: number;
  status: "draft" | "posted";
  memo: string | null;
  journal_entry_id?: string | null;
  allocations: Allocation[];
}

export interface ReceiptPayload {
  customer_id: string;
  cash_bank_account_id: string;
  receipt_date: string;
  amount_minor: number;
  currency: string;
  post: boolean;
  memo?: string | null;
  withholding_tax_code_id?: string | null;
  allocations: { document_id: string; amount_minor: number }[];
}

export interface PaymentPayload {
  supplier_id: string;
  cash_bank_account_id: string;
  payment_date: string;
  amount_minor: number;
  currency: string;
  post: boolean;
  memo?: string | null;
  withholding_tax_code_id?: string | null;
  allocations: { document_id: string; amount_minor: number }[];
}

export interface ReturnLine {
  id: string;
  line_number: number;
  description: string;
  quantity: string;
  unit_price_minor: number;
  line_total_minor: number;
}

export interface ReturnDocument {
  id: string;
  number: string;
  status: "draft" | "posted";
  currency: string;
  total_minor: number;
  base_total_minor: number;
  journal_entry_id?: string | null;
  lines: ReturnLine[];
}

export interface CreditNote extends ReturnDocument {
  customer_id: string;
  invoice_id: string;
  credit_note_date: string;
}

export interface DebitNote extends ReturnDocument {
  supplier_id: string;
  bill_id: string;
  debit_note_date: string;
}

export interface ReturnPayload {
  invoice_id?: string;
  bill_id?: string;
  customer_id?: string;
  supplier_id?: string;
  credit_note_date?: string;
  debit_note_date?: string;
  currency: string;
  post: boolean;
  lines: {
    description: string;
    quantity: string;
    unit_price_minor: number;
    item_id?: string | null;
    tax_code_id?: string | null;
    invoice_line_id?: string;
    bill_line_id?: string;
  }[];
}

export interface AgingRow {
  party_id: string;
  document_id: string;
  party_code: string;
  party_name_ar: string;
  document_number: string;
  document_date: string;
  due_date: string;
  total_minor: number;
  outstanding_minor: number;
  base_outstanding_minor: number;
  days_overdue: number;
  bucket: string;
}

export interface AgingReport {
  meta: ReportMeta;
  as_of: string;
  kind: string;
  rows: AgingRow[];
  total_base_outstanding_minor: number;
  buckets: { bucket: string; amount_minor: number }[];
}

export interface StatementRow {
  entry_date: string;
  document_id: string;
  document_type: string;
  number: string;
  description: string | null;
  debit_minor: number;
  credit_minor: number;
  balance_minor: number;
}

export interface PartyStatement {
  meta: ReportMeta;
  party_id: string;
  party_name_ar: string;
  party_name_en: string | null;
  start_date: string;
  end_date: string;
  opening_balance_minor: number;
  rows: StatementRow[];
  total_debit_minor: number;
  total_credit_minor: number;
  closing_balance_minor: number;
  compare_start_date?: string | null;
  compare_end_date?: string | null;
  compare_opening_balance_minor?: number | null;
  compare_total_debit_minor?: number | null;
  compare_total_credit_minor?: number | null;
  compare_closing_balance_minor?: number | null;
}

export interface AmountRow {
  account_id: string;
  code: string;
  name_ar: string;
  name_en: string | null;
  account_type: string;
  amount_minor: number;
  compare_amount_minor?: number | null;
}

export interface ProfitAndLoss {
  meta: ReportMeta;
  start_date: string;
  end_date: string;
  compare_start_date?: string | null;
  compare_end_date?: string | null;
  revenue: AmountRow[];
  expenses: AmountRow[];
  total_revenue_minor: number;
  total_expense_minor: number;
  net_profit_minor: number;
  compare_total_revenue_minor?: number | null;
  compare_total_expense_minor?: number | null;
  compare_net_profit_minor?: number | null;
}

export interface BalanceSheet {
  meta: ReportMeta;
  as_of: string;
  compare_as_of?: string | null;
  assets: AmountRow[];
  liabilities: AmountRow[];
  equity: AmountRow[];
  current_earnings_minor: number;
  compare_current_earnings_minor?: number | null;
  total_assets_minor: number;
  total_liabilities_minor: number;
  total_equity_minor: number;
  compare_total_assets_minor?: number | null;
  compare_total_liabilities_minor?: number | null;
  compare_total_equity_minor?: number | null;
  balanced: boolean;
}

export interface LedgerRow {
  entry_id: string;
  entry_date: string;
  entry_number: string;
  description: string | null;
  debit_minor: number;
  credit_minor: number;
  balance_minor: number;
}

export interface GeneralLedger {
  meta: ReportMeta;
  account_id: string;
  account_code: string;
  account_name_ar: string;
  start_date: string;
  end_date: string;
  opening_balance_minor: number;
  rows: LedgerRow[];
  closing_balance_minor: number;
}

export interface InventoryRow {
  item_id: string;
  code: string;
  name_ar: string;
  unit_of_measure: string;
  quantity_on_hand: string;
  stock_value_minor: number;
}

export interface InventoryValuation {
  meta: ReportMeta;
  rows: InventoryRow[];
  total_value_minor: number;
}

export interface TaxReturn {
  meta: ReportMeta;
  start_date: string;
  end_date: string;
  output_vat_minor: number;
  output_credit_notes_minor: number;
  input_vat_minor: number;
  input_debit_notes_minor: number;
  net_output_vat_minor: number;
  net_input_vat_minor: number;
  payable_minor: number;
  invoice_count: number;
  bill_count: number;
}

export async function listCashAccounts(signal?: AbortSignal): Promise<CashAccount[]> {
  return request<CashAccount[]>("/cashbank/accounts", { signal });
}

export async function listReceipts(signal?: AbortSignal): Promise<Receipt[]> {
  return request<Receipt[]>("/cashbank/receipts", { signal });
}

export async function createReceipt(payload: ReceiptPayload): Promise<Receipt> {
  return request<Receipt>("/cashbank/receipts", { method: "POST", body: payload });
}

export async function postReceipt(id: string): Promise<Receipt> {
  return request<Receipt>(`/cashbank/receipts/${id}/post`, { method: "POST" });
}

export async function listPayments(signal?: AbortSignal): Promise<Payment[]> {
  return request<Payment[]>("/cashbank/payments", { signal });
}

export async function createPayment(payload: PaymentPayload): Promise<Payment> {
  return request<Payment>("/cashbank/payments", { method: "POST", body: payload });
}

export async function postPayment(id: string): Promise<Payment> {
  return request<Payment>(`/cashbank/payments/${id}/post`, { method: "POST" });
}

export async function listBills(signal?: AbortSignal): Promise<Bill[]> {
  return request<Bill[]>("/purchasing/bills", { signal });
}

export async function createBill(payload: BillPayload): Promise<Bill> {
  return request<Bill>("/purchasing/bills", { method: "POST", body: payload });
}

export async function postBill(id: string): Promise<Bill> {
  return request<Bill>(`/purchasing/bills/${id}/post`, { method: "POST" });
}

export async function listSuppliers(signal?: AbortSignal): Promise<Customer[]> {
  return request<Customer[]>("/catalog/suppliers", { signal });
}

export async function listCreditNotes(signal?: AbortSignal): Promise<CreditNote[]> {
  return request<CreditNote[]>("/sales/credit-notes", { signal });
}

export async function listDebitNotes(signal?: AbortSignal): Promise<DebitNote[]> {
  return request<DebitNote[]>("/purchasing/debit-notes", { signal });
}

export async function createCreditNote(
  payload: ReturnPayload,
): Promise<CreditNote> {
  return request<CreditNote>("/sales/credit-notes", { method: "POST", body: payload });
}

export async function createDebitNote(payload: ReturnPayload): Promise<DebitNote> {
  return request<DebitNote>("/purchasing/debit-notes", { method: "POST", body: payload });
}

export async function postCreditNote(id: string): Promise<CreditNote> {
  return request<CreditNote>(`/sales/credit-notes/${id}/post`, { method: "POST" });
}

export async function postDebitNote(id: string): Promise<DebitNote> {
  return request<DebitNote>(`/purchasing/debit-notes/${id}/post`, { method: "POST" });
}

export async function arAging(asOf: string, signal?: AbortSignal): Promise<AgingReport> {
  return request<AgingReport>(`/reports/ar-aging?as_of=${asOf}`, { signal });
}

export async function apAging(asOf: string, signal?: AbortSignal): Promise<AgingReport> {
  return request<AgingReport>(`/reports/ap-aging?as_of=${asOf}`, { signal });
}

export async function customerStatement(
  customerId: string,
  start: string,
  end: string,
  compare = false,
  signal?: AbortSignal,
): Promise<PartyStatement> {
  const query = new URLSearchParams({ customer_id: customerId, start, end });
  if (compare) {
    query.set("compare", "true");
  }
  return request<PartyStatement>(`/reports/customer-statement?${query.toString()}`, { signal });
}

export async function profitAndLoss(
  start: string,
  end: string,
  compare = false,
  signal?: AbortSignal,
): Promise<ProfitAndLoss> {
  const query = new URLSearchParams({ start, end });
  if (compare) {
    query.set("compare", "true");
  }
  return request<ProfitAndLoss>(`/reports/profit-and-loss?${query.toString()}`, { signal });
}

export async function balanceSheet(
  asOf: string,
  compare = false,
  signal?: AbortSignal,
): Promise<BalanceSheet> {
  const query = new URLSearchParams({ as_of: asOf });
  if (compare) {
    query.set("compare", "true");
  }
  return request<BalanceSheet>(`/reports/balance-sheet?${query.toString()}`, { signal });
}

export async function inventoryValuation(signal?: AbortSignal): Promise<InventoryValuation> {
  return request<InventoryValuation>("/reports/inventory-valuation", { signal });
}

export async function taxReturn(
  start: string,
  end: string,
  signal?: AbortSignal,
): Promise<TaxReturn> {
  const query = new URLSearchParams({ start, end });
  return request<TaxReturn>(`/reports/tax-return?${query.toString()}`, { signal });
}

export interface StockMovementRow {
  occurred_on: string;
  move_type: string;
  warehouse_id: string;
  warehouse_code: string;
  warehouse_name_ar: string;
  warehouse_name_en: string | null;
  item_code: string;
  item_name_ar: string;
  quantity: string;
  unit_cost_minor: number;
  value_minor: number;
  document_type: string;
  document_id: string;
}

export interface StockMovement {
  meta: ReportMeta;
  start_date: string;
  end_date: string;
  rows: StockMovementRow[];
  total_receipt_value_minor: number;
  total_issue_value_minor: number;
}

export async function generalLedger(
  accountId: string,
  start: string,
  end: string,
  signal?: AbortSignal,
): Promise<GeneralLedger> {
  const query = new URLSearchParams({ account_id: accountId, start, end });
  return request<GeneralLedger>(`/reports/general-ledger?${query.toString()}`, { signal });
}

export async function stockMovement(
  start: string,
  end: string,
  signal?: AbortSignal,
): Promise<StockMovement> {
  const query = new URLSearchParams({ start, end });
  return request<StockMovement>(`/reports/stock-movement?${query.toString()}`, { signal });
}

export interface DocumentRegisterRow {
  document_type: string;
  document_id: string;
  number: string;
  document_date: string;
  status: string;
  currency: string;
  amount_minor: number;
  base_amount_minor: number;
}

export interface DocumentRegister {
  meta: ReportMeta;
  start_date: string;
  end_date: string;
  rows: DocumentRegisterRow[];
  total_amount_minor: number;
}

export async function documentRegister(
  start: string,
  end: string,
  documentType?: string,
  signal?: AbortSignal,
): Promise<DocumentRegister> {
  const query = new URLSearchParams({ start, end });
  if (documentType) {
    query.set("document_type", documentType);
  }
  return request<DocumentRegister>(`/reports/document-register?${query.toString()}`, { signal });
}

export interface AuditTrailReport {
  meta: ReportMeta;
  start_date: string;
  end_date: string;
  rows: AuditEvent[];
  total: number;
}

export async function auditTrail(
  start: string,
  end: string,
  options: { action?: string; entityType?: string; signal?: AbortSignal } = {},
): Promise<AuditTrailReport> {
  const query = new URLSearchParams({ start, end });
  if (options.action) {
    query.set("action", options.action);
  }
  if (options.entityType) {
    query.set("entity_type", options.entityType);
  }
  return request<AuditTrailReport>(`/reports/audit-trail?${query.toString()}`, {
    signal: options.signal,
  });
}

export async function getSalesInvoice(id: string, signal?: AbortSignal): Promise<Invoice> {
  return request<Invoice>(`/sales/invoices/${id}`, { signal });
}

export async function getPurchaseBill(id: string, signal?: AbortSignal): Promise<Bill> {
  return request<Bill>(`/purchasing/bills/${id}`, { signal });
}

export async function getSalesCreditNote(
  id: string,
  signal?: AbortSignal,
): Promise<CreditNote> {
  return request<CreditNote>(`/sales/credit-notes/${id}`, { signal });
}

export async function getPurchaseDebitNote(
  id: string,
  signal?: AbortSignal,
): Promise<DebitNote> {
  return request<DebitNote>(`/purchasing/debit-notes/${id}`, { signal });
}

export async function getReceipt(id: string, signal?: AbortSignal): Promise<Receipt> {
  return request<Receipt>(`/cashbank/receipts/${id}`, { signal });
}

export async function getPayment(id: string, signal?: AbortSignal): Promise<Payment> {
  return request<Payment>(`/cashbank/payments/${id}`, { signal });
}

export async function getCheque(id: string, signal?: AbortSignal): Promise<Cheque> {
  return request<Cheque>(`/settlements/cheques/${id}`, { signal });
}

export async function supplierStatement(
  supplierId: string,
  start: string,
  end: string,
  compare = false,
  signal?: AbortSignal,
): Promise<PartyStatement> {
  const query = new URLSearchParams({ supplier_id: supplierId, start, end });
  if (compare) {
    query.set("compare", "true");
  }
  return request<PartyStatement>(`/reports/supplier-statement?${query.toString()}`, { signal });
}

export interface JournalEntrySummary {
  id: string;
  number: string;
  status: "draft" | "posted";
  entry_date: string;
  period_id: string;
  memo: string | null;
  source_type: string | null;
  source_id: string | null;
  reverses_entry_id: string | null;
  posted_at: string | null;
  created_at: string;
}

export interface JournalLine {
  id: string;
  line_no: number;
  account_id: string;
  debit_minor: number;
  credit_minor: number;
  description: string | null;
}

export interface JournalEntry extends JournalEntrySummary {
  total_debit_minor: number;
  total_credit_minor: number;
  lines: JournalLine[];
}

export interface JournalLinePayload {
  account_id: string;
  debit_minor: number;
  credit_minor: number;
  description?: string | null;
}

export interface JournalEntryPayload {
  period_id: string;
  entry_date: string;
  memo?: string | null;
  post: boolean;
  lines: JournalLinePayload[];
}

export async function reverseJournalEntry(
  id: string,
  memo?: string | null,
): Promise<JournalEntry> {
  return request<JournalEntry>(`/ledger/journal-entries/${id}/reverse`, {
    method: "POST",
    body: { entry_date: null, memo: memo ?? null },
  });
}

export type JournalEntryUpdatePayload = Omit<JournalEntryPayload, "post">;

export async function createJournalEntry(payload: JournalEntryPayload): Promise<JournalEntry> {
  return request<JournalEntry>("/ledger/journal-entries", { method: "POST", body: payload });
}

export async function updateJournalEntry(
  id: string,
  payload: JournalEntryUpdatePayload,
): Promise<JournalEntry> {
  return request<JournalEntry>(`/ledger/journal-entries/${id}`, { method: "PUT", body: payload });
}

export async function postJournalEntry(id: string): Promise<JournalEntry> {
  return request<JournalEntry>(`/ledger/journal-entries/${id}/post`, { method: "POST" });
}

export async function deleteJournalEntry(id: string): Promise<void> {
  await request<void>(`/ledger/journal-entries/${id}`, { method: "DELETE" });
}

export async function listJournalEntries(
  options: { periodId?: string; status?: string; limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<JournalEntrySummary[]> {
  const query = new URLSearchParams();
  if (options.periodId) {
    query.set("period_id", options.periodId);
  }
  if (options.status) {
    query.set("status", options.status);
  }
  query.set("limit", String(options.limit ?? 100));
  query.set("offset", String(options.offset ?? 0));
  return request<JournalEntrySummary[]>(`/ledger/journal-entries?${query.toString()}`, { signal });
}

export async function getJournalEntry(id: string, signal?: AbortSignal): Promise<JournalEntry> {
  return request<JournalEntry>(`/ledger/journal-entries/${id}`, { signal });
}

export async function downloadReportPath(path: string): Promise<Blob> {
  const headers = new Headers();
  if (accessToken !== null) {
    headers.set("Authorization", `Bearer ${accessToken}`);
  }
  if (companyId !== null) {
    headers.set("X-Company-Id", companyId);
  }
  const response = await fetch(`${API_BASE}${path}`, { headers });
  if (!response.ok) {
    throw await parseError(response);
  }
  return response.blob();
}
export interface ChequeAllocation {
  id: string;
  invoice_id: string | null;
  bill_id: string | null;
  amount_minor: number;
  base_amount_minor: number;
  settlement_base_minor: number;
  is_reversed: boolean;
}

export interface ChequeEvent {
  id: string;
  event_type: string;
  event_date: string;
  journal_entry_id: string | null;
  notes: string | null;
}

export interface Cheque {
  id: string;
  direction: "received" | "issued";
  number: string;
  customer_id: string | null;
  supplier_id: string | null;
  cash_bank_account_id: string;
  bank_name: string | null;
  party_name: string | null;
  issue_date: string;
  due_date: string;
  currency: string;
  exchange_rate: string;
  amount_minor: number;
  base_amount_minor: number;
  unallocated_minor: number;
  status: "in_hand" | "deposited" | "cleared" | "issued" | "bounced" | "cancelled";
  memo: string | null;
  allocations: ChequeAllocation[];
  events: ChequeEvent[];
}

export interface ChequePayload {
  direction: "received" | "issued";
  number: string;
  cash_bank_account_id: string;
  issue_date: string;
  amount_minor: number;
  customer_id?: string | null;
  supplier_id?: string | null;
  due_date?: string | null;
  currency: string;
  exchange_rate?: string;
  memo?: string | null;
  post: boolean;
  allocations: { document_id: string; amount_minor: number }[];
}

export interface CashTransfer {
  id: string;
  number: string;
  transfer_date: string;
  from_account_id: string;
  to_account_id: string;
  currency: string;
  exchange_rate: string;
  amount_minor: number;
  base_amount_minor: number;
  status: string;
  memo: string | null;
  journal_entry_id: string | null;
  posted_at: string | null;
}

export interface TransferDetail extends CashTransfer {
  from_account_name_ar: string;
  from_account_name_en: string | null;
  to_account_name_ar: string;
  to_account_name_en: string | null;
}

export interface CashTransferPayload {
  transfer_date: string;
  from_account_id: string;
  to_account_id: string;
  amount_minor: number;
  currency: string;
  memo?: string | null;
}

export interface BankStatementLine {
  id: string;
  cash_bank_account_id: string;
  statement_date: string;
  description: string;
  reference: string | null;
  amount_minor: number;
  currency: string;
  status: "unmatched" | "matched";
  matched_source_type: string | null;
  matched_source_id: string | null;
  matched_at: string | null;
}

export interface BankStatementImportPayload {
  cash_bank_account_id: string;
  currency: string;
  lines: {
    statement_date: string;
    description: string;
    amount_minor: number;
    reference?: string | null;
  }[];
}

export interface BankMatchPayload {
  source_type: "receipt" | "supplier_payment" | "cheque_event" | "cash_transfer";
  source_id: string;
}

export async function listCheques(
  direction?: "received" | "issued",
  signal?: AbortSignal,
): Promise<Cheque[]> {
  const query = direction === undefined ? "" : `?direction=${direction}`;
  return request<Cheque[]>(`/cashbank/cheques${query}`, { signal });
}

export async function createCheque(payload: ChequePayload): Promise<Cheque> {
  return request<Cheque>("/cashbank/cheques", { method: "POST", body: payload });
}

export async function chequeAction(
  id: string,
  action: "deposit" | "clear" | "bounce" | "cancel",
  eventDate: string,
): Promise<Cheque> {
  return request<Cheque>(`/cashbank/cheques/${id}/${action}`, {
    method: "POST",
    body: { event_date: eventDate },
  });
}

export async function listTransfers(signal?: AbortSignal): Promise<CashTransfer[]> {
  return request<CashTransfer[]>("/cashbank/transfers", { signal });
}

export async function createTransfer(payload: CashTransferPayload): Promise<CashTransfer> {
  return request<CashTransfer>("/cashbank/transfers", { method: "POST", body: payload });
}

export async function getTransfer(id: string): Promise<TransferDetail> {
  return request<TransferDetail>(`/cashbank/transfers/${id}`);
}

export async function listBankStatementLines(
  status?: "unmatched" | "matched",
  signal?: AbortSignal,
): Promise<BankStatementLine[]> {
  const query = status === undefined ? "" : `?status_filter=${status}`;
  return request<BankStatementLine[]>(`/cashbank/bank-statements${query}`, { signal });
}

export async function importBankStatementLines(
  payload: BankStatementImportPayload,
): Promise<BankStatementLine[]> {
  return request<BankStatementLine[]>("/cashbank/bank-statements", {
    method: "POST",
    body: payload,
  });
}

export async function matchBankStatementLine(
  id: string,
  payload: BankMatchPayload,
): Promise<BankStatementLine> {
  return request<BankStatementLine>(`/cashbank/bank-statements/${id}/match`, {
    method: "POST",
    body: payload,
  });
}

export async function unmatchBankStatementLine(id: string): Promise<BankStatementLine> {
  return request<BankStatementLine>(`/cashbank/bank-statements/${id}/unmatch`, {
    method: "POST",
  });
}

export interface Account {
  id: string;
  code: string;
  name_ar: string;
  name_en: string | null;
  account_type: "asset" | "liability" | "equity" | "revenue" | "expense";
  is_postable: boolean;
  is_active: boolean;
}

export interface AccountingPeriod {
  id: string;
  number: number;
  start_date: string;
  end_date: string;
  status: "open" | "closed";
  closed_at: string | null;
}

export interface FiscalYear {
  id: string;
  code: string;
  name_ar: string;
  start_date: string;
  end_date: string;
  status: "open" | "closed";
  periods: AccountingPeriod[];
}

export interface FiscalYearClose {
  fiscal_year: FiscalYear;
  closing_entry: { id: string; number: string } | null;
  periods_closed: number;
}

export interface AuditEvent {
  id: string;
  actor_user_id: string | null;
  action: string;
  entity_type: string | null;
  entity_id: string | null;
  detail: Record<string, unknown> | null;
  request_id: string | null;
  ip_address: string | null;
  created_at: string;
}

export interface AuditPage {
  items: AuditEvent[];
  limit: number;
  offset: number;
}

export interface OpeningBalanceLine {
  account_code: string;
  account_name_ar: string;
  account_name_en: string | null;
  debit_minor: number;
  credit_minor: number;
  description: string | null;
}

export interface OpeningBalanceReport {
  valid: boolean;
  total_debit_minor: number;
  total_credit_minor: number;
  line_count: number;
  lines: OpeningBalanceLine[];
  entry_id: string | null;
  entry_number: string | null;
}

export interface AccountPayload {
  code: string;
  name_ar: string;
  name_en?: string | null;
  account_type: "asset" | "liability" | "equity" | "revenue" | "expense";
  is_postable: boolean;
  is_active: boolean;
}

export async function listAccounts(signal?: AbortSignal): Promise<Account[]> {
  return request<Account[]>("/ledger/accounts", { signal });
}

export async function createAccount(payload: AccountPayload): Promise<Account> {
  return request<Account>("/ledger/accounts", { method: "POST", body: payload });
}

export async function updateAccount(
  id: string,
  payload: Partial<Omit<AccountPayload, "code" | "account_type">>,
): Promise<Account> {
  return request<Account>(`/ledger/accounts/${id}`, { method: "PATCH", body: payload });
}

export interface PostingMappingRow {
  key: string;
  label_ar: string;
  label_en: string;
  account_type: "asset" | "liability" | "equity" | "revenue" | "expense";
  account_id: string | null;
  account_code: string | null;
  account_name_ar: string | null;
  valid_from: string | null;
}

export async function listPostingMappings(
  onDate?: string,
  signal?: AbortSignal,
): Promise<PostingMappingRow[]> {
  const query = onDate === undefined ? "" : `?on_date=${onDate}`;
  return request<PostingMappingRow[]>(`/ledger/posting-mappings${query}`, { signal });
}

export async function updatePostingMapping(
  key: string,
  accountId: string,
  validFrom: string,
): Promise<PostingMappingRow[]> {
  return request<PostingMappingRow[]>(`/ledger/posting-mappings/${key}`, {
    method: "PUT",
    body: { account_id: accountId, valid_from: validFrom },
  });
}

export async function listPeriods(signal?: AbortSignal): Promise<AccountingPeriod[]> {
  return request<AccountingPeriod[]>("/ledger/periods", { signal });
}

export async function listFiscalYears(signal?: AbortSignal): Promise<FiscalYear[]> {
  return request<FiscalYear[]>("/ledger/fiscal-years", { signal });
}

export async function closePeriod(id: string, reason: string): Promise<AccountingPeriod> {
  return request<AccountingPeriod>(`/ledger/periods/${id}/close`, {
    method: "POST",
    body: { reason },
  });
}

export async function reopenPeriod(id: string, reason: string): Promise<AccountingPeriod> {
  return request<AccountingPeriod>(`/ledger/periods/${id}/reopen`, {
    method: "POST",
    body: { reason },
  });
}

export async function closeFiscalYear(id: string, reason: string): Promise<FiscalYearClose> {
  return request<FiscalYearClose>(`/ledger/fiscal-years/${id}/close`, {
    method: "POST",
    body: { reason },
  });
}

export async function listAuditEvents(
  options: { limit?: number; offset?: number; action?: string; entity_type?: string } = {},
  signal?: AbortSignal,
): Promise<AuditPage> {
  const query = new URLSearchParams();
  if (options.limit !== undefined) {
    query.set("limit", String(options.limit));
  }
  if (options.offset !== undefined) {
    query.set("offset", String(options.offset));
  }
  if (options.action !== undefined && options.action !== "") {
    query.set("action", options.action);
  }
  if (options.entity_type !== undefined && options.entity_type !== "") {
    query.set("entity_type", options.entity_type);
  }
  const suffix = query.toString() === "" ? "" : `?${query.toString()}`;
  return request<AuditPage>(`/audit/events${suffix}`, { signal });
}

async function uploadForm<T>(path: string, form: FormData): Promise<T> {
  const send = async (): Promise<Response> => {
    const headers = new Headers({ Accept: "application/json" });
    if (accessToken !== null) {
      headers.set("Authorization", `Bearer ${accessToken}`);
    }
    if (companyId !== null) {
      headers.set("X-Company-Id", companyId);
    }
    return fetch(`${API_BASE}${path}`, { method: "POST", headers, body: form });
  };

  let response = await send();
  if (response.status === 401 && accessToken !== null) {
    if (await refreshSession()) {
      response = await send();
    } else {
      clearSession();
      window.dispatchEvent(new Event("almahmoud:unauthorized"));
      throw await parseError(response);
    }
  }
  if (!response.ok) {
    throw await parseError(response);
  }
  return (await response.json()) as T;
}

export async function importMasterData(
  kind: "customers" | "suppliers" | "items",
  file: File,
): Promise<{ entity: string; created: number }> {
  const form = new FormData();
  form.append("file", file);
  return uploadForm<{ entity: string; created: number }>(`/imports/${kind}`, form);
}

export async function importOpeningBalances(
  file: File,
  fields: { period_id: string; entry_date: string; post: boolean; dry_run: boolean },
): Promise<OpeningBalanceReport> {
  const form = new FormData();
  form.append("file", file);
  form.append("period_id", fields.period_id);
  form.append("entry_date", fields.entry_date);
  form.append("post", String(fields.post));
  form.append("dry_run", String(fields.dry_run));
  return uploadForm<OpeningBalanceReport>("/imports/opening-balances", form);
}

// --- Fresh-install provisioning and tenant administration (T-29, AC-10) ---

export interface SetupStatus {
  needs_setup: boolean;
  bootstrap_configured: boolean;
  version: string;
}

export async function setupStatus(signal?: AbortSignal): Promise<SetupStatus> {
  return request<SetupStatus>("/setup/status", { company: false, signal });
}

export interface SetupPayload {
  tenant_name: string;
  tenant_slug: string;
  company_code: string;
  company_name_ar: string;
  company_name_en: string | null;
  base_currency: string;
  admin_email: string;
  admin_full_name: string;
  admin_password: string;
  require_mfa: boolean;
  fiscal_year: number | null;
}

export interface SetupResult {
  tenant_id: string;
  tenant_slug: string;
  company_id: string;
  company_code: string;
  user_id: string;
  admin_email: string;
  login_url: string;
}

export async function runSetup(payload: SetupPayload, bootstrapToken: string): Promise<SetupResult> {
  return request<SetupResult>("/setup", {
    method: "POST",
    body: payload,
    company: false,
    headers: { "X-Bootstrap-Token": bootstrapToken },
  });
}

export interface TenantUserAssignment {
  company_id: string;
  role_codes: string[];
}

export interface TenantUser {
  user_id: string;
  email: string;
  full_name: string;
  is_active: boolean;
  companies: TenantUserAssignment[];
}

export interface RoleInfo {
  code: string;
  name: string;
  is_system: boolean;
  permission_count: number;
}

export async function listTenantUsers(signal?: AbortSignal): Promise<TenantUser[]> {
  return request<TenantUser[]>("/admin/users", { signal });
}

export async function listRoles(signal?: AbortSignal): Promise<RoleInfo[]> {
  return request<RoleInfo[]>("/admin/roles", { signal });
}

export interface CreateTenantUserPayload {
  email: string;
  full_name: string;
  company_id: string;
  role_codes: string[];
  password?: string | null;
}

export interface CreatedTenantUser {
  user: TenantUser;
  invitation_token: string | null;
  invitation_expires_at: string | null;
}

export async function createTenantUser(payload: CreateTenantUserPayload): Promise<CreatedTenantUser> {
  return request<CreatedTenantUser>("/admin/users", { method: "POST", body: payload });
}

export async function setTenantUserActive(
  userId: string,
  isActive: boolean,
): Promise<{ user_id: string; is_active: boolean }> {
  return request<{ user_id: string; is_active: boolean }>("/admin/users/" + userId, {
    method: "PATCH",
    body: { is_active: isActive },
  });
}

export async function resetTenantUserPassword(
  userId: string,
): Promise<{ reset_token: string; expires_at: string }> {
  return request<{ reset_token: string; expires_at: string }>(
    "/admin/users/" + userId + "/password-reset",
    { method: "POST" },
  );
}

export interface CreatedCompany {
  company_id: string;
  code: string;
  base_currency: string;
}

export async function createCompany(payload: {
  code: string;
  name_ar: string;
  name_en: string | null;
  base_currency: string;
  fiscal_year: number | null;
}): Promise<CreatedCompany> {
  return request<CreatedCompany>("/admin/companies", { method: "POST", body: payload });
}

export async function acceptInvitation(token: string, password: string): Promise<void> {
  await request<void>("/auth/accept-invitation", {
    method: "POST",
    body: { token, password },
    company: false,
  });
}

export async function changeOwnPassword(
  currentPassword: string,
  newPassword: string,
): Promise<void> {
  await request<void>("/auth/change-password", {
    method: "POST",
    body: { current_password: currentPassword, new_password: newPassword },
  });
}
