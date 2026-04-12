#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Xiaomi Find Device API Server — playwright_api_server.py

端点：
  GET  /health      健康检查
  GET  /status      会话状态概览
  POST /auth/ensure 确保 session 已鉴权（后台触发登录）
  POST /login       主动登录（携带凭据）
  POST /sync        同步所有设备位置

所有 URL、参数名、响应字段严格来自 HAR（i.mi.com.har），禁止臆造。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import random
import re
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cryptography.fernet import Fernet

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException
from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from pydantic import BaseModel


# ══════════════════════════════════════════════════════════════════
# 常量与配置（来自 HAR 实测）
# ══════════════════════════════════════════════════════════════════

VERSION = "2026.03.25.1"

# ── 存储 ──
STORAGE_DIR = Path(os.getenv("STORAGE_STATE_PATH", "/data"))
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

# ── 凭据加密主密钥 ──
# 优先级：环境变量 CREDENTIALS_MASTER_KEY > /data/master.key（首次启动自动生成并持久化）
# 环境变量：SHA-256 派生；/data/master.key：直接存 Fernet key（Fernet.generate_key() 输出）
_MASTER_KEY_FILE = STORAGE_DIR / "master.key"

def _init_fernet() -> Fernet:
    _log = logging.getLogger(__name__)
    raw = os.getenv("CREDENTIALS_MASTER_KEY", "")
    if raw:
        key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())
        _log.info("[Credentials] master key loaded from env")
        return Fernet(key)
    if _MASTER_KEY_FILE.exists():
        key = _MASTER_KEY_FILE.read_bytes().strip()
        _log.info("[Credentials] master key loaded from %s", _MASTER_KEY_FILE)
        return Fernet(key)
    key = Fernet.generate_key()
    _MASTER_KEY_FILE.write_bytes(key)
    _log.info("[Credentials] master key generated and saved to %s", _MASTER_KEY_FILE)
    return Fernet(key)

_FERNET: Fernet = _init_fernet()

# ── 小米端点（来自 HAR） ──
XIAOMI_FIND_URL       = "https://i.mi.com/find"
XIAOMI_API_BASE       = "https://i.mi.com"
XIAOMI_ACCOUNT_DOMAIN = "account.xiaomi.com"

# API 路径（来自 HAR 实测）
PATH_DEVICE_LIST   = "/find/v3/device/status/list"    # GET，返回设备列表
PATH_DEVICE_STATUS = "/find/v3/device/status"         # GET，返回单设备详情
PATH_COMMAND_SEND  = "/find/v2/device/command/send"   # POST，发送设备命令（来自 HAR）
PATH_FAMILY_LIST   = "/find/v2/device/family/list"    # GET，家庭/共享设备列表（来自 HAR）

# ── 命令冷却（秒，可通过环境变量覆盖） ──
RING_COOLDOWN_SECS       = int(os.getenv("RING_COOLDOWN_SECS", "30"))
LOCATE_CMD_COOLDOWN_SECS = int(os.getenv("LOCATE_CMD_COOLDOWN_SECS", "60"))

# ── force_locate 参数（可通过环境变量覆盖） ──
LOCATE_STALE_THRESHOLD_SECS = int(os.getenv("LOCATE_STALE_THRESHOLD_SECS", "300"))
LOCATE_COOLDOWN_SECS        = int(os.getenv("LOCATE_COOLDOWN_SECS", "60"))
LOCATE_POLL_INTERVAL_SECS   = int(os.getenv("LOCATE_POLL_INTERVAL_SECS", "4"))
LOCATE_POLL_MAX_WAIT_SECS   = int(os.getenv("LOCATE_POLL_MAX_WAIT_SECS", "30"))

# ── poll 并发限制（全局 semaphore） ──
LOCATE_POLL_MAX_CONCURRENCY = int(os.getenv("LOCATE_POLL_MAX_CONCURRENCY", "2"))

# ── Playwright 调用最小间隔保护（秒） ──
HEARTBEAT_MIN_INTERVAL_SECS    = int(os.getenv("HEARTBEAT_MIN_INTERVAL_SECS", "300"))
REFRESH_PAGE_MIN_INTERVAL_SECS = int(os.getenv("REFRESH_PAGE_MIN_INTERVAL_SECS", "120"))
STS_MIN_INTERVAL_SECS          = int(os.getenv("STS_MIN_INTERVAL_SECS", "120"))

# ── recovery 最小触发间隔（秒） ──
RECOVERY_MIN_INTERVAL_SECS = int(os.getenv("RECOVERY_MIN_INTERVAL_SECS", "60"))

# ── User-Agent（来自 HAR request headers） ──
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
)

# ── 固定请求头（来自 HAR request headers，排除 Cookie/ts 动态值） ──
XIAOMI_BASE_HEADERS: Dict[str, str] = {
    "accept":             "*/*",
    "accept-language":    "zh-CN,zh;q=0.9",
    "referer":            XIAOMI_FIND_URL,
    "sec-ch-ua":          '"Not(A:Brand";v="8", "Chromium";v="144", "Google Chrome";v="144"',
    "sec-ch-ua-mobile":   "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest":     "empty",
    "sec-fetch-mode":     "cors",
    "sec-fetch-site":     "same-origin",
    "User-Agent":         USER_AGENT,
}

# ── Playwright stealth 脚本（与华为版保持一致） ──
STEALTH_INIT_SCRIPT = r"""
(() => {
  try {
    const newProto = navigator.__proto__ || Object.getPrototypeOf(navigator);
    if (newProto) {
      Object.defineProperty(newProto, 'webdriver', { get: () => undefined });
    }
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh'] });
    Object.defineProperty(navigator, 'platform',  { get: () => 'Win32' });
    window.chrome = window.chrome || { runtime: {} };
    Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3] });

    if (window.navigator.permissions && window.navigator.permissions.query) {
      const origQuery = window.navigator.permissions.query.bind(window.navigator.permissions);
      window.navigator.permissions.query = (params) => {
        if (params && params.name === 'notifications') {
          return Promise.resolve({ state: Notification.permission });
        }
        return origQuery(params);
      };
    }

    try {
      Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 4 });
      Object.defineProperty(navigator, 'deviceMemory',        { get: () => 4 });
    } catch (e) {}

    try {
      const getParameter = WebGLRenderingContext.prototype.getParameter;
      WebGLRenderingContext.prototype.getParameter = function(parameter) {
        if (parameter === 37445) return 'Intel Inc.';
        if (parameter === 37446) return 'Intel Iris OpenGL Engine';
        return getParameter.call(this, parameter);
      };
    } catch (e) {}
  } catch (e) {}
})();
"""


# ══════════════════════════════════════════════════════════════════
# Pydantic 请求/响应模型（字段来自 HAR）
# ══════════════════════════════════════════════════════════════════

class LoginReq(BaseModel):
    """POST /auth/ensure 和 POST /login 请求体"""
    session_key: str
    username: str = ""
    password: str = ""


class SyncReq(BaseModel):
    """POST /sync 请求体"""
    session_key: str
    # force_locate 保留字段，与华为版接口对齐；小米不需要主动触发，此字段忽略。
    force_locate: bool = False


class GpsEntry:
    """
    单条 GPS 信息，对应 gpsInfoTransformed 数组中的一个元素。
    HAR 实测字段：latitude, longitude, accuracy, clientUpdateTime,
                  coordinateType, address, addressComponent,
                  sourceType, area, inChinaMainLand
    """
    __slots__ = (
        "latitude", "longitude", "accuracy", "client_update_time",
        "coordinate_type", "address", "address_component",
        "source_type", "area", "in_china_main_land",
    )

    def __init__(self, raw: Dict[str, Any]) -> None:
        self.latitude: Optional[float]  = raw.get("latitude")
        self.longitude: Optional[float] = raw.get("longitude")
        self.accuracy: Optional[float]  = raw.get("accuracy")
        self.client_update_time: Optional[int] = raw.get("clientUpdateTime")
        self.coordinate_type: str  = raw.get("coordinateType", "")
        self.address: str          = raw.get("address", "")
        self.address_component: str = raw.get("addressComponent", "")
        self.source_type: str      = raw.get("sourceType", "")
        self.area: str             = raw.get("area", "")
        self.in_china_main_land: bool = raw.get("inChinaMainLand", False)

    def valid(self) -> bool:
        return self.latitude is not None and self.longitude is not None


class BatteryInfo:
    """HAR 实测字段：level (int), clientUpdateTime (ms)"""
    __slots__ = ("level", "client_update_time")

    def __init__(self, raw: Dict[str, Any]) -> None:
        self.level: Optional[int] = raw.get("level")
        self.client_update_time: Optional[int] = raw.get("clientUpdateTime")


class DeviceOutput(BaseModel):
    """
    /sync 响应中每个设备的标准化输出。
    与华为插件对齐的核心字段：device_id, name, latitude, longitude, battery, ts
    小米扩展字段（来自 HAR）：fid, device_type, accuracy, coord_type,
                               fix_time (ISO8601), address, raw
    """
    # ── 通用字段（与华为版对齐）──
    device_id: str
    name: str
    model: str         = ""                 # 同 name，供 HA 插件协议使用
    latitude: Optional[float]  = None
    longitude: Optional[float] = None
    battery: Optional[int]     = None
    ts: Optional[int]          = None       # Unix 秒时间戳
    stale: bool                = False      # True=返回缓存数据
    stale_reason: str  = ""                 # 缓存原因（供 HA 插件协议使用）

    # ── 小米扩展字段 ──
    fid: str           = ""
    device_type: str   = ""
    accuracy: Optional[float]  = None
    coord_type: str    = ""                 # 固定输出 "wgs84"
    fix_time: Optional[str]    = None       # ISO8601 UTC
    locate_time: Optional[int] = None       # Unix 秒时间戳（gpsInfoTransformed[0].clientUpdateTime）
    address: str       = ""
    address_component: str = ""
    area: str          = ""                 # gpsInfoTransformed[0].area
    source_type: str   = ""                 # gpsInfoTransformed[0].sourceType
    in_china_mainland: bool = False         # gpsInfoTransformed[0].inChinaMainLand
    online: bool       = False
    raw: Optional[Dict[str, Any]] = None

    # ── 电量扩展字段 ──
    battery_time: Optional[int] = None     # batteryInfo.clientUpdateTime（Unix 秒）

    # ── 设备状态字段（来自 status API data 顶层）──
    share_location: bool  = False           # data.shareLocation
    is_locating: bool     = False           # data.isLocating
    status: str           = ""             # data.status
    is_self_device: bool  = False           # data.isSelfDevice

    # ── 多坐标系字段（调试/属性展示用）──
    raw_coord_type: str        = ""         # 小米原始 coordinateType
    raw_lat: Optional[float]   = None       # 原始纬度（未转换）
    raw_lon: Optional[float]   = None       # 原始经度（未转换）
    gcj02_lat: Optional[float] = None       # GCJ-02 纬度（高德/逆地理用）
    gcj02_lon: Optional[float] = None
    bd09_lat: Optional[float]  = None       # BD-09 纬度（百度原始）
    bd09_lon: Optional[float]  = None

    # ── 命令参数（供 /command/* 路由调用，由 do_sync 写入缓存）──
    user_id: str      = ""   # targetUserId（从 cookie/deviceList 提取）
    component_id: str = ""   # targetComponentId（componentList[0].componentId）


# ══════════════════════════════════════════════════════════════════
# Session 状态管理
# ══════════════════════════════════════════════════════════════════

logger = logging.getLogger(__name__)


@dataclass
class SessionState:
    """
    单账号会话状态。

    浏览器会话（context / page）全局复用。
    refresh_session() 负责软续命（不清 cookie）。
    full_login() 是最后手段（清 cookie + 重走登录页面）。
    """
    session_key: str
    storage_path: Path

    credentials: Dict[str, str] = field(default_factory=dict)
    device_cache: Dict[str, Dict] = field(default_factory=dict)
    device_list: List[Dict] = field(default_factory=list)

    # ── 全局 recovery 锁（单飞：同一时刻只有一个恢复/登录流程）──
    recovery_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    recovery_task: Optional[asyncio.Task] = None
    last_login_attempt: float   = 0.0
    last_login_failed: float    = 0.0

    need_reauth: bool           = False
    last_err_reason: Optional[str] = None

    user_id: str = ""    # 从 cookie 提取（小米用 userId）

    # fid -> 上次触发 syncMode=2 的 Unix 时间戳（限频用）
    locate_last_trigger: Dict[str, float] = field(default_factory=dict)

    # fid -> 上次 ring 命令的 Unix 时间戳（防误触冷却）
    ring_last_trigger: Dict[str, float] = field(default_factory=dict)

    # fid -> 上次 poll_until_fresh 开始的 Unix 时间戳（去重用）
    last_poll_ts: Dict[str, float] = field(default_factory=dict)

    # ── Session 续命机制 ──
    runtime_cookies: Dict[str, str] = field(default_factory=dict)   # 运行时捕获的最新 cookie
    last_cookie_persist: float = 0.0       # 上次将 cookie 持久化到 storage_state 的时间
    last_browser_heartbeat: float = 0.0    # 上次浏览器心跳时间
    last_successful_sync: float = 0.0      # 上次成功 /sync 的时间
    heartbeat_count_today: int = 0         # 今天心跳次数
    heartbeat_count_date: str = ""         # 计数日期（YYYY-MM-DD）

    # ── 分层登录状态字段 ──
    last_full_login_ts: float   = 0.0      # 上次 full_login 完成时间
    last_refresh_ts: float      = 0.0      # 上次 refresh_session 完成时间
    last_probe_ok_ts: float     = 0.0      # 上次 probe 验证成功时间
    last_probe_ts: float        = 0.0      # 上次 probe 调用时间（缓存用，<5s 跳过）
    login_in_progress: bool     = False    # 是否有恢复流程（refresh 或 full）正在执行
    refresh_fail_count: int     = 0        # refresh_session 连续失败次数（full_login 成功后归零）

    # ── 认证状态机 ──
    # OK / NEED_REFRESH / NEED_LOGIN / AUTH_UNAVAILABLE
    auth_state: str = "UNKNOWN"

    # ── storage_state 写锁（防止并发写入竞态）──
    storage_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _persist_task: Optional[asyncio.Task] = field(default=None, repr=False)  # 去重用

    # ── 浏览器会话（账号级单例，全局复用）──
    context: Optional[Any] = field(default=None)   # BrowserContext（唯一，禁止多创建）
    page: Optional[Any]    = field(default=None)   # Page（主页面，长期复用）
    heartbeat_page: Optional[Any] = field(default=None)  # 心跳专用长期 page
    refresh_page: Optional[Any] = field(default=None)    # refresh_session 专用 page


