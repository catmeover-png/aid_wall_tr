#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tracker.py — ADI PredictStreet: инкрементальный учёт кошельков -> Google Sheets.

Доступ к кошелькам НЕ нужен (только адреса). Активы: ADI (native gas), USDC, ETH.

Как работает:
  • Список кошельков читается из вкладки `wallets` (колонка A). Можно дополнять руками.
  • Первый прогон по кошельку считает всё с начала периода (PERIOD_START, по умолч. 18 мая):
    балансы на начало, обороты, расходы, разбивку. Сохраняет курсор (последний блок) и
    накопленные суммы в служебную вкладку `state`.
  • Следующие прогоны (например, раз в день из GitHub Actions) дочитывают ТОЛЬКО новые блоки
    (block > last_block) по каждому кошельку и докручивают накопленные метрики. История
    заново НЕ пересчитывается. Текущий баланс обновляется каждый прогон.
  • Итог пишется в `report` (по кошелькам) и `aggregate` (сводно). Детализация движений —
    в `flows` (дозаписью).

Балансы (без архивной ноды):
  balance(start) = current_balance − Σ(потоки c ts >= PERIOD_START)   [считается один раз]
  balance(now)   = current_balance                                    [обновляется каждый прогон]

Период открытый: [PERIOD_START, сейчас]. Метрики накапливаются.

Хранилище состояния — вкладка `state` (одна строка на кошелёк): курсор + накопленные суммы.
Защита от двойного счёта: учитываются только события с block > last_block (строгий фильтр).

Секреты (GitHub Secrets / env):
  GCP_SERVICE_ACCOUNT_JSON  — JSON сервис-аккаунта (или его base64).
  SPREADSHEET_ID            — ID Google-таблицы (из URL).
  (опц.) PERIOD_START, PREDICTSTREET_API_KEY, ETH_ADDRESS

