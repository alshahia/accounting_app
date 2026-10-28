import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";

import {
  ApiError,
  createStockCount,
  createStockTransfer,
  createWarehouse,
  deleteStockCount,
  listItems,
  listStockBalances,
  listStockCounts,
  listWarehouses,
  postStockCount,
  updateWarehouse,
} from "../api";
import type { Item, StockBalance, StockCount, Warehouse } from "../api";
import { translations } from "../i18n";
import type { Lang } from "../i18n";
import { anyText, errorText, money, today } from "./utils";

interface InventoryPageProps {
  lang: Lang;
  company: { id: string; base_currency: string; permissions: string[] };
}

export function InventoryPage({ lang, company }: InventoryPageProps) {
  const t = (key: keyof (typeof translations)["ar"]): string => translations[lang][key];
  const canManage = company.permissions.includes("inventory.manage");

  const [warehouses, setWarehouses] = useState<Warehouse[]>([]);
  const [items, setItems] = useState<Item[]>([]);
  const [balances, setBalances] = useState<StockBalance[]>([]);
  const [warehouseFilter, setWarehouseFilter] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [newCode, setNewCode] = useState("");
  const [newNameAr, setNewNameAr] = useState("");
  const [newNameEn, setNewNameEn] = useState("");
  const [savingWarehouse, setSavingWarehouse] = useState(false);

  const [fromWarehouse, setFromWarehouse] = useState("");
  const [toWarehouse, setToWarehouse] = useState("");
  const [transferItem, setTransferItem] = useState("");
  const [transferQuantity, setTransferQuantity] = useState("");
  const [transferDate, setTransferDate] = useState(today);
  const [transferMemo, setTransferMemo] = useState("");
  const [savingTransfer, setSavingTransfer] = useState(false);

  const [counts, setCounts] = useState<StockCount[]>([]);
  const [countDate, setCountDate] = useState(today);
  const [countWarehouse, setCountWarehouse] = useState("");
  const [countRows, setCountRows] = useState<{ item_id: string; counted: string }[]>([
    { item_id: "", counted: "" },
  ]);
  const [savingCount, setSavingCount] = useState(false);

  const defaultWarehouse = useMemo(
    () => warehouses.find((warehouse) => warehouse.is_default) ?? null,
    [warehouses],
  );

  const load = useCallback(
    async (warehouseId: string, signal?: AbortSignal) => {
      setLoading(true);
      try {
        const [warehouseRows, itemRows, balanceRows, countRows] = await Promise.all([
          listWarehouses(signal),
          listItems(signal),
          listStockBalances(warehouseId === "" ? null : warehouseId, signal),
          listStockCounts(signal),
        ]);
        setWarehouses(warehouseRows);
        setItems(itemRows);
        setBalances(balanceRows);
        setCounts(countRows);
        setError(null);
      } catch (caught) {
        if (caught instanceof ApiError && caught.status !== 401) {
          setError(errorText(caught, lang));
        }
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(warehouseFilter, controller.signal);
    return () => controller.abort();
  }, [load, warehouseFilter]);

  useEffect(() => {
    if (warehouses.length === 0) {
      return;
    }
    const fallback = defaultWarehouse ?? warehouses[0];
    setFromWarehouse((current) =>
      current !== "" && warehouses.some((warehouse) => warehouse.id === current)
        ? current
        : fallback.id,
    );
    setToWarehouse((current) => {
      if (current !== "" && warehouses.some((warehouse) => warehouse.id === current)) {
        return current;
      }
      const other = warehouses.find((warehouse) => warehouse.id !== fallback.id);
      return other?.id ?? fallback.id;
    });
  }, [warehouses, defaultWarehouse]);

  useEffect(() => {
    setTransferItem((current) => current !== "" ? current : (items[0]?.id ?? ""));
  }, [items]);

  useEffect(() => {
    if (warehouses.length === 0) {
      return;
    }
    const fallback = defaultWarehouse ?? warehouses[0];
    setCountWarehouse((current) =>
      current !== "" && warehouses.some((warehouse) => warehouse.id === current)
        ? current
        : fallback.id,
    );
  }, [warehouses, defaultWarehouse]);

  useEffect(() => {
    setCountRows((current) =>
      current.map((row) =>
        row.item_id === "" && items.length > 0 ? { ...row, item_id: items[0].id } : row,
      ),
    );
  }, [items]);

  async function saveWarehouse(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (newCode.trim() === "" || newNameAr.trim() === "") {
      setError(t("requiredFields"));
      return;
    }
    setSavingWarehouse(true);
    setNotice(null);
    setError(null);
    try {
      await createWarehouse({
        code: newCode.trim(),
        name_ar: newNameAr.trim(),
        name_en: newNameEn.trim() === "" ? null : newNameEn.trim(),
        is_default: false,
      });
      setNewCode("");
      setNewNameAr("");
      setNewNameEn("");
      setNotice(t("warehouseCreated"));
      await load(warehouseFilter);
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setSavingWarehouse(false);
    }
  }

  async function makeDefault(warehouse: Warehouse): Promise<void> {
    setNotice(null);
    setError(null);
    try {
      await updateWarehouse(warehouse.id, {
        name_ar: warehouse.name_ar,
        name_en: warehouse.name_en,
        is_active: warehouse.is_active,
        is_default: true,
      });
      await load(warehouseFilter);
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    }
  }

  async function saveTransfer(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (fromWarehouse === "" || toWarehouse === "" || transferItem === "") {
      setError(t("requiredFields"));
      return;
    }
    setSavingTransfer(true);
    setNotice(null);
    setError(null);
    try {
      const transfer = await createStockTransfer({
        transfer_date: transferDate,
        from_warehouse_id: fromWarehouse,
        to_warehouse_id: toWarehouse,
        item_id: transferItem,
        quantity: transferQuantity,
        memo: transferMemo.trim() === "" ? null : transferMemo.trim(),
      });
      setNotice(`${t("transferCreated")} · ${transfer.number}`);
      setTransferQuantity("");
      setTransferMemo("");
      await load(warehouseFilter);
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setSavingTransfer(false);
    }
  }

  function updateCountRow(index: number, patch: Partial<{ item_id: string; counted: string }>): void {
    setCountRows((current) =>
      current.map((row, position) => (position === index ? { ...row, ...patch } : row)),
    );
  }

  function addCountRow(): void {
    setCountRows((current) => [
      ...current,
      { item_id: items[0]?.id ?? "", counted: "" },
    ]);
  }

  function removeCountRow(index: number): void {
    setCountRows((current) =>
      current.length === 1 ? current : current.filter((_, position) => position !== index),
    );
  }

  async function saveCount(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const lines = countRows
      .filter((row) => row.item_id !== "" && row.counted.trim() !== "")
      .map((row) => ({ item_id: row.item_id, counted_quantity: row.counted.trim() }));
    if (countWarehouse === "" || lines.length === 0) {
      setError(t("requiredFields"));
      return;
    }
    setSavingCount(true);
    setNotice(null);
    setError(null);
    try {
      const count = await createStockCount({
        count_date: countDate,
        warehouse_id: countWarehouse,
        lines,
      });
      setNotice(`${t("countDraftSaved")} · ${count.number}`);
      setCountRows([{ item_id: items[0]?.id ?? "", counted: "" }]);
      await load(warehouseFilter);
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    } finally {
      setSavingCount(false);
    }
  }

  async function postCount(count: StockCount): Promise<void> {
    setNotice(null);
    setError(null);
    try {
      await postStockCount(count.id);
      setNotice(`${t("countPosted")} · ${count.number}`);
      await load(warehouseFilter);
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    }
  }

  async function removeCount(count: StockCount): Promise<void> {
    setNotice(null);
    setError(null);
    try {
      await deleteStockCount(count.id);
      await load(warehouseFilter);
    } catch (caught) {
      setError(caught instanceof ApiError ? errorText(caught, lang) : t("genericError"));
    }
  }

  const warehouseName = (id: string): string => {
    const warehouse = warehouses.find((row) => row.id === id);
    return warehouse === undefined ? id : anyText(lang, warehouse.name_ar, warehouse.name_en);
  };

  return (
    <section className="page" data-testid="inventory-page">
      <header className="page-head">
        <div>
          <h1>{t("inventoryNav")}</h1>
          <p className="muted">{company.base_currency}</p>
        </div>
      </header>

      {error !== null ? <div className="alert error" role="alert">{error}</div> : null}
      {notice !== null ? <div className="alert ok" role="status">{notice}</div> : null}

      <div className="grid-2">
        <section className="card">
          <h2>{t("warehouses")}</h2>
          <p className="muted">{t("balancesHint")}</p>
          {warehouses.length === 0 ? (
            <p className="muted">{t("emptyWarehouses")}</p>
          ) : (
            <table className="data-table" data-testid="warehouse-table">
              <thead>
                <tr>
                  <th>{t("warehouseCode")}</th>
                  <th>{t("warehouseNameAr")}</th>
                  <th>{t("defaultWarehouse")}</th>
                  <th>{t("isActive")}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {warehouses.map((warehouse) => (
                  <tr key={warehouse.id} data-testid="warehouse-row">
                    <td dir="ltr">{warehouse.code}</td>
                    <td>{anyText(lang, warehouse.name_ar, warehouse.name_en)}</td>
                    <td>
                      {warehouse.is_default ? (
                        <span className="badge" data-testid="warehouse-default">{t("defaultWarehouse")}</span>
                      ) : null}
                    </td>
                    <td>{warehouse.is_active ? t("isActive") : t("inactiveWarehouse")}</td>
                    <td>
                      {!warehouse.is_default && canManage ? (
                        <button
                          type="button"
                          className="button ghost"
                          onClick={() => void makeDefault(warehouse)}
                        >
                          {t("makeDefault")}
                        </button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {canManage ? (
            <form className="stack" onSubmit={(event) => void saveWarehouse(event)} data-testid="warehouse-form">
              <h3>{t("newWarehouse")}</h3>
              <label className="field">
                <span>{t("warehouseCode")}</span>
                <input
                  value={newCode}
                  maxLength={20}
                  onChange={(event) => setNewCode(event.target.value)}
                  data-testid="warehouse-code"
                />
              </label>
              <label className="field">
                <span>{t("warehouseNameAr")}</span>
                <input
                  value={newNameAr}
                  maxLength={200}
                  onChange={(event) => setNewNameAr(event.target.value)}
                  data-testid="warehouse-name"
                />
              </label>
              <label className="field">
                <span>{t("warehouseNameEn")}</span>
                <input
                  value={newNameEn}
                  maxLength={200}
                  onChange={(event) => setNewNameEn(event.target.value)}
                />
              </label>
              <button type="submit" className="button primary" disabled={savingWarehouse}>
                {savingWarehouse ? t("loading") : t("addWarehouse")}
              </button>
            </form>
          ) : null}
        </section>

        <section className="card">
          <h2>{t("newStockTransfer")}</h2>
          {warehouses.length >= 2 ? (
            <form className="stack" onSubmit={(event) => void saveTransfer(event)} data-testid="transfer-form">
              <label className="field">
                <span>{t("date")}</span>
                <input
                  type="date"
                  value={transferDate}
                  onChange={(event) => setTransferDate(event.target.value)}
                />
              </label>
              <label className="field">
                <span>{t("fromWarehouse")}</span>
                <select
                  value={fromWarehouse}
                  onChange={(event) => setFromWarehouse(event.target.value)}
                  data-testid="transfer-from"
                >
                  {warehouses.map((warehouse) => (
                    <option key={warehouse.id} value={warehouse.id}>
                      {anyText(lang, warehouse.name_ar, warehouse.name_en)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>{t("toWarehouse")}</span>
                <select
                  value={toWarehouse}
                  onChange={(event) => setToWarehouse(event.target.value)}
                  data-testid="transfer-to"
                >
                  {warehouses.map((warehouse) => (
                    <option key={warehouse.id} value={warehouse.id}>
                      {anyText(lang, warehouse.name_ar, warehouse.name_en)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>{t("transferItem")}</span>
                <select
                  value={transferItem}
                  onChange={(event) => setTransferItem(event.target.value)}
                  data-testid="transfer-item"
                >
                  {items.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.code} · {anyText(lang, item.name_ar, item.name_en)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>{t("transferQuantity")}</span>
                <input
                  type="number"
                  min="0.0001"
                  step="0.0001"
                  value={transferQuantity}
                  onChange={(event) => setTransferQuantity(event.target.value)}
                  data-testid="transfer-quantity"
                />
              </label>
              <label className="field">
                <span>{t("transferMemo")}</span>
                <input
                  value={transferMemo}
                  maxLength={300}
                  onChange={(event) => setTransferMemo(event.target.value)}
                />
              </label>
              <button type="submit" className="button primary" disabled={savingTransfer}>
                {savingTransfer ? t("loading") : t("postTransfer")}
              </button>
            </form>
          ) : (
            <p className="muted">{t("emptyWarehouses")}</p>
          )}
        </section>
      </div>

      <section className="card">
        <div className="page-head">
          <h2>{t("stockBalances")}</h2>
          <label className="field inline">
            <span>{t("warehouses")}</span>
            <select
              value={warehouseFilter}
              onChange={(event) => setWarehouseFilter(event.target.value)}
              data-testid="balance-filter"
            >
              <option value="">{t("allWarehouses")}</option>
              {warehouses.map((warehouse) => (
                <option key={warehouse.id} value={warehouse.id}>
                  {anyText(lang, warehouse.name_ar, warehouse.name_en)}
                </option>
              ))}
            </select>
          </label>
        </div>
        {loading ? (
          <p className="muted">{t("loading")}</p>
        ) : balances.length === 0 ? (
          <p className="muted">{t("emptyBalances")}</p>
        ) : (
          <table className="data-table" data-testid="balance-table">
            <thead>
              <tr>
                <th>{t("warehouses")}</th>
                <th>{t("itemLabel")}</th>
                <th>{t("transferQuantity")}</th>
                <th>{t("value")}</th>
              </tr>
            </thead>
            <tbody>
              {balances.map((row) => (
                <tr key={`${row.warehouse_id}-${row.item_id}`} data-testid="balance-row">
                  <td>{warehouseName(row.warehouse_id)}</td>
                  <td dir="ltr">{row.item_code} · {row.item_name_ar}</td>
                  <td>{row.quantity}</td>
                  <td>{money(row.value_minor, lang)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {defaultWarehouse !== null ? (
          <p className="muted">{t("defaultWarehouse")}: {anyText(
            lang,
            defaultWarehouse.name_ar,
            defaultWarehouse.name_en,
          )}</p>
        ) : null}
      </section>

      <section className="card" data-testid="counts-card">
        <div className="page-head">
          <h2>{t("stockCounts")}</h2>
        </div>
        <p className="muted">{t("countLinesHint")}</p>
        {canManage && warehouses.length > 0 ? (
          <form className="stack" onSubmit={(event) => void saveCount(event)} data-testid="count-form">
            <h3>{t("newStockCount")}</h3>
            <label className="field">
              <span>{t("countDate")}</span>
              <input
                type="date"
                value={countDate}
                onChange={(event) => setCountDate(event.target.value)}
                data-testid="count-date"
              />
            </label>
            <label className="field">
              <span>{t("warehouses")}</span>
              <select
                value={countWarehouse}
                onChange={(event) => setCountWarehouse(event.target.value)}
                data-testid="count-warehouse"
              >
                {warehouses.map((warehouse) => (
                  <option key={warehouse.id} value={warehouse.id}>
                    {anyText(lang, warehouse.name_ar, warehouse.name_en)}
                  </option>
                ))}
              </select>
            </label>
            {countRows.map((row, index) => (
              <div className="inline-fields" key={index} data-testid="count-line">
                <label className="field">
                  <span>{t("itemLabel")}</span>
                  <select
                    value={row.item_id}
                    onChange={(event) => updateCountRow(index, { item_id: event.target.value })}
                  >
                    {items.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.code} · {anyText(lang, item.name_ar, item.name_en)}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span>{t("countedQuantity")}</span>
                  <input
                    type="number"
                    min="0"
                    step="0.0001"
                    value={row.counted}
                    onChange={(event) => updateCountRow(index, { counted: event.target.value })}
                  />
                </label>
                <button
                  type="button"
                  className="button ghost"
                  onClick={() => removeCountRow(index)}
                  disabled={countRows.length === 1}
                >
                  {t("deleteCount")}
                </button>
              </div>
            ))}
            <div className="page-actions">
              <button type="button" className="button ghost" onClick={addCountRow}>
                {t("addCountLine")}
              </button>
              <button type="submit" className="button primary" disabled={savingCount}>
                {savingCount ? t("loading") : t("saveCount")}
              </button>
            </div>
          </form>
        ) : null}
        {counts.length === 0 ? (
          <p className="muted">{t("emptyCounts")}</p>
        ) : (
          <table className="data-table" data-testid="count-table">
            <thead>
              <tr>
                <th>{t("countNumber")}</th>
                <th>{t("countDate")}</th>
                <th>{t("warehouses")}</th>
                <th>{t("countStatus")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {counts.map((count) => (
                <tr key={count.id} data-testid="count-row">
                  <td dir="ltr">{count.number}</td>
                  <td>{count.count_date}</td>
                  <td>{warehouseName(count.warehouse_id)}</td>
                  <td>
                    <span className="badge">
                      {count.status === "posted" ? t("postedStatus") : t("draftStatus")}
                    </span>
                  </td>
                  <td>
                    {canManage && count.status === "draft" ? (
                      <div className="page-actions">
                        <button
                          type="button"
                          className="button primary"
                          onClick={() => void postCount(count)}
                        >
                          {t("postCount")}
                        </button>
                        <button
                          type="button"
                          className="button ghost"
                          onClick={() => void removeCount(count)}
                        >
                          {t("deleteCount")}
                        </button>
                      </div>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </section>
  );
}