class GlobalState:
    """管理 Playwright browser + 所有 session。"""

    def __init__(self) -> None:
        self.browser: Optional[Browser] = None
        self.playwright = None
        self.sessions: Dict[str, SessionState] = {}
        self.http_session: Optional[aiohttp.ClientSession] = None

    def get_session(self, session_key: str) -> SessionState:
        if session_key not in self.sessions:
            storage_path = STORAGE_DIR / f"storage_state_{session_key}.json"
            sess = SessionState(
                session_key=session_key,
                storage_path=storage_path,
            )
            cred = _load_credentials(sess)
            if cred:
                sess.credentials = cred
            self.sessions[session_key] = sess
            logger.debug(f"[Session] 创建新 session: {session_key[:8]}")
        return self.sessions[session_key]


# 模块级单例
state = GlobalState()

# ── 全局 poll 并发 semaphore（在 lifespan 中初始化） ──
poll_semaphore: Optional[asyncio.Semaphore] = None


def _set_auth_state(sess: SessionState, new_state: str) -> None:
    """统一设置 auth_state，状态变化时打印日志。"""
    old = sess.auth_state
    if old == new_state:
        return
    sess.auth_state = new_state
    logger.info(
        f"[AuthState] {old} → {new_state}，session={sess.session_key[:8]}"
    )

async def _save_credentials(sess: "SessionState", username: str, password: str) -> None:
    """加密写盘。"""
    cred_path = STORAGE_DIR / f"credentials_{sess.session_key}.json"
    try:
        async with sess.storage_lock:
            password_enc = _FERNET.encrypt(password.encode()).decode()
            cred_path.write_text(
                json.dumps({"username": username, "password_enc": password_enc}, ensure_ascii=False),
                encoding="utf-8",
            )
        logger.info("[Credentials] saved session=%s", sess.session_key[:8])
    except Exception as e:
        logger.warning("[Credentials] save failed session=%s: %s", sess.session_key[:8], e)


def _load_credentials(sess: "SessionState") -> Optional[Dict[str, str]]:
    """
    从磁盘读取凭据，支持加密格式（password_enc）和旧明文格式（password）。
    解密失败时标记 sess.need_reauth，返回 None。
    """
    cred_path = STORAGE_DIR / f"credentials_{sess.session_key}.json"
    if not cred_path.exists():
        return None
    try:
        payload = json.loads(cred_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[Credentials] load failed session=%s: %s", sess.session_key[:8], e)
        return None

    username = payload.get("username", "")
    if not username:
        return None

    password_enc = payload.get("password_enc")
    if password_enc:
        try:
            password = _FERNET.decrypt(password_enc.encode()).decode()
            logger.info("[Credentials] loaded session=%s", sess.session_key[:8])
            return {"username": username, "password": password}
        except Exception:
            logger.warning("[Credentials] decrypt failed session=%s", sess.session_key[:8])
            sess.need_reauth = True
            sess.last_err_reason = "CREDENTIALS_DECRYPT_FAILED"
            return None

    # 旧明文格式向后兼容
    password = payload.get("password", "")
    if password:
        logger.warning("[Credentials] loaded plaintext (legacy) session=%s", sess.session_key[:8])
        return {"username": username, "password": password}

    return None


# ── Playwright 调用节流时间戳（模块级） ──
_last_sts_call_ts: float = 0.0
_last_refresh_page_call_ts: float = 0.0
_last_recovery_trigger_ts: float = 0.0


async def get_cookies_from_storage(storage_path: Path) -> Optional[Dict[str, str]]:
    """从 Playwright storage_state JSON 提取 cookie dict。"""
    if not storage_path.exists():
        logger.debug(f"[Cookies] storage_state 不存在: {storage_path.name}")
        return None
    try:
        data = json.loads(storage_path.read_text(encoding="utf-8"))
        cookies: Dict[str, str] = {}
        for cookie in data.get("cookies", []):
            name = cookie.get("name", "")
            if name:
                cookies[name] = cookie.get("value", "")
        logger.debug(
            f"[Cookies] 从 {storage_path.name} 提取 {len(cookies)} 个 cookie，"
            f"serviceToken={bool(cookies.get('serviceToken'))}, "
            f"userId={bool(cookies.get('userId'))}"
        )
        return cookies if cookies else None
    except Exception as e:
        logger.exception(f"[Cookies] 解析失败 {storage_path.name}: {e}")
        return None


async def get_cookies_from_context(sess) -> Optional[Dict[str, str]]:
    """从活跃的 Playwright browser context 提取最新 cookie（轻量 IPC，无页面导航）。
    浏览器页面上的 JS 会通过 XHR 持续刷新 cookie，context cookie jar 始终比
    storage 文件快照更新鲜，优先使用可避免 ~15 分钟一次的 401 循环。"""
    ctx = sess.context
    if ctx is None:
        return None
    try:
        raw_cookies = await ctx.cookies("https://i.mi.com")
        if not raw_cookies:
            return None
        cookies = {c["name"]: c["value"] for c in raw_cookies if c.get("name")}
        if cookies:
            logger.debug(
                f"[Cookies] 从 context 提取 {len(cookies)} 个 cookie，"
                f"serviceToken={bool(cookies.get('serviceToken'))}, "
                f"userId={bool(cookies.get('userId'))}"
            )
        return cookies if cookies else None
    except Exception as e:
        logger.debug(f"[Cookies] context 提取失败（降级到 storage）: {e}")
        return None


def hydrate_user_id_from_cookies(sess: SessionState, cookies: Dict[str, str]) -> bool:
    """从 cookie 提取 userId 注入 session。优先级：cookie.userId > cookie.cUserId"""
    uid = cookies.get("userId") or cookies.get("cUserId") or ""
    if uid:
        sess.user_id = uid
    return bool(sess.user_id)


def cookies_to_header(cookies: Dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if k)


# ── Session 续命：Set-Cookie 捕获 + storage_state 热更新 ──

# 需要追踪的关键 cookie 名（小米 auth 相关）
_MI_AUTH_COOKIES = {"serviceToken", "userId", "cUserId",
                    "i.mi.com_slh", "i.mi.com_ph",
                    "i.mi.com_isvalid_servicetoken", "i.mi.com_istrudev"}

_COOKIE_PERSIST_INTERVAL = int(os.getenv("COOKIE_PERSIST_INTERVAL", "120"))


def _absorb_set_cookies(sess: SessionState, response_headers) -> bool:
    """
    从 API 响应头中捕获 Set-Cookie，更新 sess.runtime_cookies。
    返回 True 表示有 auth 相关 cookie 被更新。
    """
    changed = False
    raw_cookies = response_headers.getall("Set-Cookie", [])
    for raw in raw_cookies:
        # 简单解析：取第一段 name=value
        parts = raw.split(";", 1)
        if "=" not in parts[0]:
            continue
        name, value = parts[0].strip().split("=", 1)
        name = name.strip()
        if not name:
            continue
        old = sess.runtime_cookies.get(name)
        if old != value:
            sess.runtime_cookies[name] = value
            if name in _MI_AUTH_COOKIES:
                changed = True
                logger.debug(f"[SetCookie] {name} 已更新（来自 API 响应 Set-Cookie）")

    # ── Set-Cookie 持久化限流：changed 时延迟合并写（runtime_cookies 已更新，确保不丢）──
    if changed:
        now = time.time()
        if now - sess.last_cookie_persist < 5:
            logger.debug("[SetCookie] defer persist: changed but <5s, schedule merge write")
            _schedule_persist_cookies(sess)
            return False
    return changed


async def _persist_cookies_to_storage(sess: SessionState) -> None:
    """
    将 runtime_cookies 的更新合并回 storage_state JSON（原子写入）。
    仅在有新 cookie 且距上次写入 > _COOKIE_PERSIST_INTERVAL 时执行。
    持有 sess.storage_lock 防止与 _save_storage_state 竞态。
    """
    now = time.time()
    if now - sess.last_cookie_persist < _COOKIE_PERSIST_INTERVAL:
        return
    if not sess.storage_path.exists():
        return
    if not sess.runtime_cookies:
        return

    async with sess.storage_lock:
        try:
            data = json.loads(sess.storage_path.read_text(encoding="utf-8"))
            existing = {c["name"]: c for c in data.get("cookies", [])}

            updated = False
            for name, value in sess.runtime_cookies.items():
                if name in existing:
                    if existing[name].get("value") != value:
                        existing[name]["value"] = value
                        updated = True
                else:
                    # 新 cookie，添加到列表（使用 i.mi.com 域）
                    existing[name] = {
                        "name": name, "value": value,
                        "domain": ".i.mi.com", "path": "/",
                        "httpOnly": False, "secure": True, "sameSite": "None",
                    }
                    updated = True

            if not updated:
                sess.last_cookie_persist = now
                return

            data["cookies"] = list(existing.values())
            tmp = sess.storage_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(sess.storage_path)
            sess.last_cookie_persist = now
            logger.info(
                f"[SetCookie] storage_state 已热更新，"
                f"session={sess.session_key[:8]}, cookies={len(data['cookies'])}个"
            )
        except Exception as e:
            logger.warning(f"[SetCookie] 持久化失败: {e}")


def _schedule_persist_cookies(sess: SessionState) -> None:
    """去重调度：如果已有 persist task 在运行，跳过创建新 task。"""
    if sess._persist_task and not sess._persist_task.done():
        return
    sess._persist_task = asyncio.create_task(_persist_cookies_to_storage(sess))


# ── full_login 冷却（可通过环境变量覆盖） ──
_FULL_LOGIN_COOLDOWN_SECS = int(os.getenv("FULL_LOGIN_COOLDOWN_SECS", "600"))
_FULL_LOGIN_FAIL_COOLDOWN = int(os.getenv("FULL_LOGIN_FAIL_COOLDOWN", "30"))
_FULL_LOGIN_RATE_LIMIT    = int(os.getenv("FULL_LOGIN_RATE_LIMIT", "60"))


def trigger_recovery_nowait(sess: SessionState, reason: str = "UNKNOWN") -> None:
    """
    Fire-and-forget 触发后台恢复流程。

    单飞保证：
      - recovery_lock 确保同一时刻只有一个恢复流程
      - 并发请求直接跳过（已有任务在运行）
      - 最小触发间隔保护（RECOVERY_MIN_INTERVAL_SECS）

    流程：先 refresh_session()，失败后才 full_login()。
    """
    global _last_recovery_trigger_ts

    # ── AuthState 拦截：AUTH_UNAVAILABLE 禁止任何恢复操作 ──
    if sess.auth_state == "AUTH_UNAVAILABLE":
        logger.debug(
            f"[AuthState] skip: AUTH_UNAVAILABLE，禁止恢复操作，"
            f"session={sess.session_key[:8]}, reason={reason}"
        )
        return

    # ── 单飞检查（双重保险：task + lock）──
    if sess.recovery_task and not sess.recovery_task.done():
        logger.info(
            f"[Recovery] skip: running，session={sess.session_key[:8]}, "
            f"新 reason={reason}，等待已有任务完成"
        )
        return

    if sess.recovery_lock.locked():
        logger.info(
            f"[Recovery] skip: locked，session={sess.session_key[:8]}, "
            f"新 reason={reason}，等待已有任务完成"
        )
        return

    # ── 熔断：无凭据 + 无 storage → recovery 不可能成功，直接跳过 ──
    has_creds   = bool(sess.credentials.get("username") and sess.credentials.get("password"))
    has_storage = sess.storage_path.exists()
    if not has_creds and not has_storage:
        _set_auth_state(sess, "AUTH_UNAVAILABLE")
        logger.debug(
            f"[Recovery] skip: no credentials and no storage，session={sess.session_key[:8]}, "
            f"reason={reason}"
        )
        return

    # ── 熔断：连续失败过多且无凭据 → 指数退避 ──
    if sess.refresh_fail_count >= 5 and not has_creds:
        # 退避间隔：min(base * 2^(fail-5), 3600)，即 60→120→240→...→3600
        backoff = min(RECOVERY_MIN_INTERVAL_SECS * (2 ** (sess.refresh_fail_count - 5)), 3600)
        now_bo = time.time()
        since_last_refresh = now_bo - sess.last_refresh_ts if sess.last_refresh_ts > 0 else float("inf")
        if since_last_refresh < backoff:
            logger.warning(
                f"[Recovery] skip: backoff，session={sess.session_key[:8]}, "
                f"reason={reason}，refresh_fail_count={sess.refresh_fail_count}，"
                f"距上次 {since_last_refresh:.0f}s < 退避 {backoff:.0f}s，"
                f"请提供凭据以解除熔断"
            )
            return

    # ── 最小触发间隔保护 ──
    now = time.time()
    since_last = now - _last_recovery_trigger_ts
    if _last_recovery_trigger_ts > 0 and since_last < RECOVERY_MIN_INTERVAL_SECS:
        remaining = RECOVERY_MIN_INTERVAL_SECS - since_last
        logger.info(
            f"[Recovery] skip: min interval，session={sess.session_key[:8]}, "
            f"reason={reason}，距上次触发 {since_last:.0f}s < {RECOVERY_MIN_INTERVAL_SECS}s，"
            f"剩余 {remaining:.0f}s"
        )
        return

    _last_recovery_trigger_ts = now
    sess.recovery_task = asyncio.create_task(_recovery_pipeline(sess, reason))
    logger.info(f"[Recovery] start: 启动后台恢复流程，session={sess.session_key[:8]}, reason={reason}")


async def _recovery_pipeline(sess: SessionState, reason: str) -> None:
    """
    两层恢复管线（全程持有 recovery_lock，保证单飞）：
      1. refresh_session() — 软续命（不清 cookie，不走登录页面）
      2. full_login()      — 最后手段（清 cookie + 重走完整登录）

    full_login 触发条件（必须同时满足）：
      - refresh_session 失败
      - 不在冷却期内
      - 有凭据
    """
    async with sess.recovery_lock:
        # ── AuthState 拦截 ──
        if sess.auth_state == "AUTH_UNAVAILABLE":
            logger.debug(
                f"[AuthState] skip: AUTH_UNAVAILABLE，禁止恢复操作，"
                f"session={sess.session_key[:8]}, reason={reason}"
            )
            return

        # ── 入口熔断：无 storage + 无凭据 → AUTH_UNAVAILABLE ──
        has_storage_rp = sess.storage_path.exists()
        has_creds_rp = bool(sess.credentials.get("username") and sess.credentials.get("password"))
        if not has_storage_rp and not has_creds_rp:
            _set_auth_state(sess, "AUTH_UNAVAILABLE")
            logger.warning(
                f"[AuthState] AUTH_UNAVAILABLE（无 storage + 无凭据），"
                f"session={sess.session_key[:8]}, reason={reason}"
            )
            return

        sess.login_in_progress = True
        try:
            # ── Layer 1: refresh_session ──
            logger.info(
                f"[Recovery] L1 refresh_session start，session={sess.session_key[:8]}, reason={reason}"
            )
            refresh_ok = await refresh_session(sess)
            if refresh_ok:
                sess.refresh_fail_count = 0
                logger.info(
                    f"[Recovery] L1 refresh_session success，无需 full_login，"
                    f"session={sess.session_key[:8]}"
                )
                return

            sess.refresh_fail_count += 1
            if sess.refresh_fail_count >= 3:
                # 连续失败 ≥3 次，输出增强诊断信息
                logger.error(
                    f"[Recovery] L1 refresh_session fail（连续第 {sess.refresh_fail_count} 次），"
                    f"session={sess.session_key[:8]}, "
                    f"last_probe_ok={sess.last_probe_ok_ts:.0f}, "
                    f"last_full_login={sess.last_full_login_ts:.0f}, "
                    f"last_err={sess.last_err_reason}, "
                    f"has_storage={sess.storage_path.exists()}, "
                    f"has_context={sess.context is not None}, "
                    f"has_page={sess.page is not None}"
                )
            else:
                logger.warning(
                    f"[Recovery] L1 refresh_session fail（连续第 {sess.refresh_fail_count} 次），"
                    f"session={sess.session_key[:8]}"
                )

            # ── 熔断：refresh 连续失败 ≥5 且无凭据 → 不再尝试 full_login，直接退出 ──
            if sess.refresh_fail_count >= 5 and not (
                sess.credentials.get("username") and sess.credentials.get("password")
            ):
                logger.error(
                    f"[Recovery] circuit breaker: refresh 连续失败 {sess.refresh_fail_count} 次"
                    f"且无凭据，停止无效恢复，session={sess.session_key[:8]}，"
                    f"请调用 /login 或 /auth/ensure 提供凭据"
                )
                return

            # ── Layer 2: full_login（受冷却限制）──
            now = time.time()
            since_last_full = now - sess.last_full_login_ts
            if sess.last_full_login_ts > 0 and since_last_full < _FULL_LOGIN_COOLDOWN_SECS:
                remaining = _FULL_LOGIN_COOLDOWN_SECS - since_last_full
                logger.warning(
                    f"[Recovery] L2 full_login skip: 冷却中，session={sess.session_key[:8]}, "
                    f"距上次 {since_last_full:.0f}s < {_FULL_LOGIN_COOLDOWN_SECS}s, "
                    f"剩余 {remaining:.0f}s"
                )
                return

            if not sess.credentials.get("username") or not sess.credentials.get("password"):
                logger.error(
                    f"[Recovery] L2 full_login skip: 无凭据，session={sess.session_key[:8]}"
                )
                return

            logger.info(
                f"[Recovery] L2 full_login start，session={sess.session_key[:8]}, "
                f"reason={reason}, refresh_fail_count={sess.refresh_fail_count}"
            )
            await full_login(sess)

        finally:
            sess.login_in_progress = False
            sess.recovery_task = None


# ══════════════════════════════════════════════════════════════════
# 浏览器生命周期管理（全局唯一 browser，每 session 唯一 context/page）
# ══════════════════════════════════════════════════════════════════

def _mask(text: str, n: int = 4) -> str:
    return "***" if not text or len(text) <= n else "*" * (len(text) - n) + text[-n:]


async def ensure_browser() -> None:
    """确保全局 Playwright browser 已启动。Server 生命周期内只执行一次 launch。"""
    if state.browser and state.browser.is_connected():
        return
    logger.info("[Browser] 启动 Chromium...")
    if not state.playwright:
        state.playwright = await async_playwright().start()
    state.browser = await state.playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    )
    logger.info("[Browser] launched")