Зависимости: requests, gspread, google-auth.
"""

import os
import re
import sys
import csv
import time
import json
import base64
import random
import logging
from decimal import Decimal, getcontext
from datetime import datetime, timezone

import requests

getcontext().prec = 80

# ============================================================================
# CONFIG
# ============================================================================
# --- Google Sheets ---
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "").strip()
WALLETS_TAB = os.environ.get("WALLETS_TAB", "wallets")
REPORT_TAB = os.environ.get("REPORT_TAB", "report")
AGGREGATE_TAB = os.environ.get("AGGREGATE_TAB", "aggregate")
STATE_TAB = os.environ.get("STATE_TAB", "state")
FLOWS_TAB = os.environ.get("FLOWS_TAB", "flows")
# Пустая строка (незаданная GitHub-переменная) трактуется как включено.
WRITE_FLOWS = (os.environ.get("WRITE_FLOWS", "1").strip() or "1") not in ("0", "false", "False", "no", "off")

# Локальный файл кошельков — используется, только если вкладка `wallets` пустая/недоступна.
WALLET_FILE = os.environ.get("WALLET_FILE", "wallets.txt")
# Локальный CSV-дамп отчёта (для артефакта Actions / отладки). Пусто -> не писать.
LOCAL_REPORT_CSV = os.environ.get("LOCAL_REPORT_CSV", "report.csv")

# --- Отчётный период (открытый: [start, сейчас]) ---
# Пустая строка (незаданная GitHub-переменная) -> дефолт, иначе будет ошибка парсинга.
PERIOD_START = os.environ.get("PERIOD_START", "").strip() or "2025-05-18T00:00:00Z"

# --- Сеть / эксплорер ---
CHAIN_ID = 36900
RPC_URL = "https://rpc.adifoundation.ai"
EXPLORER_API_BASE = "https://explorer-bls.adifoundation.ai"
PREDICTSTREET_CORE_API = "https://core-api.adipredictstreet.com"
PREDICTSTREET_API_KEY = os.environ.get("PREDICTSTREET_API_KEY", "").strip()

CONFIG_PREDICTSTREET_CONTRACTS = [
    "0xD28B8295Cd57F205e4d080Ff4c6d23C06a9EEe3a",  # ConditionalTokens (ERC-1155 outcome)
    "0x90EA87493E208A14011EC700Ac9cbAf4d064acc0",  # CTFExchange (binary)
    "0x29e58C3916d1fD235f3d1e7fCF640fd1fA8BDb1e",  # NegRiskAdapter
    "0x79ACbb874dd01044FA38a89c1478E60FaAB40D00",  # NegRiskCtfExchange
    "0x744ff21c797C16272EEaE85B9E726d1A2a4E6BBE",  # DepositLimitRegistryProxy
    "0xc16B8b190064451c2FeEb2e77c4B2aC4c7009552",  # VaultFactory
    "0x320346B72dA29a680C0314440D5A93255eb05414",  # OracleProxy (binary)
    "0xf6E9A4322b3e3a5AA00545fc206A0be1cbe0D140",  # NegRiskOracleProxy
    "0x634FFddF657Bb6B2237A404c94A0f313d376848e",  # MulticallExecutor
    "0x73df6E8F0D112D22bD672952323b43d6893AB6D2",  # Multicall3
]

USDC_ADDRESS = "0x9cb8142aEBBcdc60AF7c97Af897A67A8f3CA71C2"
CONDITIONAL_TOKENS_ADDRESS = "0xD28B8295Cd57F205e4d080Ff4c6d23C06a9EEe3a"
VAULT_FACTORY_ADDRESS = "0xc16B8b190064451c2FeEb2e77c4B2aC4c7009552"
CTF_EXCHANGE_ADDRESSES = [
    "0x90EA87493E208A14011EC700Ac9cbAf4d064acc0",
    "0x79ACbb874dd01044FA38a89c1478E60FaAB40D00",
    "0x29e58C3916d1fD235f3d1e7fCF640fd1fA8BDb1e",
]

ETH_ADDRESS = os.environ.get("ETH_ADDRESS", "").strip()  # ERC-20 ETH/WETH на сети ADI (если знаем)
ETH_SYMBOLS = {"ETH", "WETH", "ETH.E"}
ETH_DECIMALS = 18

FEE_ADDRESSES = {a.lower() for a in [] if a}  # кошельки комиссий PS, если известны

USDC_SYMBOLS = {"USDC", "USDC.E", "USDBC"}
USDC_FALLBACK_DECIMALS = 6
NATIVE_DECIMALS = 18

REQUEST_TIMEOUT = 30
MAX_RETRIES = 5
BACKOFF_BASE = 1.6
REQUEST_DELAY = 0.15
MAX_PAGES = 300
MAX_VAULT_CANDIDATES = 12
MAX_VAULTS = 4

# ============================================================================
# ЛОГИ
# ============================================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("aditracker")

# ============================================================================
# КОНСТАНТЫ / СЕССИЯ
# ============================================================================
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
ANY_ADDR_RE = re.compile(r"0x[0-9a-fA-F]{40}")
ZERO_ADDR = "0x0000000000000000000000000000000000000000"

SESSION = requests.Session()
SESSION.headers.update({
    "Accept": "application/json",
    "User-Agent": "adi-wallet-tracker/3.0 (incremental-sheets)",
})

PS_CONTRACTS = {a.lower() for a in CONFIG_PREDICTSTREET_CONTRACTS if a}
PS_LABELS = {
    USDC_ADDRESS.lower(): "USDC",
    CONDITIONAL_TOKENS_ADDRESS.lower(): "ConditionalTokens",
    VAULT_FACTORY_ADDRESS.lower(): "VaultFactory",
    "0x90ea87493e208a14011ec700ac9cbaf4d064acc0": "CTFExchange",
    "0x79acbb874dd01044fa38a89c1478e60faab40d00": "NegRiskCtfExchange",
    "0x29e58c3916d1fd235f3d1e7fcf640fd1fa8bdb1e": "NegRiskAdapter",
    "0x744ff21c797c16272eeae85b9e726d1a2a4e6bbe": "DepositLimitRegistry",
    "0x320346b72da29a680c0314440d5a93255eb05414": "OracleProxy",
    "0xf6e9a4322b3e3a5aa00545fc206a0be1cbe0d140": "NegRiskOracleProxy",
    "0x634ffddf657bb6b2237a404c94a0f313d376848e": "MulticallExecutor",
    "0x73df6e8f0d112d22bd672952323b43d6893ab6d2": "Multicall3",
}
CTF_EXCHANGE_SET = {a.lower() for a in CTF_EXCHANGE_ADDRESSES if a}
TRADE_VENUE_SET = set(CTF_EXCHANGE_SET)
if ADDR_RE.match(CONDITIONAL_TOKENS_ADDRESS or ""):
    TRADE_VENUE_SET.add(CONDITIONAL_TOKENS_ADDRESS.lower())


# ============================================================================
# ХЕЛПЕРЫ
# ============================================================================
def is_valid_address(a):
    return bool(a) and bool(ADDR_RE.match(a.strip()))


def low(a):
    return a.lower() if isinstance(a, str) else a


def g(d, *keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def addr_of(node):
    if node is None:
        return None
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        return g(node, "hash", "address", "address_hash")
    return None


def to_int(x, default=0):
    try:
        return int(x)
    except Exception:
        try:
            return int(float(x))
        except Exception:
            return default


def to_dec(x, default="0"):
    try:
        return Decimal(str(x if x not in (None, "") else default))
    except Exception:
        return Decimal(default)


def raw_to_decimal(raw, decimals):
    try:
        raw = Decimal(str(raw))
    except Exception:
        return Decimal(0)
    decimals = to_int(decimals, 0)
    if decimals <= 0:
        return raw
    return raw / (Decimal(10) ** decimals)


def parse_ts(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        s = value.strip().replace("Z", "+00:00")
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(value.strip(), fmt).replace(tzinfo=timezone.utc)
            except Exception:
                continue
        return None
    return None


def iso(dt):
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fnum(dec, places):
    if dec is None:
        return 0.0
    try:
        return float(round(dec, places))
    except Exception:
        try:
            return float(dec)
        except Exception:
            return 0.0


def cp_label(addr):
    if not addr:
        return ""
    return PS_LABELS.get(addr.lower(), "")


# ============================================================================
# HTTP С RETRY
# ============================================================================
def http_get_json(url, params=None, ctx=""):
    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
            status = r.status_code
            if status == 200:
                time.sleep(REQUEST_DELAY)
                try:
                    return r.json()
                except ValueError:
                    return None
            if status == 404:
                return None
            if status == 429 or 500 <= status < 600:
                ra = r.headers.get("Retry-After")
                wait = float(ra) if (ra and ra.replace(".", "", 1).isdigit()) \
                    else (BACKOFF_BASE ** attempt) + random.uniform(0, 0.5)
                log.warning("HTTP %s %s — retry %d/%d через %.1fs", status, ctx or url, attempt, MAX_RETRIES, wait)
                time.sleep(wait)
                last = f"HTTP {status}"
                continue
            log.warning("HTTP %s %s — пропуск", status, ctx or url)
            return None
        except requests.RequestException as e:
            wait = (BACKOFF_BASE ** attempt) + random.uniform(0, 0.5)
            log.warning("Сеть %s (%s) — retry %d/%d через %.1fs", e.__class__.__name__, ctx or url, attempt, MAX_RETRIES, wait)
            time.sleep(wait)
            last = str(e)
    log.warning("Не удалось после %d попыток: %s (%s)", MAX_RETRIES, ctx or url, last)
    return None


def rpc_call(method, params, ctx=""):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = SESSION.post(RPC_URL, json=payload, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                time.sleep(REQUEST_DELAY)
                data = r.json()
                if isinstance(data, dict) and "result" in data:
                    return data["result"]
                return None
            if r.status_code == 429 or 500 <= r.status_code < 600:
                time.sleep((BACKOFF_BASE ** attempt) + random.uniform(0, 0.5))
                continue
            return None
        except (requests.RequestException, ValueError):
            time.sleep((BACKOFF_BASE ** attempt) + random.uniform(0, 0.5))
    return None


def v2_url(path):
    return f"{EXPLORER_API_BASE}/api/v2/{path.lstrip('/')}"


def paginate_v2(path, base_params=None, ctx=""):
    params = dict(base_params or {})
    pages = 0
    while True:
        data = http_get_json(v2_url(path), params=params, ctx=ctx or path)
        if not isinstance(data, dict):
            return
        for item in data.get("items", []) or []:
            yield item
        nxt = data.get("next_page_params")
        pages += 1
        if not nxt or pages >= MAX_PAGES:
            if pages >= MAX_PAGES and nxt:
                log.warning("MAX_PAGES=%d для %s — данные неполные", MAX_PAGES, ctx or path)
            return
        params = dict(base_params or {})
        params.update(nxt)


def _block_of(it):
    return to_int(g(it, "block_number", "block", "blockNumber"), 0)


# ============================================================================
# РЕЕСТР PS-КОНТРАКТОВ
# ============================================================================
def discover_predictstreet_contracts():
    found = set()
    headers = {"X-Api-Key": PREDICTSTREET_API_KEY} if PREDICTSTREET_API_KEY else {}
    for path in ("/api/platform/contracts", "/api/platform/config"):
        try:
            r = SESSION.get(PREDICTSTREET_CORE_API + path, headers=headers, timeout=REQUEST_TIMEOUT)
            if r.status_code != 200:
                continue
            for m in ANY_ADDR_RE.findall(r.text or ""):
                found.add(m.lower())
        except requests.RequestException:
            continue
    new = found - PS_CONTRACTS
    if new:
        log.info("core-api: +%d PS-контракт(ов)", len(new))
        PS_CONTRACTS.update(found)
    return found


# ============================================================================
# ФЕТЧЕРЫ (с инкрементальным early-stop по block > since_block)
# ============================================================================
def _paginate_since(path, since_block, ctx):
    """Идём от новых к старым, останавливаемся, когда block <= since_block."""
    for it in paginate_v2(path, ctx=ctx):
        blk = _block_of(it)
        if since_block and blk and blk <= since_block:
            return
        yield it, blk


def fetch_transactions(address, since_block=0):
    out = []
    for it, blk in _paginate_since(f"addresses/{address}/transactions", since_block, f"txs {address[:10]}"):
        fee = g(it, "fee", default={}) or {}
        fee_raw = g(fee, "value")
        if fee_raw is None:
            gu, gp = g(it, "gas_used"), g(it, "gas_price")
            try:
                fee_raw = str(int(gu) * int(gp)) if (gu is not None and gp is not None) else "0"
            except Exception:
                fee_raw = "0"
        status = str(g(it, "status", "result", default="")).lower()
        out.append({
            "hash": low(g(it, "hash")),
            "from": low(addr_of(g(it, "from"))),
            "to": low(addr_of(g(it, "to"))),
            "value_raw": g(it, "value", default="0"),
            "fee_raw": fee_raw,
            "success": status in ("ok", "success", "1", "true", ""),
            "blk": blk,
            "ts": parse_ts(g(it, "timestamp", "block_timestamp")),
        })
    return out


def fetch_internal_transactions(address, since_block=0):
    out = []
    for it, blk in _paginate_since(f"addresses/{address}/internal-transactions", since_block, f"intx {address[:10]}"):
        err = g(it, "error")
        succ = g(it, "success")
        success = (str(succ).lower() in ("true", "1", "ok", "success")) if succ is not None else (err in (None, ""))
        out.append({
            "tx_hash": low(g(it, "transaction_hash", "tx_hash")),
            "from": low(addr_of(g(it, "from"))),
            "to": low(addr_of(g(it, "to"))),
            "value_raw": g(it, "value", default="0"),
            "blk": blk,
            "ts": parse_ts(g(it, "timestamp", "block_timestamp")),
            "success": success,
        })
    return out


def fetch_token_transfers(address, since_block=0):
    out = []
    for it, blk in _paginate_since(f"addresses/{address}/token-transfers", since_block, f"transfers {address[:10]}"):
        token = g(it, "token", default={}) or {}
        total = g(it, "total", default={}) or {}
        ttype = g(token, "type") or g(it, "type") or ""
        value_raw = g(total, "value")
        if value_raw is None:
            value_raw = g(it, "value", default="0")
        decimals = g(total, "decimals")
        if decimals is None:
            decimals = g(token, "decimals", default=0)
        out.append({
            "tx_hash": low(g(it, "transaction_hash", "tx_hash")),
            "token_addr": low(addr_of(g(token, "address", "address_hash")) or g(token, "address")),
            "token_symbol": (g(token, "symbol") or "").upper(),
            "token_type": str(ttype).upper(),
            "decimals": decimals,
            "from": low(addr_of(g(it, "from"))),
            "to": low(addr_of(g(it, "to"))),
            "value_raw": value_raw,
            "blk": blk,
            "ts": parse_ts(g(it, "timestamp", "block_timestamp")),
        })
    return out


def address_info(address):
    return http_get_json(v2_url(f"addresses/{address}"), ctx=f"addr-info {address[:10]}")


def fetch_token_balances(address):
    out = []
    for it in paginate_v2(f"addresses/{address}/tokens", ctx=f"tokens {address[:10]}"):
        token = g(it, "token", default={}) or {}
        out.append({
            "token_addr": low(addr_of(g(token, "address", "address_hash")) or g(token, "address")),
            "symbol": (g(token, "symbol") or "").upper(),
            "decimals": g(token, "decimals", default=0),
            "value_raw": g(it, "value", default="0"),
            "type": str(g(token, "type") or "").upper(),
        })
    return out


# ============================================================================
# БАЛАНСЫ
# ============================================================================
def get_native_balance(address):
    info = address_info(address)
    if isinstance(info, dict):
        cb = g(info, "coin_balance")
        if cb is not None:
            return raw_to_decimal(cb, NATIVE_DECIMALS)
    data = http_get_json(f"{EXPLORER_API_BASE}/api",
                         params={"module": "account", "action": "balance", "address": address},
                         ctx="eth-compat balance")
    if isinstance(data, dict) and str(g(data, "status")) == "1" and g(data, "result") is not None:
        return raw_to_decimal(g(data, "result"), NATIVE_DECIMALS)
    res = rpc_call("eth_getBalance", [address, "latest"], ctx="eth_getBalance")
    if isinstance(res, str) and res.startswith("0x"):
        try:
            return raw_to_decimal(int(res, 16), NATIVE_DECIMALS)
        except ValueError:
            pass
    return Decimal(0)


def get_token_balance_via_rpc(token_addr, address):
    if not is_valid_address(token_addr):
        return None
    data = "0x70a08231" + "0" * 24 + address.lower().replace("0x", "")
    res = rpc_call("eth_call", [{"to": token_addr, "data": data}, "latest"], ctx="balanceOf")
    if isinstance(res, str) and res.startswith("0x") and len(res) >= 3:
        try:
            return int(res, 16)
        except ValueError:
            return None
    return None


def get_current_balances(wallet):
    adi = get_native_balance(wallet)
    tokens = fetch_token_balances(wallet)
    usdc_target = USDC_ADDRESS.lower() if is_valid_address(USDC_ADDRESS) else None
    eth_target = ETH_ADDRESS.lower() if is_valid_address(ETH_ADDRESS) else None
    usdc = eth = None
    for t in tokens:
        if usdc is None and ((usdc_target and t["token_addr"] == usdc_target) or
                             (not usdc_target and t["symbol"] in USDC_SYMBOLS)):
            dec = t["decimals"] if t["decimals"] not in (None, 0) else USDC_FALLBACK_DECIMALS
            usdc = raw_to_decimal(t["value_raw"], dec)
        if eth is None and ((eth_target and t["token_addr"] == eth_target) or (t["symbol"] in ETH_SYMBOLS)):
            dec = t["decimals"] if t["decimals"] not in (None, 0) else ETH_DECIMALS
            eth = raw_to_decimal(t["value_raw"], dec)
    if usdc is None and usdc_target:
        raw = get_token_balance_via_rpc(USDC_ADDRESS, wallet)
        if raw is not None:
            usdc = raw_to_decimal(raw, USDC_FALLBACK_DECIMALS)
    if eth is None and eth_target:
        raw = get_token_balance_via_rpc(ETH_ADDRESS, wallet)
        if raw is not None:
            eth = raw_to_decimal(raw, ETH_DECIMALS)
    return {"adi": adi or Decimal(0), "usdc": usdc or Decimal(0), "eth": eth or Decimal(0)}


# ============================================================================
# VAULT
# ============================================================================
def resolve_vaults(wallet, transfers):
    vaults = set()
    if PREDICTSTREET_API_KEY:
        try:
            r = SESSION.get(PREDICTSTREET_CORE_API + "/api/me/vault",
                            headers={"X-Api-Key": PREDICTSTREET_API_KEY}, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                for m in ANY_ADDR_RE.findall(r.text or ""):
                    if m.lower() != wallet.lower():
                        vaults.add(m.lower())
        except requests.RequestException:
            pass
    if not is_valid_address(VAULT_FACTORY_ADDRESS):
        return vaults
    factory = VAULT_FACTORY_ADDRESS.lower()
    usdc_target = USDC_ADDRESS.lower() if is_valid_address(USDC_ADDRESS) else None
    w = wallet.lower()
    candidates, seen = [], set()
    for tr in transfers:
        is_usdc = (usdc_target and tr["token_addr"] == usdc_target) or \
                  (not usdc_target and tr["token_symbol"] in USDC_SYMBOLS)
        if not is_usdc:
            continue
        for cp in (tr["from"], tr["to"]):
            if cp and cp != w and cp != ZERO_ADDR and cp not in PS_CONTRACTS and cp not in seen:
                seen.add(cp)
                candidates.append(cp)
    for cp in candidates[:MAX_VAULT_CANDIDATES]:
        info = address_info(cp)
        if not isinstance(info, dict) or not g(info, "is_contract", default=False):
            continue
        creator = low(addr_of(g(info, "creator_address_hash", "creator_address")))
        impls = g(info, "implementations", default=[]) or []
        impl_addrs = {low(addr_of(x)) for x in impls if x}
        if creator == factory or factory in impl_addrs:
            vaults.add(cp)
        if len(vaults) >= MAX_VAULTS:
            break
    return vaults


# ============================================================================
# СОБЫТИЯ
# ============================================================================
def _ev(ts, blk, delta, kind, cp, txh):
    return {"ts": ts, "blk": blk, "delta": delta, "kind": kind, "cp": cp, "tx": txh}


def native_events(wallet, txs, internal_txs):
    w = wallet.lower()
    ev = []
    for tx in txs:
        ts, blk, h = tx["ts"], tx["blk"], tx["hash"]
        frm, to = tx["from"], tx["to"]
        val = raw_to_decimal(tx["value_raw"], NATIVE_DECIMALS)
        fee = raw_to_decimal(tx["fee_raw"], NATIVE_DECIMALS)
        if tx["success"] and val > 0:
            if to == w and frm != w:
                ev.append(_ev(ts, blk, val, "value_in", frm, h))
            elif frm == w and to != w:
                ev.append(_ev(ts, blk, -val, "value_out", to, h))
        if frm == w and fee > 0:
            ev.append(_ev(ts, blk, -fee, "gas", to, h))
    for itx in internal_txs:
        if not itx.get("success", True):
            continue
        val = raw_to_decimal(itx["value_raw"], NATIVE_DECIMALS)
        if val <= 0:
            continue
        ts, blk, h = itx["ts"], itx["blk"], itx["tx_hash"]
        frm, to = itx["from"], itx["to"]
        if to == w and frm != w:
            ev.append(_ev(ts, blk, val, "value_in", frm, h))
        elif frm == w and to != w:
            ev.append(_ev(ts, blk, -val, "value_out", to, h))
    return ev


def token_events(wallet, transfers, match_fn, default_decimals):
    w = wallet.lower()
    ev = []
    for tr in transfers:
        if not match_fn(tr):
            continue
        if tr["token_type"] and tr["token_type"] not in ("ERC-20", "ERC20", ""):
            continue
        dec = tr["decimals"] if tr["decimals"] not in (None, 0) else default_decimals
        amt = raw_to_decimal(tr["value_raw"], dec)
        ts, blk, h = tr["ts"], tr["blk"], tr["tx_hash"]
        if tr["to"] == w and tr["from"] != w:
            ev.append(_ev(ts, blk, amt, "token_in", tr["from"], h))
        elif tr["from"] == w and tr["to"] != w:
            ev.append(_ev(ts, blk, -amt, "token_out", tr["to"], h))
    return ev


def is_usdc_tr(tr):
    usdc_target = USDC_ADDRESS.lower() if is_valid_address(USDC_ADDRESS) else None
    return (usdc_target and tr["token_addr"] == usdc_target) or (tr["token_symbol"] in USDC_SYMBOLS)


def is_eth_tr(tr):
    eth_target = ETH_ADDRESS.lower() if is_valid_address(ETH_ADDRESS) else None
    return (eth_target and tr["token_addr"] == eth_target) or (tr["token_symbol"] in ETH_SYMBOLS)


def asset_start_balance(events, current, period_start):
    """Баланс на начало периода = текущий − Σ(потоки ts >= period_start)."""
    after = sum((e["delta"] for e in events if e["ts"] and e["ts"] >= period_start), Decimal(0))
    return current - after


# ============================================================================
# ИНКРЕМЕНТ ПО КОШЕЛЬКУ
# ============================================================================
def _keep(blk, ts, since_block, last_ts):
    if blk and blk > 0:
        return blk > since_block
    return (ts is not None) and (last_ts is None or ts > last_ts)


def scan_wallet(wallet, vaults, since_block, last_ts, period_start, is_first):
    """Возвращает (increments, vaults, max_block, max_ts, min_ts, balances, start_bals|None, flows)."""
    w = wallet.lower()

    txs = fetch_transactions(wallet, since_block)
    itxs = fetch_internal_transactions(wallet, since_block)
    transfers = fetch_token_transfers(wallet, since_block)

    # резолвим vault, если ещё не знаем
    if not vaults:
        vaults = resolve_vaults(wallet, transfers)
    own_set = {w} | {v.lower() for v in vaults}
    platform_set = PS_CONTRACTS | own_set

    combined_transfers = list(transfers)
    for v in list(vaults)[:MAX_VAULTS]:
        combined_transfers += fetch_token_transfers(v, since_block)

    balances = get_current_balances(wallet)

    adi_ev = native_events(wallet, txs, itxs)
    usdc_ev = token_events(wallet, transfers, is_usdc_tr, USDC_FALLBACK_DECIMALS)
    eth_ev = token_events(wallet, transfers, is_eth_tr, ETH_DECIMALS)

    # стартовые балансы — только на первом прогоне (нужна вся история)
    start_bals = None
    if is_first:
        start_bals = {
            "adi": asset_start_balance(adi_ev, balances["adi"], period_start),
            "usdc": asset_start_balance(usdc_ev, balances["usdc"], period_start),
            "eth": asset_start_balance(eth_ev, balances["eth"], period_start),
        }

    def kept(evs):
        return [e for e in evs if _keep(e["blk"], e["ts"], since_block, last_ts)
                and e["ts"] and e["ts"] >= period_start]

    adi_k, usdc_k, eth_k = kept(adi_ev), kept(usdc_ev), kept(eth_ev)

    def is_third(cp):
        return bool(cp) and cp != ZERO_ADDR and cp not in own_set and cp not in PS_CONTRACTS

    inc = {k: Decimal(0) for k in (
        "adi_gas", "adi_in", "adi_out", "adi_to_third", "adi_to_platform",
        "usdc_in", "usdc_out", "usdc_deposit", "usdc_from_platform", "usdc_to_third",
        "eth_in", "eth_out", "other_fees", "turnover")}
    inc["trades"] = 0
    inc["tx"] = 0

    # ADI
    for e in adi_k:
        if e["kind"] == "gas":
            inc["adi_gas"] += -e["delta"]
        elif e["kind"] == "value_in":
            inc["adi_in"] += e["delta"]
        elif e["kind"] == "value_out":
            amt = -e["delta"]
            inc["adi_out"] += amt
            if is_third(e["cp"]):
                inc["adi_to_third"] += amt
            else:
                inc["adi_to_platform"] += amt
    # USDC
    for e in usdc_k:
        if e["kind"] == "token_in":
            inc["usdc_in"] += e["delta"]
            if e["cp"] in platform_set:
                inc["usdc_from_platform"] += e["delta"]
        elif e["kind"] == "token_out":
            amt = -e["delta"]
            inc["usdc_out"] += amt
            if e["cp"] in FEE_ADDRESSES:
                inc["other_fees"] += amt
            elif e["cp"] in platform_set:
                inc["usdc_deposit"] += amt
            if is_third(e["cp"]):
                inc["usdc_to_third"] += amt
    # ETH
    for e in eth_k:
        if e["kind"] == "token_in":
            inc["eth_in"] += e["delta"]
        elif e["kind"] == "token_out":
            inc["eth_out"] += -e["delta"]

    # оборот / трейды (EOA + vault), только новые блоки
    ct_addr = CONDITIONAL_TOKENS_ADDRESS.lower() if is_valid_address(CONDITIONAL_TOKENS_ADDRESS) else None
    trade_txs = set()
    for tr in combined_transfers:
        if not _keep(tr["blk"], tr["ts"], since_block, last_ts):
            continue
        if not (tr["ts"] and tr["ts"] >= period_start):
            continue
        is_ct = (ct_addr and tr["token_addr"] == ct_addr) or (tr["token_type"] == "ERC-1155")
        if is_ct and ((tr["from"] in own_set) or (tr["to"] in own_set)) and tr["tx_hash"]:
            trade_txs.add(tr["tx_hash"])
        if is_usdc_tr(tr):
            dec = tr["decimals"] if tr["decimals"] not in (None, 0) else USDC_FALLBACK_DECIMALS
            amt = raw_to_decimal(tr["value_raw"], dec)
            a, b = tr["from"], tr["to"]
            if (a in own_set and b in TRADE_VENUE_SET) or (b in own_set and a in TRADE_VENUE_SET):
                inc["turnover"] += amt
    inc["trades"] = len(trade_txs)
    inc["tx"] = len({tx["hash"] for tx in txs
                     if tx["hash"] and _keep(tx["blk"], tx["ts"], since_block, last_ts)
                     and tx["ts"] and tx["ts"] >= period_start})

    # курсоры
    all_blk = [e["blk"] for e in (adi_k + usdc_k + eth_k) if e["blk"]] + \
              [tr["blk"] for tr in combined_transfers if tr["blk"]]
    all_ts = [e["ts"] for e in (adi_k + usdc_k + eth_k) if e["ts"]]
    max_block = max(all_blk) if all_blk else since_block
    max_ts = max(all_ts) if all_ts else last_ts
    min_ts = min(all_ts) if all_ts else None

    # flows-детализация (новые события)
    flows = []
    now_iso = iso(datetime.now(timezone.utc))
    for asset, evs in (("ADI", adi_k), ("USDC", usdc_k), ("ETH", eth_k)):
        places = 6 if asset == "USDC" else 8
        for e in evs:
            if e["kind"] == "gas":
                direction, category = "out", "gas"
            elif e["kind"] in ("value_out", "token_out"):
                direction = "out"
                if e["cp"] in FEE_ADDRESSES:
                    category = "fee"
                elif e["cp"] in platform_set:
                    category = "to-platform"
                else:
                    category = "to-third-party"
            else:
                direction = "in"
                category = "from-platform" if e["cp"] in platform_set else "incoming"
            flows.append([wallet, iso(e["ts"]), asset, direction, category,
                          abs(fnum(e["delta"], places)), e["cp"] or "", cp_label(e["cp"]),
                          e["tx"] or "", now_iso])

    return inc, vaults, max_block, max_ts, min_ts, balances, start_bals, flows


# ============================================================================
# СОСТОЯНИЕ / СБОРКА СТРОК
# ============================================================================
STATE_COLUMNS = [
    "wallet", "vaults", "period_start",
    "start_adi", "start_usdc", "start_eth",
    "last_block", "last_ts", "first_activity", "last_activity", "last_run",
    "cum_adi_gas", "cum_adi_in", "cum_adi_out", "cum_adi_to_third", "cum_adi_to_platform",
    "cum_usdc_in", "cum_usdc_out", "cum_usdc_deposit", "cum_usdc_from_platform", "cum_usdc_to_third",
    "cum_eth_in", "cum_eth_out", "cum_other_fees", "cum_turnover", "cum_trades", "cum_tx",
    "cur_adi", "cur_usdc", "cur_eth",
]

REPORT_COLUMNS = [
    "wallet",
    "adi_start", "adi_now", "adi_change",
    "usdc_start", "usdc_now", "usdc_change",
    "eth_start", "eth_now", "eth_change",
    "usdc_trade_turnover", "trades_count",
    "adi_spent_total", "adi_gas_fees", "adi_transferred_out",
    "adi_to_third_parties", "adi_to_platform",
    "usdc_deposited_to_platform", "usdc_received_from_platform", "usdc_to_third_parties",
    "usdc_in_total", "usdc_out_total", "eth_in_total", "eth_out_total",
    "other_fees_usdc", "tx_count",
    "vaults", "period_start", "first_activity", "last_activity", "last_updated",
    "reconciliation", "summary", "notes",
]

FLOW_COLUMNS = ["wallet", "ts", "asset", "direction", "category", "amount",
                "counterparty", "counterparty_label", "tx_hash", "ingested_at"]

SUM_FOR_AGG = [
    "usdc_trade_turnover", "trades_count", "adi_spent_total", "adi_gas_fees",
    "adi_transferred_out", "adi_to_third_parties", "adi_to_platform",
    "usdc_deposited_to_platform", "usdc_received_from_platform", "usdc_to_third_parties",
    "usdc_in_total", "usdc_out_total", "eth_in_total", "eth_out_total",
    "other_fees_usdc", "tx_count",
    "adi_start", "adi_now", "usdc_start", "usdc_now", "eth_start", "eth_now",
]


def merge_state(prev, inc, vaults, max_block, max_ts, min_ts, balances, start_bals, period_start):
    """Слить предыдущее состояние с инкрементом -> новое состояние."""
    def pget(k):
        return to_dec(prev.get(k, "0")) if prev else Decimal(0)

    st = {}
    st["wallet"] = (prev["wallet"] if prev else "")
    st["vaults"] = ",".join(sorted(vaults))
    st["period_start"] = (prev.get("period_start") if prev and prev.get("period_start") else iso(period_start))

    if start_bals is not None:  # первый прогон
        st["start_adi"] = start_bals["adi"]
        st["start_usdc"] = start_bals["usdc"]
        st["start_eth"] = start_bals["eth"]
    else:
        st["start_adi"] = pget("start_adi")
        st["start_usdc"] = pget("start_usdc")
        st["start_eth"] = pget("start_eth")

    st["last_block"] = max(to_int(prev.get("last_block")) if prev else 0, max_block or 0)
    prev_last_ts = parse_ts(prev.get("last_ts")) if prev else None
    new_last_ts = max([t for t in (prev_last_ts, max_ts) if t], default=None)
    st["last_ts"] = iso(new_last_ts)

    prev_first = parse_ts(prev.get("first_activity")) if prev else None
    first = min([t for t in (prev_first, min_ts) if t], default=None)
    st["first_activity"] = iso(first)
    last_act = max([t for t in (prev_first, prev_last_ts, max_ts, min_ts) if t], default=None)
    st["last_activity"] = iso(last_act)
    st["last_run"] = iso(datetime.now(timezone.utc))

    pairs = [
        ("cum_adi_gas", "adi_gas"), ("cum_adi_in", "adi_in"), ("cum_adi_out", "adi_out"),
        ("cum_adi_to_third", "adi_to_third"), ("cum_adi_to_platform", "adi_to_platform"),
        ("cum_usdc_in", "usdc_in"), ("cum_usdc_out", "usdc_out"), ("cum_usdc_deposit", "usdc_deposit"),
        ("cum_usdc_from_platform", "usdc_from_platform"), ("cum_usdc_to_third", "usdc_to_third"),
        ("cum_eth_in", "eth_in"), ("cum_eth_out", "eth_out"), ("cum_other_fees", "other_fees"),
        ("cum_turnover", "turnover"),
    ]
    for skey, ikey in pairs:
        st[skey] = pget(skey) + inc[ikey]
    st["cum_trades"] = (to_int(prev.get("cum_trades")) if prev else 0) + inc["trades"]
    st["cum_tx"] = (to_int(prev.get("cum_tx")) if prev else 0) + inc["tx"]

    st["cur_adi"] = balances["adi"]
    st["cur_usdc"] = balances["usdc"]
    st["cur_eth"] = balances["eth"]
    return st


def state_to_report(st):
    start_adi, now_adi = to_dec(st["start_adi"]), to_dec(st["cur_adi"])
    start_usdc, now_usdc = to_dec(st["start_usdc"]), to_dec(st["cur_usdc"])
    start_eth, now_eth = to_dec(st["start_eth"]), to_dec(st["cur_eth"])
    gas = to_dec(st["cum_adi_gas"])
    adi_out = to_dec(st["cum_adi_out"])
    spent = gas + adi_out

    adi_net = to_dec(st["cum_adi_in"]) - adi_out - gas
    usdc_net = to_dec(st["cum_usdc_in"]) - to_dec(st["cum_usdc_out"])
    adi_diff = (start_adi + adi_net) - now_adi
    usdc_diff = (start_usdc + usdc_net) - now_usdc
    recon = f"ADI Δ-расхожд={fnum(adi_diff,6)}; USDC Δ-расхожд={fnum(usdc_diff,6)}"

    summary = (
        f"USDC: {fnum(start_usdc,2)} → {fnum(now_usdc,2)} (Δ {fnum(now_usdc-start_usdc,2)}). "
        f"Депозиты в платформу {fnum(to_dec(st['cum_usdc_deposit']),2)}, из платформы {fnum(to_dec(st['cum_usdc_from_platform']),2)}, "
        f"третьим {fnum(to_dec(st['cum_usdc_to_third']),2)}. Оборот ~{fnum(to_dec(st['cum_turnover']),2)} USDC, трейдов {to_int(st['cum_trades'])}. "
        f"ADI потрачено {fnum(spent,6)} (gas {fnum(gas,6)} + переводы {fnum(adi_out,6)}; третьим {fnum(to_dec(st['cum_adi_to_third']),6)})."
    )
    notes = []
    if not st.get("vaults"):
        notes.append("vault не найден")
    if not is_valid_address(ETH_ADDRESS):
        notes.append("ETH по символу")

    return {
        "wallet": st["wallet"],
        "adi_start": fnum(start_adi, 8), "adi_now": fnum(now_adi, 8), "adi_change": fnum(now_adi - start_adi, 8),
        "usdc_start": fnum(start_usdc, 6), "usdc_now": fnum(now_usdc, 6), "usdc_change": fnum(now_usdc - start_usdc, 6),
        "eth_start": fnum(start_eth, 8), "eth_now": fnum(now_eth, 8), "eth_change": fnum(now_eth - start_eth, 8),
        "usdc_trade_turnover": fnum(to_dec(st["cum_turnover"]), 6),
        "trades_count": to_int(st["cum_trades"]),
        "adi_spent_total": fnum(spent, 8),
        "adi_gas_fees": fnum(gas, 8),
        "adi_transferred_out": fnum(adi_out, 8),
        "adi_to_third_parties": fnum(to_dec(st["cum_adi_to_third"]), 8),
        "adi_to_platform": fnum(to_dec(st["cum_adi_to_platform"]), 8),
        "usdc_deposited_to_platform": fnum(to_dec(st["cum_usdc_deposit"]), 6),
        "usdc_received_from_platform": fnum(to_dec(st["cum_usdc_from_platform"]), 6),
        "usdc_to_third_parties": fnum(to_dec(st["cum_usdc_to_third"]), 6),
        "usdc_in_total": fnum(to_dec(st["cum_usdc_in"]), 6),
        "usdc_out_total": fnum(to_dec(st["cum_usdc_out"]), 6),
        "eth_in_total": fnum(to_dec(st["cum_eth_in"]), 8),
        "eth_out_total": fnum(to_dec(st["cum_eth_out"]), 8),
        "other_fees_usdc": fnum(to_dec(st["cum_other_fees"]), 6),
        "tx_count": to_int(st["cum_tx"]),
        "vaults": st.get("vaults", ""),
        "period_start": st.get("period_start", ""),
        "first_activity": st.get("first_activity", ""),
        "last_activity": st.get("last_activity", ""),
        "last_updated": st.get("last_run", ""),
        "reconciliation": recon,
        "summary": summary,
        "notes": "; ".join(notes),
    }


def state_to_row(st):
    out = []
    for c in STATE_COLUMNS:
        v = st.get(c, "")
        if isinstance(v, Decimal):
            v = format(v.normalize(), "f")
        out.append(v)
    return out


# ============================================================================
# GOOGLE SHEETS I/O
# ============================================================================
def get_spreadsheet():
    import gspread
    from google.oauth2.service_account import Credentials
    raw = os.environ.get("GCP_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        raise RuntimeError("GCP_SERVICE_ACCOUNT_JSON не задан")
    if not raw.startswith("{"):
        raw = base64.b64decode(raw).decode("utf-8")
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    if not SPREADSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID не задан")
    return gc.open_by_key(SPREADSHEET_ID)


def ws_get(sh, title, rows=200, cols=40):
    import gspread
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)


def read_wallets_from_sheet(sh):
    try:
        ws = ws_get(sh, WALLETS_TAB)
        col = ws.col_values(1)
    except Exception as e:
        log.warning("Не удалось прочитать вкладку '%s': %s", WALLETS_TAB, e)
        return []
    skip = {"wallet", "address", "адрес", "кошелек", "кошелёк", "wallets"}
    wallets, seen = [], set()
    for v in col:
        a = (v or "").strip()
        if a.lower() in skip:
            continue
        if is_valid_address(a) and a.lower() not in seen:
            seen.add(a.lower())
            wallets.append(a)
    return wallets


def read_wallets_txt(path):
    if not os.path.exists(path):
        return []
    wallets, seen = [], set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            a = line.strip()
            if not a or a.startswith("#") or not is_valid_address(a):
                continue
            if a.lower() not in seen:
                seen.add(a.lower())
                wallets.append(a)
    return wallets


def read_state(sh):
    try:
        ws = ws_get(sh, STATE_TAB)
        recs = ws.get_all_records()
    except Exception as e:
        log.warning("Состояние не прочитано (старт с нуля): %s", e)
        return {}
    out = {}
    for r in recs:
        w = str(r.get("wallet", "")).strip()
        if w:
            out[w.lower()] = r
    return out


def write_table(sh, title, header, rows):
    ws = ws_get(sh, title, rows=max(len(rows) + 10, 20), cols=max(len(header), 10))
    values = [header] + rows
    ws.clear()
    ws.resize(rows=max(len(values) + 5, 20), cols=max(len(header), 10))
    ws.update(range_name="A1", values=values, value_input_option="RAW")


def append_flows(sh, rows):
    if not rows:
        return
    ws = ws_get(sh, FLOWS_TAB, rows=1000, cols=len(FLOW_COLUMNS))
    existing = ws.get_all_values()
    payload = []
    if not existing:
        payload.append(FLOW_COLUMNS)
    payload.extend(rows)
    for i in range(0, len(payload), 500):
        ws.append_rows(payload[i:i + 500], value_input_option="RAW")


def write_aggregate(sh, report_rows, period_start):
    metrics = [["period_start", iso(period_start)],
               ["wallets", len(report_rows)],
               ["last_updated", iso(datetime.now(timezone.utc))]]
    for col in SUM_FOR_AGG:
        total = 0.0
        for r in report_rows:
            try:
                total += float(r.get(col) or 0)
            except Exception:
                pass
        metrics.append([f"total_{col}", round(total, 8)])
    write_table(sh, AGGREGATE_TAB, ["metric", "value"], metrics)


def write_local_csv(report_rows):
    if not LOCAL_REPORT_CSV:
        return
    try:
        with open(LOCAL_REPORT_CSV, "w", newline="", encoding="utf-8") as f:
            wr = csv.DictWriter(f, fieldnames=REPORT_COLUMNS)
            wr.writeheader()
            for r in report_rows:
                wr.writerow({c: r.get(c, "") for c in REPORT_COLUMNS})
        log.info("Локальный CSV: %s", LOCAL_REPORT_CSV)
    except Exception as e:
        log.warning("Локальный CSV не записан: %s", e)


# ============================================================================
# MAIN
# ============================================================================
def main():
    period_start = parse_ts(PERIOD_START)
    if not period_start:
        log.error("Некорректный PERIOD_START=%r", PERIOD_START)
        sys.exit(1)

    log.info("ADI tracker (incremental) | период с %s", iso(period_start))
    sh = get_spreadsheet()
    discover_predictstreet_contracts()
    log.info("PS-контрактов: %d", len(PS_CONTRACTS))

    wallets = read_wallets_from_sheet(sh)
    if not wallets:
        wallets = read_wallets_txt(WALLET_FILE)
    if not wallets:
        log.error("Список кошельков пуст (вкладка '%s' и %s).", WALLETS_TAB, WALLET_FILE)
        sys.exit(1)
    log.info("Кошельков: %d", len(wallets))

    state = read_state(sh)
    report_rows, state_rows, flows_all = [], [], []

    for i, w in enumerate(wallets, 1):
        log.info("[%d/%d] %s", i, len(wallets), w)
        prev = state.get(w.lower())
        is_first = prev is None
        vaults = set(filter(None, (prev.get("vaults", "").split(",") if prev else []))) if prev else set()
        since_block = to_int(prev.get("last_block")) if prev else 0
        last_ts = parse_ts(prev.get("last_ts")) if prev else None
        try:
            inc, vaults, max_block, max_ts, min_ts, balances, start_bals, flows = scan_wallet(
                w, vaults, since_block, last_ts, period_start, is_first)
            st = merge_state(prev, inc, vaults, max_block, max_ts, min_ts, balances, start_bals, period_start)
            st["wallet"] = w
            report_rows.append(state_to_report(st))
            state_rows.append(state_to_row(st))
            if WRITE_FLOWS:
                flows_all.extend(flows)
            log.info("  %s | usdc %s->%s | turnover+%s trades+%d tx+%d | last_block=%s",
                     "FIRST" if is_first else "inc",
                     fnum(to_dec(st["start_usdc"]), 2), fnum(to_dec(st["cur_usdc"]), 2),
                     fnum(inc["turnover"], 2), inc["trades"], inc["tx"], st["last_block"])
        except Exception as e:
            log.exception("Ошибка по %s: %s", w, e)
            if prev:  # сохраняем прежнее состояние без изменений
                state_rows.append(state_to_row({**prev, "wallet": w}))
                try:
                    report_rows.append(state_to_report({**prev, "wallet": w}))
                except Exception:
                    pass

    write_table(sh, STATE_TAB, STATE_COLUMNS, state_rows)
    write_table(sh, REPORT_TAB, REPORT_COLUMNS,
                [[r.get(c, "") for c in REPORT_COLUMNS] for r in report_rows])
    write_aggregate(sh, report_rows, period_start)
    if WRITE_FLOWS:
        append_flows(sh, flows_all)
    write_local_csv(report_rows)
    log.info("Готово: report=%d, state=%d, flows+%d", len(report_rows), len(state_rows), len(flows_all))


if __name__ == "__main__":
    main()