async def _apply_stealth_to_context(context: BrowserContext) -> None:
    try:
        await context.add_init_script(STEALTH_INIT_SCRIPT)
    except Exception as e:
        logger.debug(f"[Stealth] 注入失败（非致命）: {e}")


async def _block_resources_handler(route) -> None:
    """资源拦截：只放行小米域名，屏蔽图片/字体/媒体文件。"""
    rt  = route.request.resource_type
    url = route.request.url
    mi_hosts = ["account.xiaomi.com", "i.mi.com", "xiaomi.com", "mi.com"]
    if any(h in url for h in mi_hosts):
        await route.continue_()
        return
    if rt in ("image", "font", "media"):
        await route.abort()
        return
    ext_blacklist = (
        ".png", ".jpg", ".jpeg", ".gif", ".svg",
        ".webp", ".ico", ".woff", ".woff2", ".ttf",
        ".otf", ".mp4", ".webm", ".mp3", ".wav",
    )
    if any(url.split("?")[0].endswith(ext) for ext in ext_blacklist):
        await route.abort()
        return
    await route.continue_()


async def ensure_context(sess: SessionState) -> BrowserContext:
    """
    确保 session 的 BrowserContext 存在且有效。

    强约束：
      - 全局唯一 context（账号级单例）
      - 仅在首次启动或浏览器崩溃后才允许创建新 context
      - 创建新 context 时必须清理所有关联的 page
      - 正常运行中绝不重建 context
    """
    # 检查 browser 是否存活，崩溃则重建
    if not state.browser or not state.browser.is_connected():
        logger.warning("[Context] browser 断开，重新启动并重建 context...")
        await ensure_browser()
        # browser 崩溃 → 所有 page/context 失效，必须清理
        sess.context = None
        sess.page = None
        sess.heartbeat_page = None
        sess.refresh_page = None

    # 验证已有 context 是否仍可用
    if sess.context is not None:
        try:
            await sess.context.cookies("https://i.mi.com")
            return sess.context
        except Exception as e:
            logger.warning(f"[Context] 已有 context 不可用（{e}），必须重建...")
            # context 损坏 → 清理所有关联 page
            sess.context = None
            sess.page = None
            sess.heartbeat_page = None
            sess.refresh_page = None

    # ★ 创建新 context（仅在首次或崩溃后执行，正常运行中永远不会到这里）
    storage_state = str(sess.storage_path) if sess.storage_path.exists() else None
    context = await state.browser.new_context(
        user_agent=USER_AGENT,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        viewport={"width": 1280, "height": 800},
        storage_state=storage_state,
    )
    await _apply_stealth_to_context(context)
    await context.route("**/*", _block_resources_handler)
    sess.context = context
    logger.info(
        f"[Context] created（账号级单例），session={sess.session_key[:8]}, "
        f"storage_loaded={bool(storage_state)}"
    )
    return context


async def ensure_page(sess: SessionState) -> Page:
    """
    确保 session 的 Page 存在且未关闭。
    长期复用同一个 page，禁止在 sync 流程中创建新 page。
    """
    context = await ensure_context(sess)

    if sess.page is not None and not sess.page.is_closed():
        return sess.page

    page = await context.new_page()
    sess.page = page
    logger.info(f"[Page] created，session={sess.session_key[:8]}")
    return page


# ══════════════════════════════════════════════════════════════════
# Playwright 登录
# ══════════════════════════════════════════════════════════════════

async def _save_diagnostic(page: Page, sess: SessionState, reason: str) -> None:
    try:
        diag_path = Path(f"diag_{sess.session_key[:8]}_{reason}.png")
        await page.screenshot(path=str(diag_path), full_page=True)
        logger.info(f"[Diag] 截图已保存: {diag_path.resolve()}")
    except Exception as e:
        logger.debug(f"[Diag] 截图失败: {e}")


def _is_on_find_page(url: str) -> bool:
    return "i.mi.com" in url and XIAOMI_ACCOUNT_DOMAIN not in url


async def _has_login_cookies(context) -> bool:
    """验证 i.mi.com 是否存在有效登录 cookie。"""
    try:
        cookies = await context.cookies("https://i.mi.com")
        if not cookies:
            return False
        logger.debug(f"[CookieCheck] 存在 {len(cookies)} 个 cookie, names={[c['name'] for c in cookies][:20]}")
        return True
    except Exception as e:
        logger.warning(f"[CookieCheck] 获取 cookie 异常: {e}")
        return False


async def _probe_auth_ok(context, sess: SessionState) -> tuple[bool, str, Optional[str]]:
    """
    用最轻量的 API（device/status/list）探测当前 context cookie 是否真实可用。

    返回：(ok, reason, sts_url)
      - (True,  "OK",              None)
      - (False, "NEED_STS_LOGIN",  sts_url_or_None)
      - (False, "API_FAIL",        None)
      - (False, "NO_COOKIES",      None)
    """
    # ── probe 缓存：成功结果 5s 内不重复调用（need_reauth 时强制穿透）──
    now = time.time()
    if sess.last_probe_ts > 0 and now - sess.last_probe_ts < 5:
        if sess.need_reauth:
            logger.info("[ProbeAuth] bypass cache: need_reauth=True")
        else:
            logger.debug("[ProbeAuth] skip: cached result (<5s)")
            return True, "OK", None

    try:
        raw_cookies = await context.cookies("https://i.mi.com")
        if not raw_cookies:
            logger.warning("[ProbeAuth] cookie 列表为空，直接返回 NO_COOKIES")
            _set_auth_state(sess, "NEED_LOGIN")
            return False, "NO_COOKIES", None

        names = [c["name"] for c in raw_cookies]
        logger.info(f"[ProbeAuth] 开始 probe=list，cookie names={names[:20]}")

        cookies_dict = {c["name"]: c["value"] for c in raw_cookies}
        result = await call_xiaomi_api(
            PATH_DEVICE_LIST, params=None, cookies=cookies_dict, sess=sess, timeout=8.0
        )

        if result is None:
            logger.warning("[ProbeAuth] call_xiaomi_api 返回 None，判定 API_FAIL")
            return False, "API_FAIL", None

        code = result.get("code", -1)
        info = result.get("info", "")

        if code == 0:
            sess.last_probe_ts = time.time()
            _set_auth_state(sess, "OK")
            logger.info("[ProbeAuth] probe=list OK，session 真实可用")
            return True, "OK", None

        if code == 990 or "AUTH" in info or "SESSION" in info or "STS" in info:
            sts_url = result.get("sts_url")
            _set_auth_state(sess, "NEED_REFRESH")
            logger.warning(
                f"[ProbeAuth] probe NEED_STS_LOGIN，code={code} info={info}，"
                f"有sts_url={bool(sts_url)}"
            )
            return False, "NEED_STS_LOGIN", sts_url

        logger.warning(f"[ProbeAuth] probe API_FAIL，code={code} info={info}")
        return False, "API_FAIL", None

    except Exception as e:
        logger.warning(f"[ProbeAuth] 异常: {e}")
        return False, "API_FAIL", None


async def _sts_renew_via_playwright(
    context: BrowserContext,
    sts_url: str,
    sess: SessionState,
) -> bool:
    """
    通过 Playwright 跟随小米 STS 跳转链路完成 session 续期。
    复用 refresh_page（减少 new_page 创建），失败时回退到临时 page。
    """
    global _last_sts_call_ts

    _sts_display = (sts_url[:80] + "...") if len(sts_url) > 80 else sts_url

    # ── STS 最小调用间隔保护 ──
    now = time.time()
    since_last_sts = now - _last_sts_call_ts
    if _last_sts_call_ts > 0 and since_last_sts < STS_MIN_INTERVAL_SECS:
        logger.info(
            f"[STS] skip: 距上次 STS 调用 {since_last_sts:.0f}s < {STS_MIN_INTERVAL_SECS}s，"
            f"跳过本次续期"
        )
        return False

    _last_sts_call_ts = now
    logger.info(f"[STS] start: 开始续期，sts_url(前80)={_sts_display}")

    # 优先复用 refresh_page
    page = await _ensure_refresh_page(sess)
    used_refresh_page = page is not None
    temp_page = None

    if page is None:
        # refresh_page 不可用，回退创建临时 page
        try:
            temp_page = await context.new_page()
            page = temp_page
            logger.debug("[STS] refresh_page 不可用，使用临时 page")
        except Exception as e:
            logger.warning(f"[STS] 创建临时 page 失败: {e}")
            return False

    try:
        await page.goto(sts_url, wait_until="domcontentloaded", timeout=20_000)
        # 快速检测：STS 跳到了密码输入页（Xiaomi 要求重新验证密码），无法自动续期
        if "/login/password" in page.url or "/pass/serviceLogin" in page.url:
            logger.warning(f"[STS] 检测到密码重验页，跳过等待直接回退全量登录，url={page.url[:80]}")
            return False
        try:
            await page.wait_for_url(_is_on_find_page, timeout=15_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=5_000)
            except Exception:
                await asyncio.sleep(2.0)
            logger.info(f"[STS] success: 续期完成，当前 url={page.url[:60]}")
            return True
        except Exception:
            logger.warning(f"[STS] 跳转超时，当前 url={page.url[:60]}")
            return False
    except Exception as e:
        logger.exception(f"[STS] 续期异常: {e}")
        # 如果用的是 refresh_page 且出异常，重置它
        if used_refresh_page:
            try:
                await page.close()
            except Exception:
                pass
            sess.refresh_page = None
        return False
    finally:
        # 只关闭临时 page，refresh_page 保持长期复用
        if temp_page is not None:
            try:
                await temp_page.close()
            except Exception:
                pass


async def _dismiss_agreement_popup(page: Page) -> None:
    """自动点击小米账号协议弹窗中的"同意并继续"按钮（若存在）。"""
    try:
        btn = await page.wait_for_selector(
            'button:has-text("同意并继续")',
            state="visible",
            timeout=2_000,
        )
        if btn:
            await btn.click()
            logger.info("[Login] 已自动点击协议弹窗[同意并继续]")
            await asyncio.sleep(0.5)
    except Exception:
        pass


async def _find_element(page: Page, selectors: List[str], timeout_each: int = 1_000):
    """按选择器列表顺序查找可见元素，失败继续下一个，也遍历 frames。"""
    for sel in selectors:
        try:
            el = await page.wait_for_selector(sel, state="visible", timeout=timeout_each)
            if el:
                return el
        except Exception:
            pass
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        for sel in selectors:
            try:
                el = await frame.wait_for_selector(sel, state="visible", timeout=timeout_each // 2)
                if el:
                    return el
            except Exception:
                pass
    return None


async def _login_xiaomi(
    page: Page,
    username: str,
    password: str,
    sess: SessionState,
    context=None,
) -> bool:
    """
    通过 Playwright 完成小米账号登录。

    流程（来自 HAR Referer 分析）：
      1. 导航到 https://i.mi.com/find
      2. 自动跳转到 account.xiaomi.com 登录页
      3. 填写账号/密码，点击登录
      4. 等待跳转回 i.mi.com
      5. 若出现 2FA/滑块，给额外等待时间
    """
    t0 = time.time()
    elapsed_ms = lambda: int((time.time() - t0) * 1000)

    try:
        logger.debug(f"[Login] 开始，session={sess.session_key[:8]}, user={_mask(username)}")
        await page.wait_for_load_state("domcontentloaded", timeout=15_000)

        if _is_on_find_page(page.url):
            if await _has_login_cookies(context):
                probe_ok, probe_reason, probe_sts_url = await _probe_auth_ok(context, sess)
                if probe_ok:
                    logger.info("[Login] 已在 find 页面且 probe=list OK，跳过登录")
                    return True
                else:
                    logger.warning(
                        f"[Login] cookie 存在但 probe 失败（{probe_reason}），"
                        f"继续执行登录流程"
                    )
                    if probe_reason == "NEED_STS_LOGIN":
                        logger.warning("[Login] 检测到 NEED_STS_LOGIN，尝试 STS 续期")
                        if probe_sts_url:
                            sts_ok = await _sts_renew_via_playwright(context, probe_sts_url, sess)
                            if sts_ok:
                                probe_ok2, probe_reason2, _ = await _probe_auth_ok(context, sess)
                                if probe_ok2:
                                    logger.info("[Login] STS 续期成功，session 可用，跳过重新登录")
                                    return True
                                logger.warning(
                                    f"[Login] STS 续期后 probe 仍失败（{probe_reason2}），进入完整登录"
                                )
                        # ★ 禁止在此 clear_cookies()，cookie 已由 full_login 预先清除
                        logger.info("[Login] STS 无效，继续执行用户名密码登录流程")
            else:
                logger.warning("[Login] URL 在 find 页面但 cookie=0，判定未登录，继续执行登录流程")

        if XIAOMI_ACCOUNT_DOMAIN not in page.url:
            try:
                await page.wait_for_url(
                    lambda url: XIAOMI_ACCOUNT_DOMAIN in url,
                    timeout=12_000,
                )
            except Exception:
                logger.warning(f"[Login] 未自动跳转到登录页，当前 url={page.url}，继续尝试")

        await _dismiss_agreement_popup(page)

        user_el = await _find_element(page, [
            'input[name="user"]',
            'input[type="email"]',
            'input[type="tel"]',
            '#ius-userid',
            'input[placeholder*="手机"]',
            'input[placeholder*="邮箱"]',
            'input[placeholder*="账号"]',
            'input[type="text"]:visible',
        ], timeout_each=1_500)

        if user_el is None:
            logger.error(f"[Login] 无法找到用户名输入框（{elapsed_ms()}ms），url={page.url}")
            await _save_diagnostic(page, sess, "no_username")
            return False

        await user_el.fill(username)

        next_el = await _find_element(page, [
            'button[data-testid="sign-in-btn"]',
            'button:has-text("下一步")',
            'button:has-text("Next")',
        ], timeout_each=800)
        if next_el:
            try:
                await next_el.click()
                await asyncio.sleep(1.0)
            except Exception:
                pass

        pwd_el = await _find_element(page, [
            'input[type="password"]',
            '#ius-password',
            'input[name="password"]',
            'input[placeholder*="密码"]',
        ], timeout_each=2_000)

        if pwd_el is None:
            logger.error(f"[Login] 无法找到密码输入框（{elapsed_ms()}ms），url={page.url}")
            await _save_diagnostic(page, sess, "no_password")
            return False

        await pwd_el.fill(password)

        submit_el = await _find_element(page, [
            'button[type="submit"]',
            '#ius-sign-in-submit-btn',
            'button:has-text("登录")',
            'button:has-text("Sign in")',
            'input[type="submit"]',
        ], timeout_each=1_500)

        if submit_el:
            await submit_el.click()
        else:
            await pwd_el.press("Enter")

        await _dismiss_agreement_popup(page)

        try:
            await page.wait_for_url(_is_on_find_page, timeout=20_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=6_000)
            except Exception:
                await asyncio.sleep(3.0)
            logger.info(f"[Login] 登录成功（{elapsed_ms()}ms），url={page.url}")
            return True
        except Exception:
            pass

        if XIAOMI_ACCOUNT_DOMAIN in page.url:
            logger.warning(
                f"[Login] 可能需要 2FA（{elapsed_ms()}ms），url={page.url}，等待 30s"
            )
            await _save_diagnostic(page, sess, "need_2fa")
            try:
                await page.wait_for_url(_is_on_find_page, timeout=30_000)
                logger.info(f"[Login] 2FA 完成，登录成功（{elapsed_ms()}ms）")
                return True
            except Exception:
                logger.error(f"[Login] 2FA 超时（{elapsed_ms()}ms）")
                await _save_diagnostic(page, sess, "2fa_timeout")
                return False

        logger.error(f"[Login] 登录超时（{elapsed_ms()}ms），url={page.url}")
        await _save_diagnostic(page, sess, "login_timeout")
        return False

    except Exception as e:
        logger.exception(f"[Login] 异常 session={sess.session_key[:8]}: {e}")
        try:
            await _save_diagnostic(page, sess, "exception")
        except Exception:
            pass
        return False


async def _ensure_refresh_page(sess: SessionState) -> Optional[Page]:
    """获取或创建 refresh_session 专用 page，长期复用，不污染主 page。"""
    if sess.refresh_page is not None and not sess.refresh_page.is_closed():
        return sess.refresh_page

    if sess.context is None:
        return None

    try:
        sess.refresh_page = await sess.context.new_page()
        logger.info(f"[RefreshSession] 创建新 refresh_page，session={sess.session_key[:8]}")
        return sess.refresh_page
    except Exception as e:
        logger.warning(f"[RefreshSession] 创建 refresh_page 失败: {e}")
        return None


async def refresh_session(sess: SessionState) -> bool:
    """
    软续命：使用已有 context/page/cookies 尝试恢复 session。

    禁止：clear_cookies()、走用户名密码登录页面。
    允许：probe_auth、STS 续期、browser 页面刷新（不清 cookie）、再次 probe。

    返回 True 表示 session 已恢复可用。
    """
    if sess.auth_state == "AUTH_UNAVAILABLE":
        logger.debug(
            f"[AuthState] skip: AUTH_UNAVAILABLE，禁止 refresh_session，"
            f"session={sess.session_key[:8]}"
        )
        return False

    t0 = time.time()
    logger.info(f"[RefreshSession] 开始，session={sess.session_key[:8]}")

    try:
        await ensure_browser()
        context = await ensure_context(sess)

        # ── Step 1: probe 现有 cookie 是否可用 ──
        probe_ok, probe_reason, probe_sts_url = await _probe_auth_ok(context, sess)
        logger.info(f"[RefreshSession] 首次 probe: {probe_reason}")

        if probe_ok:
            # ★ 关键：browser context 中的 cookie 可能比 storage 文件更新，
            #   必须写回 storage，否则 /sync 读到的还是旧 cookie → 永远 401
            await _save_storage_state(sess, context)
            sess.last_probe_ok_ts = time.time()
            sess.last_refresh_ts  = time.time()
            sess.need_reauth      = False
            sess.last_err_reason  = None
            _set_auth_state(sess, "OK")
            logger.info(
                f"[RefreshSession] 成功（probe 直接 OK，已同步 storage），"
                f"session={sess.session_key[:8]}, cost={int((time.time()-t0)*1000)}ms"
            )
            return True

        # ── Step 2: STS 续期（如果有 sts_url）──
        if probe_reason == "NEED_STS_LOGIN" and probe_sts_url:
            logger.info("[RefreshSession] 尝试 STS 续期...")
            sts_ok = await _sts_renew_via_playwright(context, probe_sts_url, sess)
            if sts_ok:
                probe_ok, probe_reason, _ = await _probe_auth_ok(context, sess)
                logger.info(f"[RefreshSession] STS 续期后 probe: {probe_reason}")
                if probe_ok:
                    await _save_storage_state(sess, context)
                    sess.last_probe_ok_ts = time.time()
                    sess.last_refresh_ts  = time.time()
                    sess.need_reauth      = False
                    sess.last_err_reason  = None
                    _set_auth_state(sess, "OK")
                    logger.info(
                        f"[RefreshSession] 成功（STS 续期），"
                        f"session={sess.session_key[:8]}, cost={int((time.time()-t0)*1000)}ms"
                    )
                    return True
            else:
                logger.warning("[RefreshSession] STS 续期失败")

        # ── Step 3: 浏览器软刷新（不清 cookie，让 JS 自动续期）──
        # ★ 使用 refresh_page 而非主 page，避免污染主 page 状态
        # ── 浏览器软刷新最小间隔保护 ──
        global _last_refresh_page_call_ts
        now_rp = time.time()
        since_last_rp = now_rp - _last_refresh_page_call_ts
        _skip_browser_refresh = (
            _last_refresh_page_call_ts > 0
            and since_last_rp < REFRESH_PAGE_MIN_INTERVAL_SECS
        )
        if _skip_browser_refresh:
            logger.info(
                f"[RefreshSession] skip: 浏览器刷新距上次 {since_last_rp:.0f}s < "
                f"{REFRESH_PAGE_MIN_INTERVAL_SECS}s，跳过本次浏览器刷新"
            )
            rpage = None
        else:
            _last_refresh_page_call_ts = now_rp
            logger.info("[RefreshSession] start: 尝试浏览器软刷新（不清 cookie）...")
            rpage = await _ensure_refresh_page(sess)
        if rpage is None and not _skip_browser_refresh:
            logger.warning("[RefreshSession] 无法获取 refresh_page，跳过浏览器刷新")
        elif rpage is not None:
            try:
                await rpage.goto(XIAOMI_FIND_URL, wait_until="domcontentloaded", timeout=20_000)
                if _is_on_find_page(rpage.url):
                    try:
                        await rpage.wait_for_load_state("networkidle", timeout=6_000)
                    except Exception:
                        await asyncio.sleep(2.0)

                    # 再次 probe
                    probe_ok, probe_reason, _ = await _probe_auth_ok(context, sess)
                    logger.info(f"[RefreshSession] 浏览器刷新后 probe: {probe_reason}")
                    if probe_ok:
                        await _save_storage_state(sess, context)
                        sess.last_probe_ok_ts = time.time()
                        sess.last_refresh_ts  = time.time()
                        sess.need_reauth      = False
                        sess.last_err_reason  = None
                        _set_auth_state(sess, "OK")
                        logger.info(
                            f"[RefreshSession] success:（浏览器刷新），"
                            f"session={sess.session_key[:8]}, cost={int((time.time()-t0)*1000)}ms"
                        )
                        return True
                else:
                    logger.warning(f"[RefreshSession] 浏览器刷新后不在 find 页面，url={rpage.url[:80]}")
            except Exception as e:
                logger.warning(f"[RefreshSession] 浏览器刷新异常: {e}，重置 refresh_page")
                try:
                    await rpage.close()
                except Exception:
                    pass
                sess.refresh_page = None

        # ── 所有软续命手段均失败 ──
        sess.last_refresh_ts = time.time()
        _set_auth_state(sess, "NEED_LOGIN")
        logger.warning(
            f"[RefreshSession] 失败，所有软续命手段均无效，"
            f"session={sess.session_key[:8]}, cost={int((time.time()-t0)*1000)}ms"
        )
        return False

    except Exception as e:
        logger.exception(f"[RefreshSession] 异常 session={sess.session_key[:8]}: {e}")
        return False


async def full_login(sess: SessionState) -> None:
    """
    最后手段：清除 cookie 并重走完整登录页面（用户名/密码）。

    强约束：
      - 只有此函数允许 clear_cookies()
      - 只有此函数允许走登录页面（用户名/密码）
      - 登录成功后必须保存 storage_state
      - 单飞由 _recovery_pipeline 的 recovery_lock 保证

    调用者：仅 _recovery_pipeline
    """
    # ★ 单飞由 _recovery_pipeline 的 recovery_lock 保证，此处不再加锁
    t0 = time.time()

    username = sess.credentials.get("username", "")
    password = sess.credentials.get("password", "")

    if not username or not password:
        logger.error(f"[FullLogin] fail: 无凭据，session={sess.session_key[:8]}")
        return

    # ── 冷却检查（快速 skip，不在锁内 sleep）──
    now = time.time()
    if sess.last_login_failed > 0 and now - sess.last_login_failed < _FULL_LOGIN_FAIL_COOLDOWN:
        remaining = _FULL_LOGIN_FAIL_COOLDOWN - (now - sess.last_login_failed)
        logger.warning(
            f"[FullLogin] skip: fail cooldown，session={sess.session_key[:8]}, "
            f"距上次失败 {now - sess.last_login_failed:.0f}s < {_FULL_LOGIN_FAIL_COOLDOWN}s，"
            f"剩余 {remaining:.0f}s"
        )
        return

    if sess.last_login_attempt > 0 and now - sess.last_login_attempt < _FULL_LOGIN_RATE_LIMIT:
        remaining = _FULL_LOGIN_RATE_LIMIT - (now - sess.last_login_attempt)
        logger.warning(
            f"[FullLogin] skip: rate limit cooldown，session={sess.session_key[:8]}, "
            f"距上次尝试 {now - sess.last_login_attempt:.0f}s < {_FULL_LOGIN_RATE_LIMIT}s，"
            f"剩余 {remaining:.0f}s"
        )
        return

    logger.info(
        f"[FullLogin] start，session={sess.session_key[:8]}, user={_mask(username)}"
    )
    sess.last_login_attempt = time.time()

    try:
        await ensure_browser()

        # 获取（或首次创建）长期复用的 context 和 page
        context = await ensure_context(sess)
        page    = await ensure_page(sess)

        # ★ 只有 full_login 允许 clear_cookies
        await context.clear_cookies()
        logger.info(f"[FullLogin] 已清除 cookie，准备重新登录")

        goto_ok = False
        for attempt in range(2):
            try:
                await page.goto(
                    XIAOMI_FIND_URL,
                    wait_until="domcontentloaded",
                    timeout=20_000,
                )
                goto_ok = True
                break
            except Exception as e:
                logger.warning(f"[FullLogin] goto 失败（attempt {attempt + 1}/2）: {e}")

        if not goto_ok:
            logger.error("[FullLogin] fail: goto 失败，放弃登录")
            sess.last_login_failed = time.time()
            return

        success    = await _login_xiaomi(page, username, password, sess, context)
        elapsed_ms = int((time.time() - t0) * 1000)

        if success:
            # cookie 非空检查
            cookies = await context.cookies("https://i.mi.com")
            if not cookies:
                logger.warning(
                    f"[FullLogin] fail: cookie=0，拒绝保存 storage_state，"
                    f"session={sess.session_key[:8]}"
                )
                sess.last_login_failed = time.time()
                sess.need_reauth       = True
                sess.last_err_reason   = "LOGIN_FAILED"
                _set_auth_state(sess, "NEED_LOGIN")
            else:
                # API probe 验证 cookie 真实可用
                logger.info(
                    f"[FullLogin] cookie 非空（{len(cookies)} 个），"
                    f"names={[c['name'] for c in cookies][:20]}，开始 probe..."
                )
                probe_ok, probe_reason, probe_sts_url = await _probe_auth_ok(context, sess)
                logger.info(f"[FullLogin] probe 结果: {probe_reason}")

                # probe 失败且有 STS URL → 续期后再次 probe
                if not probe_ok and probe_reason == "NEED_STS_LOGIN" and probe_sts_url:
                    logger.warning("[FullLogin] NEED_STS_LOGIN，尝试 STS 续期...")
                    sts_ok = await _sts_renew_via_playwright(context, probe_sts_url, sess)
                    if sts_ok:
                        probe_ok, probe_reason, _ = await _probe_auth_ok(context, sess)
                        logger.info(f"[FullLogin] STS续期后 probe: {probe_reason}")
                    else:
                        logger.warning("[FullLogin] STS 续期失败")

                if not probe_ok:
                    logger.warning(
                        f"[FullLogin] fail: probe 失败（{probe_reason}），禁止保存 storage_state"
                    )
                    sess.last_login_failed = time.time()
                    sess.need_reauth       = True
                    sess.last_err_reason   = probe_reason
                    _set_auth_state(sess, "NEED_LOGIN")
                else:
                    # probe OK，原子写入 storage_state
                    await _save_storage_state(sess, context)

                    file_size = sess.storage_path.stat().st_size
                    logger.info(
                        f"[FullLogin] success，session={sess.session_key[:8]}, "
                        f"cookies={len(cookies)}个, storage={file_size}B, "
                        f"elapsed={elapsed_ms}ms"
                    )
                    sess.need_reauth        = False
                    sess.last_err_reason    = None
                    sess.last_login_failed  = 0
                    sess.last_full_login_ts = time.time()
                    sess.last_probe_ok_ts   = time.time()
                    sess.refresh_fail_count = 0
                    _set_auth_state(sess, "OK")
        else:
            logger.error(
                f"[FullLogin] fail（{elapsed_ms}ms），session={sess.session_key[:8]}"
            )
            sess.last_login_failed = time.time()
            sess.need_reauth       = True
            sess.last_err_reason   = "LOGIN_FAILED"
            _set_auth_state(sess, "NEED_LOGIN")

    except Exception as e:
        logger.exception(f"[FullLogin] fail: 异常 session={sess.session_key[:8]}: {e}")
        sess.last_login_failed = time.time()
        sess.need_reauth       = True
        sess.last_err_reason   = str(e)
        _set_auth_state(sess, "NEED_LOGIN")


async def _save_storage_state(sess: SessionState, context) -> None:
    """原子写入 storage_state。供 refresh_session / full_login 共用。
    持有 sess.storage_lock 防止与 _persist_cookies_to_storage 竞态。"""
    async with sess.storage_lock:
        try:
            tmp = sess.storage_path.with_suffix(".tmp")
            await context.storage_state(path=str(tmp))
            tmp.replace(sess.storage_path)
            sess.last_cookie_persist = time.time()
            logger.info(f"[StorageState] 已保存，session={sess.session_key[:8]}")
        except Exception as e:
            logger.warning(f"[StorageState] 保存失败: {e}")


# ══════════════════════════════════════════════════════════════════
# 小米 API 客户端
# ══════════════════════════════════════════════════════════════════

# ── 坐标转换（GCJ-02 → WGS-84） ──

def _out_of_china(lng: float, lat: float) -> bool:
    return not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(x: float, y: float) -> float:
    ret  = -100.0 + 2.0*x + 3.0*y + 0.2*y*y + 0.1*x*y + 0.2*math.sqrt(abs(x))
    ret += (20.0*math.sin(6.0*x*math.pi) + 20.0*math.sin(2.0*x*math.pi)) * 2.0/3.0
    ret += (20.0*math.sin(y*math.pi)     + 40.0*math.sin(y/3.0*math.pi)) * 2.0/3.0
    ret += (160.0*math.sin(y/12.0*math.pi) + 320*math.sin(y*math.pi/30.0)) * 2.0/3.0
    return ret


def _transform_lng(x: float, y: float) -> float:
    ret  = 300.0 + x + 2.0*y + 0.1*x*x + 0.1*x*y + 0.1*math.sqrt(abs(x))
    ret += (20.0*math.sin(6.0*x*math.pi) + 20.0*math.sin(2.0*x*math.pi)) * 2.0/3.0
    ret += (20.0*math.sin(x*math.pi)     + 40.0*math.sin(x/3.0*math.pi)) * 2.0/3.0
    ret += (150.0*math.sin(x/12.0*math.pi) + 300.0*math.sin(x/30.0*math.pi)) * 2.0/3.0
    return ret


def _gcj02_to_wgs84(gcj_lng: float, gcj_lat: float) -> Tuple[float, float]:
    """GCJ-02（高德/autonavi）→ WGS-84。"""
    if _out_of_china(gcj_lng, gcj_lat):
        return gcj_lng, gcj_lat
    a, ee = 6378245.0, 0.00669342162296594323
    dlat  = _transform_lat(gcj_lng - 105.0, gcj_lat - 35.0)
    dlng  = _transform_lng(gcj_lng - 105.0, gcj_lat - 35.0)
    radlat    = gcj_lat / 180.0 * math.pi
    magic     = math.sin(radlat)
    magic     = 1 - ee * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (a / sqrtmagic * math.cos(radlat) * math.pi)
    return gcj_lng - dlng, gcj_lat - dlat


def _wgs84_to_gcj02(wgs_lng: float, wgs_lat: float) -> Tuple[float, float]:
    """WGS-84 → GCJ-02（高德/autonavi）。"""
    if _out_of_china(wgs_lng, wgs_lat):
        return wgs_lng, wgs_lat
    a, ee = 6378245.0, 0.00669342162296594323
    dlat  = _transform_lat(wgs_lng - 105.0, wgs_lat - 35.0)
    dlng  = _transform_lng(wgs_lng - 105.0, wgs_lat - 35.0)
    radlat    = wgs_lat / 180.0 * math.pi
    magic     = math.sin(radlat)
    magic     = 1 - ee * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (a / sqrtmagic * math.cos(radlat) * math.pi)
    return wgs_lng + dlng, wgs_lat + dlat


def _gcj02_to_bd09(gcj_lng: float, gcj_lat: float) -> Tuple[float, float]:
    """GCJ-02 → BD-09（百度）。"""
    z = math.sqrt(gcj_lng * gcj_lng + gcj_lat * gcj_lat) + 0.00002 * math.sin(gcj_lat * math.pi * 3000.0 / 180.0)
    theta = math.atan2(gcj_lat, gcj_lng) + 0.000003 * math.cos(gcj_lng * math.pi * 3000.0 / 180.0)
    bd_lng = z * math.cos(theta) + 0.0065
    bd_lat = z * math.sin(theta) + 0.006
    return bd_lng, bd_lat


def _bd09_to_gcj02(bd_lng: float, bd_lat: float) -> Tuple[float, float]:
    """BD-09（百度）→ GCJ-02。"""
    x = bd_lng - 0.0065
    y = bd_lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * math.pi * 3000.0 / 180.0)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * math.pi * 3000.0 / 180.0)
    gcj_lng = z * math.cos(theta)
    gcj_lat = z * math.sin(theta)
    return gcj_lng, gcj_lat


def normalize_coord_type(coord_type: str) -> str:
    """将坐标类型字符串统一为 'wgs84' / 'gcj02' / 'bd09' / 'unknown'。"""
    ct = (coord_type or "").strip().lower()
    if ct in ("autonavi", "google", "gcj02", "gcj-02"):
        return "gcj02"
    if ct in ("baidu", "bd09", "bd-09"):
        return "bd09"
    if ct in ("wgs84", "wgs-84", "gps"):
        return "wgs84"
    return "unknown"


def compute_all_coords(
    lat: float, lon: float, coord_type: str
) -> Dict[str, Optional[float]]:
    """给定原始坐标及坐标系类型，返回 wgs84 / gcj02 / bd09 三种坐标。"""
    ct = normalize_coord_type(coord_type)
    result: Dict[str, Optional[float]] = {
        "wgs84_lat": None, "wgs84_lon": None,
        "gcj02_lat": None, "gcj02_lon": None,
        "bd09_lat":  None, "bd09_lon":  None,
    }
    try:
        if ct == "wgs84":
            wgs_lon, wgs_lat = lon, lat
            gcj_lon, gcj_lat = _wgs84_to_gcj02(lon, lat)
        elif ct == "gcj02":
            wgs_lon, wgs_lat = _gcj02_to_wgs84(lon, lat)
            gcj_lon, gcj_lat = lon, lat
        elif ct == "bd09":
            gcj_lon, gcj_lat = _bd09_to_gcj02(lon, lat)
            wgs_lon, wgs_lat = _gcj02_to_wgs84(gcj_lon, gcj_lat)
        else:
            return result
        bd_lon, bd_lat = _gcj02_to_bd09(gcj_lon, gcj_lat)
        result.update({
            "wgs84_lat": wgs_lat, "wgs84_lon": wgs_lon,
            "gcj02_lat": gcj_lat, "gcj02_lon": gcj_lon,
            "bd09_lat":  bd_lat,  "bd09_lon":  bd_lon,
        })
    except Exception:
        pass
    return result


def _is_html(text: str) -> bool:
    t = text.strip().lower()
    return t.startswith("<!doctype") or t.startswith("<html")


def _parse_sts_url(text: str) -> Optional[str]:
    """从 API 401 响应 body 中提取小米 STS 跳转 URL。"""
    if not text:
        return None
    try:
        obj = json.loads(text)
        for key in ("D", "url", "redirectUrl", "redirect"):
            url = obj.get(key)
            if url and "account.xiaomi.com" in url:
                return url
    except Exception:
        pass
    m = re.search(
        r'https?://account\.xiaomi\.com/pass/serviceLogin[^\s"\'<>]{0,600}',
        text,
    )
    return m.group(0) if m else None


def _ms_to_iso8601(ms: int) -> str:
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def _parse_model_name(raw: str) -> str:
    """解析小米 JSON 编码的型号字段。"""
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
        return obj.get("modelName") or obj.get("deviceName") or raw
    except Exception:
        return raw


def _extract_device_name(component_model_info: Dict[str, Any]) -> str:
    """从 componentModelInfo 提取设备名称。"""
    comp0      = component_model_info.get("0", {})
    model_name = comp0.get("modelName", {})
    raw = model_name.get("zhCN") or model_name.get("defaultName") or ""
    return _parse_model_name(raw)


_COORD_PRIORITY = ("autonavi", "google", "baidu")


def _pick_best_gps_entry(gps_list: List[Dict[str, Any]]) -> Optional[GpsEntry]:
    """从 gpsInfoTransformed 中选取最优 GPS 条目。优先级：autonavi > google > baidu。"""
    if not gps_list:
        return None
    by_coord = {g.get("coordinateType", ""): g for g in gps_list}
    for preferred in _COORD_PRIORITY:
        raw = by_coord.get(preferred)
        if raw:
            entry = GpsEntry(raw)
            if entry.valid():
                return entry
    for raw in gps_list:
        entry = GpsEntry(raw)
        if entry.valid():
            return entry
    return None


def _pick_latest_location(
    location_list: List[Dict[str, Any]],
) -> Tuple[Optional[GpsEntry], Optional[int]]:
    """从 locationList 按 clientUpdateTime 最大值选最新位置条目。"""
    if not location_list:
        return None, None
    sorted_locs = sorted(
        location_list,
        key=lambda loc: loc.get("clientUpdateTime", 0),
        reverse=True,
    )
    for loc in sorted_locs:
        entry = _pick_best_gps_entry(loc.get("gpsInfoTransformed", []))
        if entry:
            return entry, loc.get("clientUpdateTime")
    return None, None


async def call_xiaomi_api(
    path: str,
    params: Optional[Dict[str, Any]],
    cookies: Dict[str, str],
    sess: SessionState,
    timeout: float = 8.0,
) -> Optional[Dict[str, Any]]:
    """
    向 i.mi.com 发送 GET 请求。

    错误处理：
      - HTTP 401/403  → 清空 userId，返回 {"code": 990}
      - 返回 HTML     → session 失效，返回 {"code": 990}
      - JSON 解析异常 → 返回 {"code": -1}
      - code != 0     → 记录 warning，原样返回
      - 超时          → 返回 {"code": -1}
    """
    ts = int(time.time() * 1000)
    full_params: Dict[str, Any] = {"ts": ts}
    if params:
        full_params.update(params)

    url     = f"{XIAOMI_API_BASE}{path}"
    headers = dict(XIAOMI_BASE_HEADERS)
    headers["Cookie"] = cookies_to_header(cookies)

    endpoint_label = path.split("/")[-1] or path

    try:
        async with state.http_session.get(
            url,
            params=full_params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            status = r.status
            text   = await r.text(encoding="utf-8", errors="replace")

            if status in (401, 403):
                sts_url = _parse_sts_url(text)
                _sts_display = (sts_url[:80] + "...") if (sts_url and len(sts_url) > 80) else sts_url
                logger.error(
                    f"[Auth] cookie expired，{endpoint_label} HTTP {status}，"
                    f"body前200={text[:200]}，sts_url(截断)={_sts_display}"
                )
                sess.user_id = ""
                ret: Dict[str, Any] = {"code": 990, "info": f"HTTP_{status}_AUTH_FAILED"}
                if sts_url:
                    ret["sts_url"] = sts_url
                return ret

            if status != 200:
                logger.error(f"[API] {endpoint_label} HTTP {status}, body={text[:120]}")
                return {"code": -1, "info": f"HTTP_{status}"}

            if _is_html(text):
                logger.error(
                    f"[API] {endpoint_label} 返回 HTML（session 失效），body={text[:120]}"
                )
                sess.user_id = ""
                return {"code": 990, "info": "SESSION_EXPIRED_HTML"}

            # ── 第 1 层续命：捕获 Set-Cookie ──
            auth_updated = _absorb_set_cookies(sess, r.headers)
            if auth_updated:
                _schedule_persist_cookies(sess)

            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                logger.error(f"[API] {endpoint_label} JSON 解析失败，body={text[:120]}")
                return {"code": -1, "info": "JSON_DECODE_ERROR"}

            api_code   = data.get("code", -1)
            api_result = data.get("result", "")
            if api_code != 0 or api_result != "ok":
                logger.warning(
                    f"[API] {endpoint_label} code={api_code}, "
                    f"result={api_result}, description={data.get('description', '')}"
                )

            return data

    except asyncio.TimeoutError:
        logger.error(f"[API] {endpoint_label} 请求超时（{timeout}s）")
        return {"code": -1, "info": "TIMEOUT"}
    except aiohttp.ClientError as e:
        logger.error(f"[API] {endpoint_label} 网络异常: {e}")
        return {"code": -1, "info": f"CLIENT_ERROR: {e}"}
    except Exception as e:
        logger.exception(f"[API] {endpoint_label} 未知异常: {e}")
        return {"code": -1, "info": f"EXCEPTION: {e}"}


def _build_device_output(
    fid: str,
    device_type: str,
    model_name: str,
    status_data: Optional[Dict[str, Any]],
    stale: bool = False,
    stale_reason: str = "",
    is_self_device: bool = False,
) -> DeviceOutput:
    """
    将 status API 返回的原始数据整合为 DeviceOutput。字段路径严格来自 HAR。

    locate_time 使用 gpsInfoTransformed[x].clientUpdateTime（GPS 级时间戳），
    而非 locationList[x].clientUpdateTime，与 HAR 规范一致。
    """
    _name = model_name or fid
    out = DeviceOutput(
        device_id=fid,
        name=_name,
        model=_name,
        fid=fid,
        device_type=device_type,
        stale=stale,
        stale_reason=stale_reason,
        is_self_device=is_self_device,
    )

    if status_data is None:
        return out

    # ── status API data 顶层字段（来自 HAR）──
    out.share_location = bool(status_data.get("shareLocation", False))
    out.is_locating    = bool(status_data.get("isLocating", False))
    out.status         = str(status_data.get("status", "") or "")
    # isSelfDevice 优先读 status_data，回退到调用方传入值
    if "isSelfDevice" in status_data:
        out.is_self_device = bool(status_data["isSelfDevice"])

    component_list: List[Dict[str, Any]] = status_data.get("componentList", [])
    if not component_list:
        return out

    comp       = component_list[0]
    out.online = comp.get("online", False)

    # ── 电量（来自 HAR：batteryInfo.level / batteryInfo.clientUpdateTime）──
    battery_raw = comp.get("batteryInfo", {})
    if battery_raw:
        bi = BatteryInfo(battery_raw)
        out.battery = bi.level
        if bi.client_update_time:
            out.battery_time = int(bi.client_update_time / 1000)

    # ── 位置（来自 HAR：locationList[*].gpsInfoTransformed[*]）──
    gps_entry, _loc_update_ms = _pick_latest_location(comp.get("locationList", []))

    if gps_entry and gps_entry.valid():
        lat_raw    = gps_entry.latitude
        lng_raw    = gps_entry.longitude
        coord_type = gps_entry.coordinate_type

        norm = normalize_coord_type(coord_type)
        if norm == "unknown":
            logger.warning(
                "[GeoTransform] 未知坐标系 raw_coord_type=%r fid=%s "
                "raw_lat=%.6f raw_lon=%.6f，默认按 gcj02 处理",
                coord_type, fid, lat_raw, lng_raw,
            )
        coords = compute_all_coords(lat_raw, lng_raw, coord_type)
        logger.debug(
            "[GeoTransform] fid=%s raw_coord_type=%r(→%s) "
            "raw=(%.6f,%.6f) wgs84=(%.6f,%.6f) gcj02=(%.6f,%.6f) bd09=(%.6f,%.6f)",
            fid, coord_type, norm,
            lat_raw, lng_raw,
            coords["wgs84_lat"] or 0, coords["wgs84_lon"] or 0,
            coords["gcj02_lat"] or 0, coords["gcj02_lon"] or 0,
            coords["bd09_lat"]  or 0, coords["bd09_lon"]  or 0,
        )

        out.latitude          = coords["wgs84_lat"]
        out.longitude         = coords["wgs84_lon"]
        out.gcj02_lat         = coords["gcj02_lat"]
        out.gcj02_lon         = coords["gcj02_lon"]
        out.bd09_lat          = coords["bd09_lat"]
        out.bd09_lon          = coords["bd09_lon"]
        out.raw_lat           = lat_raw
        out.raw_lon           = lng_raw
        out.raw_coord_type    = coord_type
        out.coord_type        = "wgs84"
        out.accuracy          = gps_entry.accuracy
        out.address           = gps_entry.address
        out.address_component = gps_entry.address_component
        out.area              = gps_entry.area
        out.source_type       = gps_entry.source_type
        out.in_china_mainland = gps_entry.in_china_main_land

        # locate_time 使用 gpsInfoTransformed[x].clientUpdateTime（HAR 规范字段）
        gps_ts = gps_entry.client_update_time
        if gps_ts:
            out.locate_time = int(gps_ts / 1000)
            out.ts          = out.locate_time
            out.fix_time    = _ms_to_iso8601(gps_ts)

        out.raw = {
            "latitude":         gps_entry.latitude,
            "longitude":        gps_entry.longitude,
            "accuracy":         gps_entry.accuracy,
            "clientUpdateTime": gps_entry.client_update_time,
            "coordinateType":   gps_entry.coordinate_type,
            "address":          gps_entry.address,
            "addressComponent": gps_entry.address_component,
            "sourceType":       gps_entry.source_type,
            "area":             gps_entry.area,
            "inChinaMainLand":  gps_entry.in_china_main_land,
        }

    return out


def _get_latest_gps_update_time(status_data: Dict[str, Any]) -> Optional[int]:
    """
    从 device/status data 中提取 gpsInfoTransformed[x].clientUpdateTime（毫秒）。

    使用 GPS 级时间戳（而非 locationList 级），与 HAR 规范一致，
    用于判断刷新定位是否已产生新位置。
    """
    if not status_data:
        return None
    comp_list = status_data.get("componentList", [])
    if not comp_list:
        return None
    gps_entry, _ = _pick_latest_location(comp_list[0].get("locationList", []))
    if gps_entry:
        return gps_entry.client_update_time
    return None


async def trigger_locate(
    fid: str,
    target_user_id: str,
    cookies: Dict[str, str],
    sess: SessionState,
) -> bool:
    """触发 syncMode=2 定位刷新（来自 HAR 实测接口）。"""
    result = await call_xiaomi_api(
        PATH_DEVICE_STATUS,
        params={"targetUserId": target_user_id, "targetFid": fid, "syncMode": 2},
        cookies=cookies,
        sess=sess,
        timeout=10.0,
    )
    if result and result.get("code") == 0:
        logger.info(f"[ForceLocate] syncMode=2 触发成功，fid={fid[:20]}...")
        return True
    logger.warning(
        f"[ForceLocate] syncMode=2 触发失败，fid={fid[:20]}... "
        f"code={result.get('code') if result else None}"
    )
    return False


async def poll_until_fresh(
    fid: str,
    target_user_id: str,
    old_gps_time: Optional[int],
    cookies: Dict[str, str],
    sess: SessionState,
    device_type: str,
    model_name: str,
    is_self_device: bool = False,
) -> Tuple[Optional["DeviceOutput"], bool]:
    """
    轮询 GET status(syncMode=0)，每 LOCATE_POLL_INTERVAL_SECS+jitter 秒一次，直到：
      gpsInfoTransformed[x].clientUpdateTime > old_gps_time  → 返回新位置
      超过 LOCATE_POLL_MAX_WAIT_SECS 秒                       → 返回 (None, False)

    比较基准：gpsInfoTransformed[x].clientUpdateTime（GPS 级时间戳，来自 HAR）。
    受全局 poll_semaphore 并发限制保护。
    """
    global poll_semaphore

    # ── poll 去重：同一设备短时间内不重复 poll ──
    now = time.time()
    last = sess.last_poll_ts.get(fid, 0)
    if now - last < LOCATE_POLL_INTERVAL_SECS:
        logger.info(
            f"[Poll] skip: too frequent, fid={fid[:20]}..., "
            f"距上次 {now - last:.1f}s < {LOCATE_POLL_INTERVAL_SECS}s"
        )
        cached = sess.device_cache.get(fid)
        if cached:
            return DeviceOutput(**{**cached, "stale": True, "stale_reason": "POLL_TOO_FREQUENT"}), False
        return None, False
    sess.last_poll_ts[fid] = now

    logger.info(
        f"[PollFresh] start: fid={fid[:20]}... old_gps_ts={old_gps_time}, "
        f"interval={LOCATE_POLL_INTERVAL_SECS}s, max_wait={LOCATE_POLL_MAX_WAIT_SECS}s"
    )

    # ── 全局并发限制 ──
    sem = poll_semaphore
    if sem is not None:
        if sem.locked():
            logger.info(
                f"[PollFresh] 等待 semaphore（当前已占满 {LOCATE_POLL_MAX_CONCURRENCY} 槽），"
                f"fid={fid[:20]}..."
            )
        await sem.acquire()
        logger.debug(f"[PollFresh] 已获取 semaphore 槽位，fid={fid[:20]}...")

    try:
        deadline = time.time() + LOCATE_POLL_MAX_WAIT_SECS
        attempt  = 0
        while time.time() < deadline:
            # jitter: interval + random(0, 1.0)，避免多设备同时轮询
            sleep_time = LOCATE_POLL_INTERVAL_SECS + random.uniform(0, 1.0)
            await asyncio.sleep(sleep_time)
            attempt += 1
            result = await call_xiaomi_api(
                PATH_DEVICE_STATUS,
                params={"targetUserId": target_user_id, "targetFid": fid, "syncMode": 0},
                cookies=cookies,
                sess=sess,
                timeout=8.0,
            )
            if result is None or result.get("code") != 0:
                logger.debug(
                    f"[PollFresh] 轮询第{attempt}次，API 异常或非0，继续等待，fid={fid[:20]}..."
                )
                continue

            status_data   = result.get("data", {})
            new_gps_time  = _get_latest_gps_update_time(status_data)

            logger.debug(
                f"[PollFresh] 轮询第{attempt}次，fid={fid[:20]}... "
                f"old_gps_ts={old_gps_time} new_gps_ts={new_gps_time}"
            )

            if new_gps_time and (old_gps_time is None or new_gps_time > old_gps_time):
                logger.info(
                    f"[PollFresh] success: 位置已刷新，fid={fid[:20]}... "
                    f"old_gps_ts={old_gps_time} new_gps_ts={new_gps_time}（第{attempt}次轮询）"
                )
                out = _build_device_output(
                    fid, device_type, model_name, status_data,
                    is_self_device=is_self_device,
                )
                return out, True

        logger.warning(
            f"[PollFresh] timeout: 轮询超时（{LOCATE_POLL_MAX_WAIT_SECS}s/{attempt}次），fid={fid[:20]}..."
        )
        return None, False
    finally:
        if sem is not None:
            sem.release()


async def do_sync(
    sess: SessionState,
    cookies: Dict[str, str],
    force_locate: bool = False,
) -> Tuple[int, str, List[DeviceOutput]]:
    """
    完整同步流程（纯 API 调用，禁止创建 browser/context）：
      1. GET device/status/list
      2. 遍历 deviceList，逐个 GET device/status（syncMode=0）
      3. 位置陈旧时：触发 syncMode=2 + 轮询等待新位置
      4. 解析 gpsInfoTransformed，构建 DeviceOutput 列表（坐标转为 WGS-84）

    返回：(code, reason, devices)
      code=0   → 成功
      code=990 → 需要重新鉴权
      code=-1  → 网络/API 错误
    """
    if sess.auth_state == "AUTH_UNAVAILABLE":
        logger.debug(
            f"[AuthState] skip: AUTH_UNAVAILABLE，禁止 do_sync，"
            f"session={sess.session_key[:8]}"
        )
        return 990, "AUTH_UNAVAILABLE", []

    list_result = await call_xiaomi_api(
        PATH_DEVICE_LIST, params=None, cookies=cookies, sess=sess, timeout=8.0,
    )

    if list_result is None:
        return -1, "API_CALL_FAILED", []

    list_code = list_result.get("code", -1)
    if list_code == 990:
        return 990, "AUTH_FAILED_990", []
    if list_code != 0:
        return list_code, f"API_ERROR_{list_code}", []

    list_data = list_result.get("data", {})
    complete_user_ids: List[str]          = list_data.get("completeUserIds", [])
    device_list_raw: List[Dict[str, Any]] = list_data.get("deviceList", [])

    logger.info(
        f"[Sync] device/status/list 成功，"
        f"deviceCount={len(device_list_raw)}, "
        f"completeUserIds={len(complete_user_ids)}"
    )

    if not device_list_raw:
        return 0, "NO_DEVICES", []

    devices: List[DeviceOutput] = []

    for dev_raw in device_list_raw:
        fid: str = dev_raw.get("fid", "")
        if not fid:
            logger.warning("[Sync] deviceList 中存在无 fid 的条目，跳过")
            continue

        device_type: str = dev_raw.get("deviceType", "")
        model_name: str  = _extract_device_name(dev_raw.get("componentModelInfo", {}))

        own_user_id: str     = dev_raw.get("userId", "")
        is_self_device: bool = dev_raw.get("isSelfDevice", False)
        target_user_id: str  = own_user_id or (complete_user_ids[0] if complete_user_ids else "")

        if not target_user_id:
            logger.warning(
                f"[Sync] fid={fid[:20]}... 无法确定 targetUserId，跳过 status 查询，"
                f"deviceType={device_type}, model={model_name}"
            )
            devices.append(_build_device_output(
                fid, device_type, model_name, None,
                stale=True, stale_reason="NO_TARGET_USER_ID",
                is_self_device=is_self_device,
            ))
            continue

        status_result = await call_xiaomi_api(
            PATH_DEVICE_STATUS,
            params={"targetUserId": target_user_id, "targetFid": fid, "syncMode": 0},
            cookies=cookies,
            sess=sess,
            timeout=8.0,
        )

        if status_result is None or status_result.get("code") != 0:
            status_code = status_result.get("code", -1) if status_result else -1
            logger.warning(f"[Sync] fid={fid[:20]}... device/status code={status_code}，使用缓存")

            cached = sess.device_cache.get(fid)
            if cached:
                devices.append(DeviceOutput(**{**cached, "stale": True}))
            else:
                devices.append(_build_device_output(
                    fid, device_type, model_name, None,
                    stale=True, stale_reason=f"STATUS_CODE_{status_code}",
                    is_self_device=is_self_device,
                ))

            if status_code == 990:
                return 990, "AUTH_FAILED_990", devices
            continue

        status_data = status_result.get("data", {})

        # componentId：优先从 componentList[0].componentId 取，确实没有才为空
        component_id = ""
        _comp_list = status_data.get("componentList", [])
        if _comp_list:
            _cid = _comp_list[0].get("componentId", "")
            component_id = str(_cid) if _cid else ""
            if not component_id:
                logger.debug(
                    "[Sync] fid=%s... componentList[0].componentId 未找到，命令将使用默认值 '0'",
                    fid[:20],
                )

        # old_gps_time：使用 gpsInfoTransformed[x].clientUpdateTime（HAR 规范）
        old_gps_time = _get_latest_gps_update_time(status_data)
        now_ms       = int(time.time() * 1000)
        is_stale_loc = (
            old_gps_time is None
            or (now_ms - old_gps_time) > LOCATE_STALE_THRESHOLD_SECS * 1000
        )
        is_online = bool(
            status_data
            and status_data.get("componentList")
            and status_data["componentList"][0].get("online", False)
        )

        if is_stale_loc:
            stale_age_s  = (now_ms - (old_gps_time or 0)) / 1000 if old_gps_time else -1
            last_trigger = sess.locate_last_trigger.get(fid, 0.0)
            in_cooldown  = (time.time() - last_trigger) < LOCATE_COOLDOWN_SECS
            if not is_online:
                logger.info(
                    f"[ForceLocate] 设备离线，跳过 syncMode=2 fid={fid[:20]}..."
                )
                dev_out = _build_device_output(
                    fid, device_type, model_name, status_data,
                    stale=True, stale_reason="DEVICE_OFFLINE",
                    is_self_device=is_self_device,
                )
            elif in_cooldown:
                logger.info(
                    f"[ForceLocate] fid={fid[:20]}... 冷却中（剩余"
                    f"{LOCATE_COOLDOWN_SECS - (time.time() - last_trigger):.0f}s），跳过触发 syncMode=2"
                )
                dev_out = _build_device_output(
                    fid, device_type, model_name, status_data,
                    stale=True, stale_reason="LOCATE_COOLDOWN",
                    is_self_device=is_self_device,
                )
            else:
                logger.info(
                    f"[ForceLocate] 位置陈旧（{stale_age_s:.0f}s），"
                    f"触发 syncMode=2 fid={fid[:20]}... old_gps_ts={old_gps_time}"
                )
                trigger_ok = await trigger_locate(fid, target_user_id, cookies, sess)
                if trigger_ok:
                    sess.locate_last_trigger[fid] = time.time()
                    fresh_out, fresh_ok = await poll_until_fresh(
                        fid, target_user_id, old_gps_time,
                        cookies, sess, device_type, model_name,
                        is_self_device=is_self_device,
                    )
                    if fresh_ok and fresh_out:
                        dev_out = fresh_out
                    else:
                        dev_out = _build_device_output(
                            fid, device_type, model_name, status_data,
                            stale=True, stale_reason="LOCATE_TIMEOUT",
                            is_self_device=is_self_device,
                        )
                else:
                    dev_out = _build_device_output(
                        fid, device_type, model_name, status_data,
                        stale=True, stale_reason="LOCATE_TRIGGER_FAILED",
                        is_self_device=is_self_device,
                    )
        else:
            age_s = (now_ms - old_gps_time) / 1000 if old_gps_time else -1
            logger.debug(
                f"[ForceLocate] 位置新鲜（{age_s:.0f}s < {LOCATE_STALE_THRESHOLD_SECS}s），"
                f"无需触发 syncMode=2 fid={fid[:20]}..."
            )
            dev_out = _build_device_output(
                fid, device_type, model_name, status_data,
                is_self_device=is_self_device,
            )

        dev_out.user_id      = target_user_id
        dev_out.component_id = component_id

        devices.append(dev_out)
        sess.device_cache[fid] = dev_out.model_dump()

        logger.info(
            f"[Sync] fid={fid[:20]}... "
            f"lat={dev_out.latitude}, lng={dev_out.longitude}, "
            f"battery={dev_out.battery}, online={dev_out.online}, stale={dev_out.stale}"
        )

    return 0, "OK", devices


# ══════════════════════════════════════════════════════════════════
# FastAPI 应用
# ══════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    启动：初始化 http_session + 启动全局唯一 browser。
    关闭：清理资源（context/page 随 browser 一起销毁）。
    """
    global poll_semaphore
    logger.info(f"[Lifespan] 小米 API 服务启动，version={VERSION}")

    # ── 初始化全局 poll 并发 semaphore ──
    poll_semaphore = asyncio.Semaphore(LOCATE_POLL_MAX_CONCURRENCY)
    logger.info(f"[Lifespan] poll_semaphore 初始化，max_concurrency={LOCATE_POLL_MAX_CONCURRENCY}")

    state.http_session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=True, limit=20),
    )

    try:
        await ensure_browser()
    except Exception as e:
        logger.warning(f"[Lifespan] browser 启动失败（将在首次登录时重试）: {e}")

    yield

    logger.info("[Lifespan] 服务关闭，清理资源...")
    if state.http_session and not state.http_session.closed:
        await state.http_session.close()
    if state.browser:
        try:
            await state.browser.close()
        except Exception:
            pass
    if state.playwright:
        try:
            await state.playwright.stop()
        except Exception:
            pass


app = FastAPI(title="小米 Find 设备 API 代理", version=VERSION, lifespan=lifespan)


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "version": VERSION,
        "browser_connected": bool(state.browser and state.browser.is_connected()),
        "http_session_open": bool(state.http_session and not state.http_session.closed),
    }


@app.get("/status")
async def status() -> Dict[str, Any]:
    global poll_semaphore
    sessions_info: Dict[str, Any] = {}
    for sk, sess in state.sessions.items():
        sessions_info[sk[:8]] = {
            "auth_state":          sess.auth_state,
            "need_reauth":         sess.need_reauth,
            "last_err":            sess.last_err_reason,
            "has_user_id":         bool(sess.user_id),
            "has_storage":         sess.storage_path.exists(),
            "has_context":         sess.context is not None,
            "has_page":            sess.page is not None and not sess.page.is_closed(),
            "has_heartbeat_page":  sess.heartbeat_page is not None and not sess.heartbeat_page.is_closed(),
            "device_count":        len(sess.device_list),
            "login_in_progress":   sess.login_in_progress,
            "recovery_running":    sess.recovery_task is not None and not sess.recovery_task.done(),
            "refresh_fail_count":  sess.refresh_fail_count,
            "last_successful_sync":  sess.last_successful_sync,
            "last_browser_heartbeat": sess.last_browser_heartbeat,
            "last_full_login_ts":  sess.last_full_login_ts,
            "last_refresh_ts":     sess.last_refresh_ts,
            "last_probe_ok_ts":    sess.last_probe_ok_ts,
        }

    # ── 全局 poll 指标 ──
    sem = poll_semaphore
    poll_capacity = LOCATE_POLL_MAX_CONCURRENCY
    if sem is not None:
        # _value 是 asyncio.Semaphore 内部可用槽位数
        poll_inflight = poll_capacity - sem._value
    else:
        poll_inflight = 0

    return {
        "ok": True,
        "version": VERSION,
        "browser_connected": bool(state.browser and state.browser.is_connected()),
        "session_count": len(sessions_info),
        "sessions": sessions_info,
        "poll_semaphore_capacity": poll_capacity,
        "poll_inflight": poll_inflight,
    }


@app.post("/auth/ensure")
async def auth_ensure(req: LoginReq) -> Dict[str, Any]:
    """确保 session 已鉴权；提供凭据则更新；无效时触发后台登录。"""
    sess = state.get_session(req.session_key)

    if req.username and req.password:
        sess.credentials = {"username": req.username, "password": req.password}
        await _save_credentials(sess, req.username, req.password)
        # 收到凭据 → 解除 AUTH_UNAVAILABLE 熔断
        if sess.auth_state == "AUTH_UNAVAILABLE":
            _set_auth_state(sess, "NEED_LOGIN")

    has_storage = sess.storage_path.exists()
    cookies     = await get_cookies_from_context(sess) or await get_cookies_from_storage(sess.storage_path)
    if cookies:
        hydrate_user_id_from_cookies(sess, cookies)

    if not (cookies and sess.user_id):
        has_creds = bool(sess.credentials.get("username") and sess.credentials.get("password"))
        if has_creds:
            trigger_recovery_nowait(sess, reason="AUTH_ENSURE_NO_COOKIES")
            message = "未鉴权，已触发后台恢复流程"
        else:
            message = "未鉴权，且无凭据（请先调用 /login 注入账号密码）"
        logger.warning(f"[AuthEnsure] {message}，session={req.session_key[:8]}")
        return {
            "ok": False, "code": 990, "need_reauth": True,
            "message": message, "has_storage": has_storage,
            "session_key": req.session_key,
        }

    return {
        "ok": True, "code": 0, "need_reauth": False,
        "message": "已鉴权", "has_storage": has_storage,
        "session_key": req.session_key,
    }


@app.post("/login")
async def login(req: LoginReq) -> Dict[str, Any]:
    """存储凭据并立即触发登录（AUTH_FAILED_990 跳过冷却）。"""
    if not req.username or not req.password:
        raise HTTPException(status_code=400, detail="username 和 password 不能为空")

    sess = state.get_session(req.session_key)
    sess.credentials = {"username": req.username, "password": req.password}
    await _save_credentials(sess, req.username, req.password)
    # 收到凭据 → 解除 AUTH_UNAVAILABLE 熔断
    if sess.auth_state == "AUTH_UNAVAILABLE":
        _set_auth_state(sess, "NEED_LOGIN")
    trigger_recovery_nowait(sess, reason="USER_LOGIN_REQUEST")
    logger.info(f"[Login] 触发后台恢复流程，session={req.session_key[:8]}")

    return {
        "ok": True, "code": 0,
        "message": "登录任务已触发，请稍候后调用 /sync 或 /auth/ensure 检查状态",
        "session_key": req.session_key,
    }


# ── 第 2 层续命：浏览器心跳（让 JS 自动续期 serviceToken） ──

_BROWSER_HEARTBEAT_INTERVAL    = int(os.getenv("HEARTBEAT_INTERVAL", "1800"))
_BROWSER_HEARTBEAT_MAX_PER_DAY = int(os.getenv("HEARTBEAT_MAX_PER_DAY", "24"))
_BROWSER_HEARTBEAT_SYNC_WINDOW = int(os.getenv("HEARTBEAT_SYNC_WINDOW", "3600"))


async def _ensure_heartbeat_page(sess: SessionState) -> Optional[Page]:
    """获取或创建心跳专用 page，长期复用。"""
    if sess.heartbeat_page is not None and not sess.heartbeat_page.is_closed():
        logger.debug(f"[Heartbeat] 复用已有 heartbeat_page，session={sess.session_key[:8]}")
        return sess.heartbeat_page

    if sess.context is None:
        return None

    try:
        sess.heartbeat_page = await sess.context.new_page()
        logger.info(f"[Heartbeat] 创建新 heartbeat_page，session={sess.session_key[:8]}")
        return sess.heartbeat_page
    except Exception as e:
        logger.warning(f"[Heartbeat] 创建 heartbeat_page 失败: {e}")
        return None


async def _browser_heartbeat(sess: SessionState) -> None:
    """
    用 Playwright context 访问 i.mi.com，让浏览器 JS 自动续期 session。
    成功后从 context 提取最新 cookies 更新 storage_state。

    保护机制：
      - 最近有成功 /sync 才心跳（用户不活跃时不刷）
      - recovery_lock 未被占用
      - 距上次 heartbeat 足够久（HEARTBEAT_MIN_INTERVAL_SECS）
      - 每天心跳次数上限 → 防止被识别为异常

    ★ 心跳只用于续命，不得触发 full_login。
    ★ 复用长期 heartbeat_page，不再每次 new_page()。
    """
    now = time.time()

    # 保护 -2：AUTH_UNAVAILABLE 禁止心跳
    if sess.auth_state == "AUTH_UNAVAILABLE":
        logger.debug(
            f"[AuthState] skip: AUTH_UNAVAILABLE，禁止 heartbeat，"
            f"session={sess.session_key[:8]}"
        )
        return

    # 保护 -1：poll 正在执行时不抢资源
    if poll_semaphore is not None:
        inflight = LOCATE_POLL_MAX_CONCURRENCY - poll_semaphore._value
        if inflight > 0:
            logger.info(f"[Heartbeat] skip: poll inflight={inflight}")
            return

    # 保护 0：recovery 流程进行中
    if sess.recovery_lock.locked() or sess.login_in_progress:
        logger.info("[Heartbeat] skip: recovery 流程进行中")
        return

    # 保护 1：最小间隔保护（HEARTBEAT_MIN_INTERVAL_SECS）
    since_last_hb = now - sess.last_browser_heartbeat
    if sess.last_browser_heartbeat > 0 and since_last_hb < HEARTBEAT_MIN_INTERVAL_SECS:
        logger.debug(
            f"[Heartbeat] skip: 距上次心跳 {since_last_hb:.0f}s < {HEARTBEAT_MIN_INTERVAL_SECS}s"
        )
        return

    # 保护 2：最近无成功 sync 则不心跳
    if sess.last_successful_sync <= 0:
        logger.info("[Heartbeat] skip: 尚无成功 /sync 记录")
        return
    if now - sess.last_successful_sync > _BROWSER_HEARTBEAT_SYNC_WINDOW:
        logger.info(
            f"[Heartbeat] skip: 距上次成功 sync "
            f"{now - sess.last_successful_sync:.0f}s > {_BROWSER_HEARTBEAT_SYNC_WINDOW}s"
        )
        return

    # 保护 3：_BROWSER_HEARTBEAT_INTERVAL 传统间隔
    if now - sess.last_browser_heartbeat < _BROWSER_HEARTBEAT_INTERVAL:
        return

    # 保护 4：每天心跳次数上限
    import datetime as _dt
    today_str = _dt.date.today().isoformat()
    if sess.heartbeat_count_date != today_str:
        sess.heartbeat_count_date = today_str
        sess.heartbeat_count_today = 0
    if sess.heartbeat_count_today >= _BROWSER_HEARTBEAT_MAX_PER_DAY:
        logger.info(f"[Heartbeat] skip: 今日已达上限 {_BROWSER_HEARTBEAT_MAX_PER_DAY} 次")
        return

    sess.last_browser_heartbeat = now
    sess.heartbeat_count_today += 1

    logger.info(f"[Heartbeat] start: session={sess.session_key[:8]}, 第{sess.heartbeat_count_today}次")

    try:
        if not state.browser or not state.browser.is_connected():
            logger.info("[Heartbeat] skip: browser 未连接")
            return
        if sess.context is None:
            logger.info("[Heartbeat] skip: context 为 None")
            return

        # ★ 复用长期 heartbeat_page
        page = await _ensure_heartbeat_page(sess)
        if page is None:
            logger.warning("[Heartbeat] 无法获取 heartbeat_page，跳过")
            return

        reused = "复用" if not page.url.startswith("about:") else "首次导航"
        try:
            await page.goto(XIAOMI_FIND_URL, wait_until="domcontentloaded", timeout=15_000)
        except Exception as e:
            logger.warning(f"[Heartbeat] goto 失败: {e}，尝试重建 heartbeat_page")
            # page 可能已损坏，重建
            try:
                await page.close()
            except Exception:
                pass
            sess.heartbeat_page = None
            page = await _ensure_heartbeat_page(sess)
            if page is None:
                return
            await page.goto(XIAOMI_FIND_URL, wait_until="domcontentloaded", timeout=15_000)
            reused = "重建"

        if not _is_on_find_page(page.url):
            logger.warning(f"[Heartbeat] 页面不在 find，可能 session 已失效，url={page.url[:60]}")
            # ★ 心跳不触发 full_login，仅记录
            return

        # 等待 JS 执行（可能会通过 XHR 刷新 token）
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            await asyncio.sleep(2.0)

        # 从 context 提取最新 cookies，更新 storage_state
        cookies = await sess.context.cookies("https://i.mi.com")
        if cookies:
            await _save_storage_state(sess, sess.context)
            logger.info(
                f"[Heartbeat] success: storage_state 已刷新（{reused}），"
                f"session={sess.session_key[:8]}, cookies={len(cookies)}个"
            )
        else:
            logger.warning(f"[Heartbeat] 访问后 cookie=0，session 可能已失效")

    except Exception as e:
        logger.warning(f"[Heartbeat] 异常: {e}")


@app.post("/sync")
async def sync(req: SyncReq) -> Dict[str, Any]:
    """
    同步所有设备位置。
    sync 过程中禁止创建 browser/context，只调用 API。
    401 → fire-and-forget recovery pipeline（refresh_session → full_login）。
    """
    t0   = time.time()
    sess = state.get_session(req.session_key)

    # ★ 优先从浏览器 context 提取最新 cookie（JS 持续刷新，比 storage 文件更鲜活）
    #   这样可避免 storage 快照过期导致的 ~15 分钟 401 循环
    cookies = await get_cookies_from_context(sess)
    cookie_source = "context"
    if not cookies:
        cookies = await get_cookies_from_storage(sess.storage_path)
        cookie_source = "storage"
    if not cookies:
        has_creds   = bool(sess.credentials.get("username") and sess.credentials.get("password"))
        has_storage = sess.storage_path.exists()

        # ★ cookie 为空不直接 full_login，走 recovery 管线（先 refresh_session）
        trigger_recovery_nowait(sess, reason="NO_COOKIES")

        # ── 区分 protocol_reason：让插件端能识别"可恢复"vs"不可恢复" ──
        recovery_running = sess.recovery_task is not None and not sess.recovery_task.done()
        if recovery_running:
            no_cookie_reason = "LOGIN_IN_PROGRESS"
            message = "后台登录进行中，请稍候"
        elif not has_creds and not has_storage:
            no_cookie_reason = "NO_CREDENTIALS"
            message = "无凭据且无 storage，请先调用 /auth/ensure 或 /login 注入账号密码"
        else:
            no_cookie_reason = "AUTH_FAILED"
            message = "尚未登录，已触发后台恢复流程"

        # 降噪：无凭据+无 storage 的重复日志用 DEBUG 级别
        if not has_creds and not has_storage:
            logger.debug(
                f"[Sync] NO_COOKIES（无凭据+无storage），session={req.session_key[:8]}"
            )
        else:
            logger.info(
                f"[Sync] NO_COOKIES，reason={no_cookie_reason}，session={req.session_key[:8]}"
            )

        return {
            "code": 990, "ok": False, "need_reauth": True,
            "reason": no_cookie_reason,
            "auth_state": sess.auth_state,
            "message": message,
            "devices": [], "device_count": 0,
            "cost_ms": int((time.time() - t0) * 1000),
            "session_key": req.session_key,
            "retry_after": 10 if recovery_running else None,
        }

    # 合并运行时捕获的最新 cookie（Set-Cookie 刷新的值优先）
    if sess.runtime_cookies:
        cookies.update(sess.runtime_cookies)

    hydrate_user_id_from_cookies(sess, cookies)

    code, reason, devices = await do_sync(sess, cookies, force_locate=req.force_locate)
    cost_ms     = int((time.time() - t0) * 1000)
    ok          = code == 0
    need_reauth = code == 990

    if need_reauth:
        # ★ AUTH_FAILED 不直接 full_login，走 recovery 管线（先 refresh_session）
        logger.warning(f"[Auth] cookie expired, triggering recovery，session={req.session_key[:8]}")
        trigger_recovery_nowait(sess, reason="AUTH_FAILED_990")

    if ok and devices:
        sess.device_list = [d.model_dump() for d in devices]
        sess.last_successful_sync = time.time()
        # ★ context cookie 成功 → 异步回写 storage（保持文件不过期，供重启后使用）
        if cookie_source == "context" and sess.context and not sess.storage_lock.locked():
            if time.time() - sess.last_cookie_persist > 120:
                asyncio.create_task(_save_storage_state(sess, sess.context))
        # 第 2 层续命：成功同步后触发浏览器心跳（fire-and-forget）
        asyncio.create_task(_browser_heartbeat(sess))

    logger.info(
        f"[Sync] 完成，code={code}, reason={reason}, "
        f"devices={len(devices)}, cost={cost_ms}ms, "
        f"cookie_src={cookie_source}, session={req.session_key[:8]}"
    )

    if code == 990:
        recovery_running = sess.recovery_task is not None and not sess.recovery_task.done()
        protocol_reason  = "LOGIN_IN_PROGRESS" if recovery_running else "AUTH_FAILED"
    elif code == 0:
        protocol_reason = ""
    else:
        protocol_reason = reason

    return {
        "code":        code,
        "ok":          ok,
        "reason":      protocol_reason,
        "auth_state":  sess.auth_state,
        "backend":     "xiaomi",
        "ts":          int(time.time()),
        "retry_after": 10 if code == 990 else None,
        "need_reauth": need_reauth,
        "message":     reason,
        "devices":     [d.model_dump() for d in devices],
        "device_count": len(devices),
        "cost_ms":     cost_ms,
        "session_key": req.session_key,
    }


# ══════════════════════════════════════════════════════════════════
# 命令接口（响铃 / 停止响铃 / 主动定位）
# ══════════════════════════════════════════════���═══════════════════

class CommandReq(BaseModel):
    """POST /command/* 请求体"""
    session_key: str
    device_id: str   # fid（/sync 返回的 device_id/fid 字段）


async def call_xiaomi_api_post(
    path: str,
    body: Dict[str, Any],
    cookies: Dict[str, str],
    sess: SessionState,
    timeout: float = 10.0,
) -> Optional[Dict[str, Any]]:
    """
    向 i.mi.com 发送 POST 请求（用于命令接口）。
    错误处理对齐 call_xiaomi_api（GET 版本）。
    """
    url     = f"{XIAOMI_API_BASE}{path}"
    headers = dict(XIAOMI_BASE_HEADERS)
    headers["Cookie"]       = cookies_to_header(cookies)
    headers["Content-Type"] = "application/x-www-form-urlencoded"

    endpoint_label = path.split("/")[-1] or path

    try:
        async with state.http_session.post(
            url,
            data=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            status = r.status
            text   = await r.text(encoding="utf-8", errors="replace")

            if status in (401, 403):
                sts_url = _parse_sts_url(text)
                _sts_display = (sts_url[:80] + "...") if (sts_url and len(sts_url) > 80) else sts_url
                logger.error(
                    f"[CMD] {endpoint_label} HTTP {status}，"
                    f"body前200={text[:200]}，sts_url(截断)={_sts_display}"
                )
                sess.user_id = ""
                ret: Dict[str, Any] = {"code": 990, "info": f"HTTP_{status}_AUTH_FAILED"}
                if sts_url:
                    ret["sts_url"] = sts_url
                return ret

            if status != 200:
                logger.error(f"[CMD] {endpoint_label} HTTP {status}, body={text[:200]}")
                return {"code": -1, "info": f"HTTP_{status}"}

            if _is_html(text):
                logger.error(f"[CMD] {endpoint_label} 返回 HTML（session 失效），body={text[:200]}")
                sess.user_id = ""
                return {"code": 990, "info": "SESSION_EXPIRED_HTML"}

            # ── 第 1 层续命：捕获 Set-Cookie ──
            auth_updated = _absorb_set_cookies(sess, r.headers)
            if auth_updated:
                _schedule_persist_cookies(sess)

            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                logger.error(f"[CMD] {endpoint_label} JSON 解析失败，body={text[:200]}")
                return {"code": -1, "info": "JSON_DECODE_ERROR"}

            return data

    except asyncio.TimeoutError:
        logger.error(f"[CMD] {endpoint_label} 请求超时（{timeout}s）")
        return {"code": -1, "info": "TIMEOUT"}
    except aiohttp.ClientError as e:
        logger.error(f"[CMD] {endpoint_label} 网络异常: {e}")
        return {"code": -1, "info": f"CLIENT_ERROR: {e}"}
    except Exception as e:
        logger.exception(f"[CMD] {endpoint_label} 未知异常: {e}")
        return {"code": -1, "info": f"EXCEPTION: {e}"}


async def _execute_command(
    sess: SessionState,
    device_id: str,
    command_type: int,
    action: str,
) -> Dict[str, Any]:
    """
    统一命令执行逻辑（响铃/停止/定位）。
    401 → fire-and-forget recovery pipeline → 返回 code=990。
    """
    t0 = time.time()

    cached = sess.device_cache.get(device_id)
    if not cached:
        logger.warning("[CMD] device_id=...%s 不在缓存中，请先调用 /sync", device_id[-4:])
        return {
            "code": -2, "reason": "DEVICE_NOT_FOUND",
            "device_id": device_id, "action": action,
            "request_id": "", "cost_ms": int((time.time() - t0) * 1000),
        }

    target_user_id = cached.get("user_id", "")
    target_fid     = cached.get("fid", device_id)
    target_comp_id = cached.get("component_id", "") or "0"

    if not target_user_id:
        logger.warning("[CMD] device=...%s 无 user_id，无法发送命令", device_id[-4:])
        return {
            "code": -2, "reason": "NO_USER_ID",
            "device_id": device_id, "action": action,
            "request_id": "", "cost_ms": int((time.time() - t0) * 1000),
        }

    body: Dict[str, Any] = {
        "targetUserId":      target_user_id,
        "targetFid":         target_fid,
        "targetComponentId": target_comp_id,
        "commandType":       command_type,
    }
    if command_type == 6:
        body["fromDeviceName"] = "HomeAssistant"

    async def _do_call() -> Optional[Dict[str, Any]]:
        cookies = await get_cookies_from_context(sess) or await get_cookies_from_storage(sess.storage_path)
        if not cookies:
            return {"code": 990, "info": "NO_COOKIES"}
        hydrate_user_id_from_cookies(sess, cookies)
        form_body = dict(body)
        svc_token = cookies.get("serviceToken", "")
        if svc_token:
            form_body["serviceToken"] = svc_token
        return await call_xiaomi_api_post(PATH_COMMAND_SEND, form_body, cookies, sess, timeout=10.0)

    result = await _do_call()

    if result is not None and result.get("code") == 990:
        logger.warning(
            "[CMD] 鉴权失败，触发后台恢复流程，action=%s device=...%s",
            action, device_id[-4:],
        )
        trigger_recovery_nowait(sess, reason="CMD_HTTP_401")
        return {
            "code": 990, "reason": "AUTH_FAILED_990",
            "device_id": device_id, "action": action,
            "request_id": "", "cost_ms": int((time.time() - t0) * 1000),
        }

    cost_ms = int((time.time() - t0) * 1000)

    if result is None:
        logger.error("[CMD] action=%s device=...%s 调用返回 None cost=%dms", action, device_id[-4:], cost_ms)
        return {
            "code": -1, "reason": "CALL_FAILED",
            "device_id": device_id, "action": action,
            "request_id": "", "cost_ms": cost_ms,
        }

    r_code     = result.get("code", -1)
    request_id = str(result.get("cmdId") or result.get("taskId") or result.get("id") or "")

    logger.info(
        "[CMD] action=%s device=...%s commandType=%d code=%d cost=%dms",
        action, device_id[-4:], command_type, r_code, cost_ms,
    )
    if r_code != 0:
        logger.warning("[CMD] 命令返回非0，body前200=%s", str(result)[:200])

    return {
        "code":       0 if r_code == 0 else r_code,
        "reason":     "" if r_code == 0 else str(result.get("description") or result.get("info") or r_code),
        "device_id":  device_id,
        "action":     action,
        "request_id": request_id,
        "cost_ms":    cost_ms,
    }


@app.post("/command/ring")
async def command_ring(req: CommandReq) -> Dict[str, Any]:
    """发送响铃命令（commandType=6），30 秒冷却防误触。"""
    t0   = time.time()
    sess = state.get_session(req.session_key)

    last_ring = sess.ring_last_trigger.get(req.device_id, 0.0)
    elapsed   = time.time() - last_ring
    if elapsed < RING_COOLDOWN_SECS:
        remain = int(RING_COOLDOWN_SECS - elapsed)
        logger.info("[CMD] ring 冷却中 device=...%s 剩余 %ds", req.device_id[-4:], remain)
        return {
            "code": 429, "reason": "RING_COOLDOWN",
            "device_id": req.device_id, "action": "ring",
            "cooldown_remain_s": remain,
            "request_id": "", "cost_ms": int((time.time() - t0) * 1000),
        }

    result = await _execute_command(sess, req.device_id, 6, "ring")
    if result.get("code") == 0:
        sess.ring_last_trigger[req.device_id] = time.time()
    return result


@app.post("/command/stop_ring")
async def command_stop_ring(req: CommandReq) -> Dict[str, Any]:
    """发送停止响铃命令（commandType=7），不限频。"""
    sess = state.get_session(req.session_key)
    return await _execute_command(sess, req.device_id, 7, "stop_ring")


@app.post("/command/locate")
async def command_locate(req: CommandReq) -> Dict[str, Any]:
    """发送主动定位命令（commandType=34），60 秒冷却。"""
    t0   = time.time()
    sess = state.get_session(req.session_key)

    last_loc = sess.locate_last_trigger.get(req.device_id, 0.0)
    elapsed  = time.time() - last_loc
    if elapsed < LOCATE_CMD_COOLDOWN_SECS:
        remain = int(LOCATE_CMD_COOLDOWN_SECS - elapsed)
        logger.info("[CMD] locate 冷却中 device=...%s 剩余 %ds", req.device_id[-4:], remain)
        return {
            "code": 429, "reason": "LOCATE_COOLDOWN",
            "device_id": req.device_id, "action": "locate",
            "cooldown_remain_s": remain,
            "request_id": "", "cost_ms": int((time.time() - t0) * 1000),
        }

    result = await _execute_command(sess, req.device_id, 34, "locate")
    if result.get("code") == 0:
        sess.locate_last_trigger[req.device_id] = time.time()
    return result


# ══════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    host      = os.getenv("HOST", "0.0.0.0")
    port      = int(os.getenv("PORT", "8080"))
    log_level = os.getenv("LOG_LEVEL", "info").lower()
    if os.getenv("DEBUG_MODE", "0").strip() == "1":
        log_level = "debug"

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )

    uvicorn.run(app, host=host, port=port, log_level=log_level, access_log=True)
