# -*- coding: utf-8 -*-
"""
XingSwitch —— 显示器输入源切换
--------------------------------
电脑上常驻托盘，右键直接选输入源，左键打开控制窗口；
同时开一个极小的 HTTP 服务，手机连同一 WiFi 用浏览器切换。

原理：Windows 的 dxva2.dll 可以直接对显示器发 DDC/CI 命令（VCP 0x60 = 输入源）。
零第三方依赖：只用 Python 标准库 + ctypes。
"""
import ctypes
import json
import logging
import logging.handlers
import os
import queue
import socket
import struct
import sys
import threading
import time
import tkinter as tk
import zlib
from contextlib import contextmanager
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

APP_NAME = "XingSwitch"
APP_VERSION = "1.1.0"

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RES_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    RES_DIR = BASE_DIR

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_PATH = os.path.join(BASE_DIR, "xingswitch.log")
UI_PATH = os.path.join(BASE_DIR, "ui.html")
UI_PATH_BUNDLED = os.path.join(RES_DIR, "ui.html")
ICON_PATH = os.path.join(BASE_DIR, "icon.ico")

log = logging.getLogger("xingswitch")

# ---------------------------------------------------------------- 常量

VCP_INPUT = 0x60

INPUT_NAMES = {
    0x01: "VGA-1", 0x02: "VGA-2",
    0x03: "DVI-1", 0x04: "DVI-2",
    0x05: "Composite-1", 0x06: "Composite-2",
    0x07: "S-Video-1", 0x08: "S-Video-2",
    0x0C: "Component-1", 0x0D: "Component-2",
    0x0F: "DisplayPort-1", 0x10: "DisplayPort-2",
    0x11: "HDMI-1", 0x12: "HDMI-2", 0x13: "HDMI-3", 0x14: "HDMI-4",
    0x15: "HDMI-5", 0x16: "HDMI-6",
    0x1B: "USB-C", 0x1C: "USB-C-2",
}

DEFAULT_LABELS = {0x11: "电脑", 0x12: "Switch", 0x0F: "DP", 0x01: "VGA"}

FALLBACK_INPUTS = [0x11, 0x12, 0x0F, 0x10, 0x01, 0x03]

INPUT_PRIORITY = {0x11: 0, 0x12: 1, 0x13: 2, 0x14: 3,
                  0x0F: 4, 0x10: 5, 0x1B: 6, 0x1C: 7}

DEFAULT_CONFIG = {
    "port": 8765,
    "preventSleep": True,
    "tray": True,
    "monitor": "",
    "labels": {},
    "hidden": [],
    "usage": {},
    # 开源后请把这里改成真实仓库地址，界面上的 GitHub 链接会跟着变
    "projectUrl": "https://github.com/xing/xingswitch",
}

# ---------------------------------------------------------------- Win32 绑定

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_ubyte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                              wintypes.WPARAM, wintypes.LPARAM)

_user32.RegisterClassW.restype = wintypes.WORD
_user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
_user32.DefWindowProcW.restype = ctypes.c_ssize_t
_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.DestroyWindow.argtypes = [wintypes.HWND]
_user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
_user32.LoadIconW.restype = wintypes.HICON
_user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
_user32.LoadImageW.restype = wintypes.HICON
_user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                               ctypes.c_int, ctypes.c_int, wintypes.UINT]
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.CreatePopupMenu.restype = wintypes.HMENU
_user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
_user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
_user32.TrackPopupMenu.restype = ctypes.c_int
_user32.DestroyMenu.argtypes = [wintypes.HMENU]
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.PostQuitMessage.argtypes = [ctypes.c_int]
_user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
_user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]


class _PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [
        ("hPhysicalMonitor", wintypes.HANDLE),
        ("szPhysicalMonitorDescription", wintypes.WCHAR * 128),
    ]


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
    ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)


def _bind_lib():
    dx = ctypes.WinDLL("dxva2.dll")
    dx.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [wintypes.HMONITOR, ctypes.POINTER(wintypes.DWORD)]
    dx.GetNumberOfPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL
    dx.GetPhysicalMonitorsFromHMONITOR.argtypes = [wintypes.HMONITOR, wintypes.DWORD, ctypes.POINTER(_PHYSICAL_MONITOR)]
    dx.GetPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL
    dx.DestroyPhysicalMonitors.argtypes = [wintypes.DWORD, ctypes.POINTER(_PHYSICAL_MONITOR)]
    dx.DestroyPhysicalMonitors.restype = wintypes.BOOL
    dx.GetVCPFeatureAndVCPFeatureReply.argtypes = [
        wintypes.HANDLE, ctypes.c_ubyte, ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
    dx.GetVCPFeatureAndVCPFeatureReply.restype = wintypes.BOOL
    dx.SetVCPFeature.argtypes = [wintypes.HANDLE, ctypes.c_ubyte, wintypes.DWORD]
    dx.SetVCPFeature.restype = wintypes.BOOL
    dx.GetCapabilitiesStringLength.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    dx.GetCapabilitiesStringLength.restype = wintypes.BOOL
    dx.CapabilitiesRequestAndCapabilitiesReply.argtypes = [wintypes.HANDLE, ctypes.c_char_p, wintypes.DWORD]
    dx.CapabilitiesRequestAndCapabilitiesReply.restype = wintypes.BOOL
    return dx


_dx = _bind_lib()

# ---------------------------------------------------------------- DDC/CI 核心

_DDC_LOCK = threading.RLock()
_SWITCH_LOCK = threading.Lock()
_CAPS_CACHE = {}

# DDC 防护：这类显示器的 DDC 固件很脆，操作太密会把它的状态机打死，
# 打死之后连实体按键之外没有任何软件手段能救回来，所以必须自己限速。
COOLDOWN_S = 3.0          # 两次切换之间的最小间隔
SETTLE_S = 1.2            # 切换后先等多久再读回校验
FAIL_LIMIT = 3            # 连续失败几次就熔断
OPEN_S = 30.0             # 熔断持续时间

_LAST_SWITCH_AT = 0.0
_FAIL_STREAK = 0
_OPEN_UNTIL = 0.0

DDC_STUCK_MSG = ("显示器 DDC 无响应，已暂停操作 30 秒。"
                 "画面如果正常，说明显示器的 DDC 引擎卡住了："
                 "把 HDMI 线拔下来等 3 秒再插回去就能复位，"
                 "然后点下面的「重新检测」。")


def _breaker_open():
    return time.time() < _OPEN_UNTIL


def _note_ddc(ok):
    global _FAIL_STREAK, _OPEN_UNTIL
    if ok:
        _FAIL_STREAK = 0
        _OPEN_UNTIL = 0.0
    else:
        _FAIL_STREAK += 1
        if _FAIL_STREAK >= FAIL_LIMIT:
            _OPEN_UNTIL = time.time() + OPEN_S
            log.warning("DDC 连续失败 %d 次，熔断 %.0f 秒", _FAIL_STREAK, OPEN_S)


def reset_breaker():
    global _FAIL_STREAK, _OPEN_UNTIL
    _FAIL_STREAK = 0
    _OPEN_UNTIL = 0.0
    log.info("DDC 熔断已手动复位")


@contextmanager
def enumerate_monitors():
    """每次操作都重新枚举 —— 输入源一变，旧句柄就作废了。"""
    items = []
    arrays = []

    def _cb(hmon, hdc, lprect, lparam):
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if not _user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return True
        n = wintypes.DWORD(0)
        if not _dx.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, ctypes.byref(n)) or n.value == 0:
            return True
        arr = (_PHYSICAL_MONITOR * n.value)()
        if not _dx.GetPhysicalMonitorsFromHMONITOR(hmon, n.value, arr):
            return True
        arrays.append((n.value, arr))
        for i in range(n.value):
            pm = arr[i]
            items.append({
                "handle": pm.hPhysicalMonitor,
                "desc": pm.szPhysicalMonitorDescription or "显示器",
                "device": info.szDevice,
                "primary": bool(info.dwFlags & 1),
                "width": info.rcMonitor.right - info.rcMonitor.left,
                "height": info.rcMonitor.bottom - info.rcMonitor.top,
            })
        return True

    _user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(_cb), 0)
    try:
        yield items
    finally:
        for cnt, arr in arrays:
            try:
                _dx.DestroyPhysicalMonitors(cnt, arr)
            except Exception:
                pass


def read_vcp(handle, code):
    cur = wintypes.DWORD(0)
    mx = wintypes.DWORD(0)
    typ = wintypes.DWORD(0)
    if _dx.GetVCPFeatureAndVCPFeatureReply(handle, code, ctypes.byref(typ), ctypes.byref(cur), ctypes.byref(mx)):
        return cur.value
    return None


def write_vcp(handle, code, value):
    return bool(_dx.SetVCPFeature(handle, code, value))


def read_capabilities(handle):
    ln = wintypes.DWORD(0)
    if not _dx.GetCapabilitiesStringLength(handle, ctypes.byref(ln)) or ln.value == 0:
        return None
    buf = ctypes.create_string_buffer(ln.value + 1)
    if not _dx.CapabilitiesRequestAndCapabilitiesReply(handle, buf, ln.value):
        return None
    try:
        return buf.value.decode("ascii", "ignore")
    except Exception:
        return None


def parse_supported_inputs(cap):
    """从 capabilities 里抠 60(...) 这一段。"""
    if not cap:
        return None
    i = cap.find("60(")
    if i < 0:
        return None
    start = i + 3
    depth = 0
    j = start
    while j < len(cap):
        c = cap[j]
        if c == "(":
            depth += 1
        elif c == ")":
            if depth == 0:
                break
            depth -= 1
        j += 1
    body = cap[start:j]
    vals = []
    for tok in body.split():
        try:
            v = int(tok.strip("()"), 16)
        except ValueError:
            continue
        if v not in vals:
            vals.append(v)
    return vals or None


def input_name(value):
    return INPUT_NAMES.get(value, "输入 0x%02X" % value)


def supported_inputs(mon):
    key = (mon["device"], mon["desc"])
    if key in _CAPS_CACHE:
        return _CAPS_CACHE[key]
    cap = read_capabilities(mon["handle"])
    found = parse_supported_inputs(cap)
    _CAPS_CACHE[key] = found
    if not found:
        log.info("读不到 %s 的 capabilities，输入源列表用兜底值", mon["desc"])
    return found


def pick_monitor(mons):
    if not mons:
        return None
    want = CONFIG.get("monitor") or ""
    if want:
        for m in mons:
            if m["device"] == want:
                return m
    for m in mons:
        if m["primary"]:
            return m
    return mons[0]

# ---------------------------------------------------------------- 配置

CONFIG = dict(DEFAULT_CONFIG)
_CFG_LOCK = threading.RLock()


def _merge(cfg):
    out = dict(DEFAULT_CONFIG)
    if isinstance(cfg, dict):
        out.update({k: v for k, v in cfg.items() if k in DEFAULT_CONFIG})
    if not isinstance(out.get("labels"), dict):
        out["labels"] = {}
    if not isinstance(out.get("hidden"), list):
        out["hidden"] = []
    if not isinstance(out.get("usage"), dict):
        out["usage"] = {}
    if not isinstance(out.get("projectUrl"), str):
        out["projectUrl"] = DEFAULT_CONFIG["projectUrl"]
    try:
        out["port"] = int(out.get("port") or 8765)
    except (TypeError, ValueError):
        out["port"] = 8765
    return out


def load_config():
    global CONFIG
    raw = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            log.warning("config.json 读取失败，用默认值：%s", e)
            raw = {}
    cfg = _merge(raw)

    if not cfg["labels"] and not os.path.exists(CONFIG_PATH):
        for k, v in DEFAULT_LABELS.items():
            cfg["labels"][str(k)] = v

    with _CFG_LOCK:
        CONFIG = cfg
    if not os.path.exists(CONFIG_PATH):
        save_config()
    return cfg


def save_config():
    with _CFG_LOCK:
        data = json.dumps(CONFIG, ensure_ascii=False, indent=2)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, CONFIG_PATH)


def label_of(value):
    with _CFG_LOCK:
        lab = CONFIG["labels"].get(str(value))
    return lab or DEFAULT_LABELS.get(value) or input_name(value)


def is_hidden(value):
    with _CFG_LOCK:
        return value in CONFIG["hidden"]


def set_hidden(value, hidden):
    with _CFG_LOCK:
        cur = set(CONFIG["hidden"])
        if hidden:
            cur.add(value)
        else:
            cur.discard(value)
        CONFIG["hidden"] = sorted(cur)
    save_config()


def used_at(value):
    with _CFG_LOCK:
        return int(CONFIG["usage"].get(str(value)) or 0)


def mark_used(value):
    """切成功一次就记一笔，用来判断这个口是不是真的在用。"""
    with _CFG_LOCK:
        CONFIG["usage"][str(value)] = int(time.time())
    if is_hidden(value):
        set_hidden(value, False)
        log.info("%s 被使用过，自动取消隐藏", label_of(value))
    else:
        save_config()


def project_url():
    with _CFG_LOCK:
        return CONFIG.get("projectUrl") or DEFAULT_CONFIG["projectUrl"]

# ---------------------------------------------------------------- 业务逻辑

def _probe_state(tries=3, delay=0.4):
    """读当前输入源。刚切完那几百毫秒显示器可能不应答，所以要重试；
    但也不能猛敲 —— 连续失败会熔断，交给人工复位。"""
    if _breaker_open():
        return None, None, None
    mon = None
    for i in range(tries):
        with _DDC_LOCK:
            with enumerate_monitors() as mons:
                mon = pick_monitor(mons)
                if mon:
                    cur = read_vcp(mon["handle"], VCP_INPUT)
                    if cur is not None:
                        _note_ddc(True)
                        return mon, cur, supported_inputs(mon)
        if i < tries - 1:
            time.sleep(delay)
    _note_ddc(False)
    return mon, None, None


_STATE_CACHE = {"at": 0.0, "data": None}


def build_state(force=False):
    now = time.time()
    if not force and _STATE_CACHE["data"] and now - _STATE_CACHE["at"] < 5:
        return _STATE_CACHE["data"]

    if _breaker_open():
        return {"ok": False, "error": DDC_STUCK_MSG, "stuck": True}

    mon, cur, sup = _probe_state()
    if not mon:
        return {"ok": False, "error": "没有检测到物理显示器"}
    if cur is None:
        data = {"ok": False, "error": DDC_STUCK_MSG if _breaker_open()
                else "显示器没有响应 DDC/CI。如果画面正常，可能是显示器的 DDC 引擎卡住了，"
                     "拔插一次 HDMI 线即可复位。",
                "stuck": _breaker_open(),
                "monitor": {"device": mon["device"], "desc": mon["desc"]}}
        return data

    values = list(sup) if sup else list(FALLBACK_INPUTS)
    if cur not in values:
        values.insert(0, cur)
    values = list(dict.fromkeys(values))
    values.sort(key=lambda v: (INPUT_PRIORITY.get(v, 50), v))

    inputs = []
    for v in values:
        inputs.append({
            "value": v,
            "raw": input_name(v),
            "label": label_of(v),
            "hidden": is_hidden(v),
            "active": v == cur,
            "usedAt": used_at(v),
        })
    data = {
        "ok": True,
        "monitor": {
            "device": mon["device"],
            "desc": mon["desc"],
            "resolution": "%dx%d" % (mon["width"], mon["height"]),
        },
        "current": cur,
        "currentLabel": label_of(cur),
        "currentRaw": input_name(cur),
        "inputs": inputs,
        "lanUrl": LAN_URL,
        "version": APP_VERSION,
        "projectUrl": project_url(),
        "uptime": int(time.time() - START_TIME),
    }
    _STATE_CACHE["at"] = time.time()
    _STATE_CACHE["data"] = data
    return data


def switch_to(value):
    global _LAST_SWITCH_AT
    if not _SWITCH_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "上一次切换还没结束，稍等一下"}
    try:
        if _breaker_open():
            return {"ok": False, "error": DDC_STUCK_MSG, "stuck": True}

        wait = COOLDOWN_S - (time.time() - _LAST_SWITCH_AT)
        if wait > 0:
            return {"ok": False, "error": "刚切过一次，请等 %d 秒再切" % int(wait + 0.99)}

        t0 = time.time()
        with _DDC_LOCK:
            with enumerate_monitors() as mons:
                mon = pick_monitor(mons)
                if not mon:
                    return {"ok": False, "error": "没有检测到物理显示器"}
                # 显示器 DDC 偶尔会漏一次应答，先给三次机会再判定
                cur = None
                for _ in range(3):
                    cur = read_vcp(mon["handle"], VCP_INPUT)
                    if cur is not None:
                        break
                    time.sleep(0.35)
                if cur is None:
                    _note_ddc(False)
                    msg = DDC_STUCK_MSG if _breaker_open() else \
                        "显示器没有响应 DDC/CI，命令没发出去"
                    return {"ok": False, "error": msg, "stuck": _breaker_open()}
                _note_ddc(True)
                sup = supported_inputs(mon)
                if sup and value not in sup:
                    return {"ok": False,
                            "error": "这个输入源不在显示器的支持列表里，已拦下"}
                if cur == value:
                    mark_used(value)
                    return {"ok": True, "current": value, "label": label_of(value),
                            "unchanged": True, "message": "已经在 %s 了" % label_of(value)}

                ok = write_vcp(mon["handle"], VCP_INPUT, value)
                if not ok:
                    time.sleep(0.3)
                    ok = write_vcp(mon["handle"], VCP_INPUT, value)
                if not ok:
                    log.warning("切换 %s -> 0x%02X 写入失败", mon["desc"], value)
                    _note_ddc(False)
                    return {"ok": False, "error": "显示器拒绝了切换命令", "stuck": _breaker_open()}
                _LAST_SWITCH_AT = time.time()

        # 温柔校验：先让它喘口气，再最多读三次
        time.sleep(SETTLE_S)
        for _ in range(3):
            with _DDC_LOCK:
                with enumerate_monitors() as mons:
                    mon = pick_monitor(mons)
                    now = read_vcp(mon["handle"], VCP_INPUT) if mon else None
            if now == value:
                _note_ddc(True)
                _STATE_CACHE["data"] = None
                mark_used(value)
                log.info("切换成功 -> %s (0x%02X)，耗时 %.0f ms",
                         label_of(value), value, (time.time() - t0) * 1000)
                return {"ok": True, "current": value, "label": label_of(value),
                        "message": "已切换到 %s" % label_of(value)}
            if now is None:
                _note_ddc(False)
                if _breaker_open():
                    return {"ok": False, "error": DDC_STUCK_MSG, "stuck": True}
            time.sleep(0.8)

        log.warning("切换 0x%02X 命令已发送但回读不符", value)
        _STATE_CACHE["data"] = None
        return {"ok": False, "error": "命令发出去了，但显示器没有切换过来"}
    except Exception as e:
        log.exception("切换异常")
        return {"ok": False, "error": "内部错误：%s" % e}
    finally:
        _SWITCH_LOCK.release()

# ---------------------------------------------------------------- 网络

def detect_lan_ip():
    ip = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("223.5.5.5", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    if not ip or ip.startswith("127."):
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            ip = "127.0.0.1"
    return ip


LAN_URL = ""


def local_ip():
    return detect_lan_ip()

# ---------------------------------------------------------------- 图标

def _icon_pixels(size):
    bg = (16, 19, 24, 255)
    fg = (96, 235, 170, 255)
    buf = bytearray()

    def rr(px, py, x0, y0, x1, y1, r):
        if px < x0 or px > x1 or py < y0 or py > y1:
            return False
        cx = min(max(px, x0 + r), x1 - r)
        cy = min(max(py, y0 + r), y1 - r)
        return (px - cx) ** 2 + (py - cy) ** 2 <= r * r

    s = float(size)
    sx0, sx1 = 0.16 * s, 0.84 * s
    sy0, sy1 = 0.22 * s, 0.60 * s
    r = max(1.2, 0.05 * s)
    nk0, nk1 = 0.46 * s, 0.54 * s
    ny0, ny1 = sy1, 0.72 * s
    bx0, bx1 = 0.34 * s, 0.66 * s
    by0, by1 = 0.72 * s, 0.78 * s

    for y in range(size):
        for x in range(size):
            c = bg
            if rr(x, y, sx0, sy0, sx1, sy1, r):
                c = fg
                if rr(x, y, sx0 + 0.05 * s, sy0 + 0.05 * s, sx1 - 0.05 * s, sy1 - 0.05 * s, r * 0.6):
                    c = bg
            if nk0 <= x <= nk1 and ny0 <= y <= ny1:
                c = fg
            if rr(x, y, bx0, by0, bx1, by1, r * 0.5):
                c = fg
            buf += bytes(c)
    return bytes(buf)


def build_png(size=192):
    raw = bytearray()
    px = _icon_pixels(size)
    stride = size * 4
    for y in range(size):
        raw += b"\x00" + px[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


_PNG_CACHE = {}


def png_bytes(size=192):
    if size not in _PNG_CACHE:
        _PNG_CACHE[size] = build_png(size)
    return _PNG_CACHE[size]


def write_icon_file(path):
    """生成多尺寸 .ico，给快捷方式用（ICO 里直接嵌 PNG，Vista 以后都认）。"""
    sizes = [16, 32, 48, 256]
    images = [png_bytes(s) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for s, img in zip(sizes, images):
        dim = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(img), offset)
        offset += len(img)
    with open(path, "wb") as f:
        f.write(header + entries + b"".join(images))


def make_hicon(size=32):
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class RGBQUAD(ctypes.Structure):
        _fields_ = [("b", ctypes.c_ubyte), ("g", ctypes.c_ubyte),
                    ("r", ctypes.c_ubyte), ("a", ctypes.c_ubyte)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                    ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HANDLE),
                    ("hbmColor", wintypes.HANDLE)]

    gdi32.CreateDIBSection.restype = wintypes.HANDLE
    gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO),
                                       wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                       wintypes.HANDLE, wintypes.DWORD]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _user32.CreateIconIndirect.restype = wintypes.HICON
    _user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
    gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL

    hdc = gdi32.CreateCompatibleDC(None)
    if not hdc:
        return None

    def dib(bits_per_px):
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = size
        bmi.bmiHeader.biHeight = -size
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = bits_per_px
        bmi.bmiHeader.biCompression = 0
        bits = ctypes.c_void_p()
        hbm = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        return hbm, bits

    hbm_color, bits_color = dib(32)
    hbm_mask, bits_mask = dib(1)
    if not hbm_color or not hbm_mask:
        gdi32.DeleteDC(hdc)
        return None

    px = _icon_pixels(size)
    bgra = bytearray()
    for i in range(0, len(px), 4):
        r, g, b, a = px[i], px[i + 1], px[i + 2], px[i + 3]
        bgra += bytes((b, g, r, a))
    ctypes.memmove(bits_color, bytes(bgra), len(bgra))
    ctypes.memset(bits_mask, 0, ((size + 31) // 32) * 4 * size)

    ii = ICONINFO()
    ii.fIcon = 1
    ii.hbmMask = hbm_mask
    ii.hbmColor = hbm_color
    hicon = _user32.CreateIconIndirect(ctypes.byref(ii))
    gdi32.DeleteObject(hbm_color)
    gdi32.DeleteObject(hbm_mask)
    gdi32.DeleteDC(hdc)
    return hicon

# ---------------------------------------------------------------- 托盘

def load_icon(size=32):
    """优先从 icon.ico 读图标 —— 比运行时拼 HICON 稳，打包后也能用。"""
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x10
    for p in (ICON_PATH, os.path.join(RES_DIR, "icon.ico")):
        try:
            if p and os.path.exists(p):
                h = _user32.LoadImageW(None, p, IMAGE_ICON, size, size, LR_LOADFROMFILE)
                if h:
                    return h
        except Exception:
            pass
    return None

class TrayIcon:
    WM_TRAY = 0x8000 + 1
    WM_DESTROY = 0x0002
    WM_CLOSE = 0x0010
    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    NIM_ADD, NIM_DELETE = 0, 2
    NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
    TPM_RETURNCMD, TPM_RIGHTBUTTON, TPM_NONOTIFY = 0x0100, 0x0002, 0x0080
    MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
    MF_CHECKED, MF_GRAYED = 0x0008, 0x0001
    ID_OPEN, ID_ADDR, ID_QUIT = 1, 2, 3
    CLASS_NAME = "XingSwitchTrayWnd"

    def __init__(self, title):
        self.title = title
        self.hwnd = None
        self.hicon = None
        self._ready = threading.Event()
        self._proc = None
        self._shell32 = None
        self._icon_added = False
        self._icon_nid = None
        self._retrying = False
        self._menu_values = []

    def start(self):
        threading.Thread(target=self._run, name="tray", daemon=True).start()
        self._ready.wait(6.0)
        return bool(self.hwnd)

    def _nid(self, flags, callback_msg):
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = flags
        nid.uCallbackMessage = callback_msg
        nid.hIcon = self.hicon
        nid.szTip = self.title[:120]
        return nid

    def _run(self):
        try:
            shell32 = ctypes.WinDLL("shell32", use_last_error=True)
            shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)]
            shell32.Shell_NotifyIconW.restype = wintypes.BOOL

            hinst = _kernel32.GetModuleHandleW(None)
            self._proc = _WNDPROC(self._wndproc)

            wc = _WNDCLASSW()
            wc.lpfnWndProc = ctypes.cast(self._proc, ctypes.c_void_p).value
            wc.hInstance = hinst
            wc.lpszClassName = self.CLASS_NAME
            _user32.RegisterClassW(ctypes.byref(wc))

            self.hwnd = _user32.CreateWindowExW(
                0, self.CLASS_NAME, APP_NAME, 0, 0, 0, 0, 0, None, None, hinst, None)
            if not self.hwnd:
                log.error("托盘窗口创建失败，错误码 %s", ctypes.get_last_error())
                return

            self.hicon = load_icon() or make_hicon(32) or _user32.LoadIconW(None, ctypes.c_void_p(32512))
            pid = wintypes.DWORD(0)
            _user32.GetWindowThreadProcessId(self.hwnd, ctypes.byref(pid))
            log.info("托盘诊断：hwnd=%s isWindow=%s ownerPid=%s selfPid=%s hicon=%s",
                     self.hwnd, _user32.IsWindow(self.hwnd), pid.value,
                     _kernel32.GetCurrentProcessId(), self.hicon)

            self._shell32 = shell32
            self._add_icon()

            self._ready.set()
            msg = wintypes.MSG()
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))

            nid2 = self._nid(0, 0)
            shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(nid2))
            if self.hwnd:
                _user32.DestroyWindow(self.hwnd)
                self.hwnd = None
        except Exception:
            log.exception("托盘线程异常")
        finally:
            self._ready.set()

    def _add_icon(self):
        """加托盘图标；失败就退避重试（任务栏刚开始初始化时也可能失败）。"""
        nid = self._nid(self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP, self.WM_TRAY)
        if self._shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(nid)):
            self._icon_added = True
            self._icon_nid = nid
            log.info("托盘图标已就绪")
            return True
        err = ctypes.get_last_error()
        log.warning("托盘图标添加失败（错误码 %s），稍后重试", err)
        if not self._retrying:
            self._retrying = True
            threading.Thread(target=self._retry_loop, daemon=True).start()
        return False

    def _retry_loop(self):
        for _ in range(15):
            time.sleep(2)
            if self._icon_added or not self.hwnd:
                return
            if self._add_icon_silent():
                return
        log.error("托盘图标始终加不上，程序继续以无托盘模式运行")

    def _add_icon_silent(self):
        nid = self._nid(self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP, self.WM_TRAY)
        if self._shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(nid)):
            self._icon_added = True
            log.info("托盘图标已就绪（重试成功）")
            return True
        return False

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == self.WM_TRAY:
                if lparam == self.WM_RBUTTONUP:
                    self._menu()
                elif lparam in (self.WM_LBUTTONUP, self.WM_LBUTTONDBLCLK):
                    show_window()
                return 0
            if msg == self.WM_CLOSE:
                _user32.DestroyWindow(hwnd)
                return 0
            if msg == self.WM_DESTROY:
                _user32.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("托盘消息处理异常")
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _menu(self):
        entries = menu_entries()
        self._menu_values = [e[0] for e in entries]

        hmenu = _user32.CreatePopupMenu()
        for i, (value, label, active) in enumerate(entries):
            flags = self.MF_STRING | (self.MF_CHECKED if active else 0)
            _user32.AppendMenuW(hmenu, flags, 1000 + i, label)

        if not entries:
            _user32.AppendMenuW(hmenu, self.MF_STRING | self.MF_GRAYED, 1099, "（读不到输入源）")

        _user32.AppendMenuW(hmenu, self.MF_SEPARATOR, 0, None)
        _user32.AppendMenuW(hmenu, self.MF_STRING, self.ID_OPEN, "打开控制窗口")
        _user32.AppendMenuW(hmenu, self.MF_STRING, self.ID_ADDR, "复制手机地址")
        _user32.AppendMenuW(hmenu, self.MF_SEPARATOR, 0, None)
        _user32.AppendMenuW(hmenu, self.MF_STRING, self.ID_QUIT, "退出")

        pt = _POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        _user32.SetForegroundWindow(self.hwnd)
        cmd = _user32.TrackPopupMenu(
            hmenu, self.TPM_RETURNCMD | self.TPM_RIGHTBUTTON | self.TPM_NONOTIFY,
            pt.x, pt.y, 0, self.hwnd, None)
        _user32.PostMessageW(self.hwnd, 0, 0, 0)
        _user32.DestroyMenu(hmenu)

        if 1000 <= cmd < 1099:
            idx = cmd - 1000
            if 0 <= idx < len(self._menu_values):
                switch_from_tray(self._menu_values[idx])
        elif cmd == self.ID_OPEN:
            show_window()
        elif cmd == self.ID_ADDR:
            copy_lan_url()
        elif cmd == self.ID_QUIT:
            stop_app()

    def stop(self):
        if self.hwnd:
            _user32.PostMessageW(self.hwnd, self.WM_CLOSE, 0, 0)


def menu_entries():
    """托盘菜单用的轻量快照，绝不在这里碰硬件。
    没有缓存就返回空，宁可显示「读不到」，也不要列出并不存在的接口。"""
    st = _STATE_CACHE.get("data")
    if st and st.get("ok"):
        return [(i["value"], i["label"], i["active"]) for i in st["inputs"]
                if not i["hidden"]]
    return []


def switch_from_tray(value):
    def work():
        r = switch_to(value)
        if not r.get("ok"):
            _user32.MessageBoxW(None, "切换失败：\n\n%s" % r.get("error", ""), APP_NAME, 0x30)
        UI_QUEUE.put(_refresh_if_visible)

    threading.Thread(target=work, daemon=True).start()


def _refresh_if_visible():
    try:
        if UI and UI.is_visible():
            UI.refresh()
    except Exception:
        pass


def copy_lan_url():
    def work():
        try:
            if UI:
                UI.root.clipboard_clear()
                UI.root.clipboard_append(LAN_URL)
                _user32.MessageBoxW(None, "已复制到剪贴板：\n\n%s" % LAN_URL, APP_NAME, 0x40)
        except Exception:
            _user32.MessageBoxW(None, "手机浏览器打开：\n\n%s" % LAN_URL, APP_NAME, 0x40)
    UI_QUEUE.put(work)


def show_window(page=None):
    UI_QUEUE.put(lambda: UI and UI.show(page))


def open_url(url):
    try:
        os.startfile(url)
    except Exception:
        log.exception("打开链接失败")

# ---------------------------------------------------------------- HTTP 服务

class Handler(BaseHTTPRequestHandler):
    server_version = APP_NAME + "/" + APP_VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def handle_one_request(self):
        """浏览器/curl 关连接时别刷一整段 traceback。"""
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 65536:
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return None

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        try:
            if path in ("/", "/index.html"):
                return self._serve_ui()
            if path == "/api/state":
                return self._json(build_state())
            if path == "/go":
                q = parse_qs(u.query)
                raw = (q.get("v") or [""])[0]
                try:
                    val = int(raw, 0)
                except ValueError:
                    return self._redirect("/?msg=%s&err=1" % quote("参数 v 不合法"))
                r = switch_to(val)
                msg = r.get("message") or r.get("error") or "已切换"
                return self._redirect("/?msg=%s&err=%d" % (quote(msg), 0 if r.get("ok") else 1))
            if path == "/icon.png":
                return self._send(200, png_bytes(192), "image/png")
            if path == "/manifest.json":
                return self._json({
                    "name": "XingSwitch", "short_name": "XingSwitch",
                    "start_url": "/", "display": "standalone",
                    "background_color": "#0b0d10", "theme_color": "#0b0d10",
                    "icons": [{"src": "/icon.png", "sizes": "192x192", "type": "image/png"}],
                })
            if path == "/favicon.ico":
                return self._send(200, png_bytes(32), "image/png")
            return self._send(404, "not found", "text/plain; charset=utf-8")
        except Exception:
            log.exception("GET %s 出错", path)
            try:
                return self._json({"ok": False, "error": "服务器内部错误"}, 500)
            except Exception:
                pass

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._read_json()
            if path == "/api/switch":
                if not isinstance(body, dict) or "value" not in body:
                    return self._json({"ok": False, "error": "缺少 value"}, 400)
                try:
                    val = int(body["value"])
                except (TypeError, ValueError):
                    return self._json({"ok": False, "error": "value 不合法"}, 400)
                return self._json(switch_to(val))

            if path == "/api/config":
                if not isinstance(body, dict):
                    return self._json({"ok": False, "error": "请求体不合法"}, 400)
                with _CFG_LOCK:
                    if isinstance(body.get("labels"), dict):
                        clean = {}
                        for k, v in body["labels"].items():
                            try:
                                ik = int(k)
                            except (TypeError, ValueError):
                                continue
                            if isinstance(v, str) and v.strip():
                                clean[str(ik)] = v.strip()[:16]
                        CONFIG["labels"].update(clean)
                    if isinstance(body.get("hidden"), list):
                        hid = []
                        for v in body["hidden"]:
                            try:
                                hid.append(int(v))
                            except (TypeError, ValueError):
                                pass
                        CONFIG["hidden"] = hid
                    if isinstance(body.get("monitor"), str):
                        CONFIG["monitor"] = body["monitor"]
                save_config()
                log.info("配置已保存")
                return self._json({"ok": True})

            if path in ("/api/reset", "/api/retry"):
                reset_breaker()
                _STATE_CACHE["data"] = None
                return self._json({"ok": True, "state": build_state(force=True)})

            if path == "/api/quit" and self.client_address[0] in ("127.0.0.1", "::1"):
                self._json({"ok": True})
                threading.Thread(target=stop_app, daemon=True).start()
                return

            return self._json({"ok": False, "error": "未知接口"}, 404)
        except Exception:
            log.exception("POST %s 出错", path)
            try:
                return self._json({"ok": False, "error": "服务器内部错误"}, 500)
            except Exception:
                pass

    def _serve_ui(self):
        data = None
        for p in (UI_PATH, UI_PATH_BUNDLED):
            try:
                with open(p, "rb") as f:
                    data = f.read()
                break
            except OSError:
                continue
        if data is None:
            data = b"<h1>ui.html missing</h1>"
        self._send(200, data, "text/html; charset=utf-8")

# ---------------------------------------------------------------- 主程序

STOP = threading.Event()
START_TIME = time.time()
TRAY = None
UI = None
UI_QUEUE = queue.Queue()
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001

BG = "#0b0d10"
CARD = "#151b23"
CARD_ON = "#10261a"
TEXT = "#e6edf3"
MUTED = "#8b949e"
ACCENT = "#3fb950"
LINE = "#242c36"
DARK = "#111418"
FONT = "Microsoft YaHei UI"


def stop_app():
    STOP.set()
    UI_QUEUE.put(_quit_ui)


def _quit_ui():
    try:
        if UI:
            UI.root.quit()
    except Exception:
        pass


def keep_awake(enable):
    try:
        flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if enable else 0)
        ctypes.WinDLL("kernel32").SetThreadExecutionState(flags)
        log.info("防休眠：%s", "已开启" if enable else "已关闭")
    except Exception:
        log.exception("设置防休眠失败")


def setup_logging(console):
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if console and sys.stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)


def single_instance():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW(None, False, "Global\\XingSwitch_SingleInstance")
    return ctypes.get_last_error() != 183


def _refresh_ip():
    global LAN_URL
    while not STOP.wait(60):
        url = "http://%s:%d/" % (local_ip(), CONFIG["port"])
        if url != LAN_URL:
            LAN_URL = url
            log.info("地址变化，手机请改用：%s", url)


def _used_text(ts):
    if not ts:
        return "从未用过"
    days = int((time.time() - ts) // 86400)
    if days <= 0:
        return "今天用过"
    if days == 1:
        return "昨天用过"
    return "%d 天前用过" % days


# ---------------------------------------------------------------- 桌面窗口

class DesktopUI:
    def __init__(self, root):
        self.root = root
        self.root.withdraw()
        self.root.title(APP_NAME)
        self.root.configure(bg=BG)
        self.root.geometry("460x600")
        self.root.minsize(440, 300)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)

        try:
            if os.path.exists(ICON_PATH):
                self.root.iconbitmap(ICON_PATH)
        except Exception:
            pass

        self.st = None
        self.page = "main"
        self.busy = False
        self.loading = False
        self.drafts = {}
        self.hidden_set = set()
        self._built = False

        self.root.after(120, self._pump)

    # ---------- 骨架
    def _build(self):
        if self._built:
            return
        self._built = True

        head = tk.Frame(self.root, bg=DARK, height=92)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text=APP_NAME, bg=DARK, fg=TEXT,
                 font=(FONT, 16, "bold")).place(x=22, y=16)
        tk.Label(head, text="显示器输入源切换", bg=DARK, fg=MUTED,
                 font=(FONT, 9)).place(x=23, y=46)
        self.lbl_cur = tk.Label(head, text="", bg=DARK, fg=ACCENT,
                                font=(FONT, 15, "bold"), anchor="e")
        self.lbl_cur.place(relx=1.0, x=-22, y=18, anchor="ne")
        self.lbl_raw = tk.Label(head, text="", bg=DARK, fg=MUTED,
                                font=(FONT, 8), anchor="e")
        self.lbl_raw.place(relx=1.0, x=-22, y=50, anchor="ne")

        self.body = tk.Frame(self.root, bg=BG)
        self.body.pack(fill="both", expand=True, padx=14, pady=12)

        self.status = tk.Label(self.root, text="", bg=BG, fg=MUTED,
                               font=(FONT, 9), anchor="w")
        self.status.pack(fill="x", padx=16, pady=(0, 4))

        foot = tk.Frame(self.root, bg=BG)
        foot.pack(fill="x", padx=14, pady=(0, 12))

        self.btn_manage = tk.Button(foot, text="管理输入源", command=self.toggle_page,
                                    font=(FONT, 9), bg="#1b222b", fg=TEXT,
                                    activebackground="#232c37", activeforeground=TEXT,
                                    relief="flat", bd=0, cursor="hand2", padx=12, pady=6)
        self.btn_manage.pack(side="left")

        self.btn_link = tk.Button(foot, text="GitHub", command=lambda: open_url(project_url()),
                                  font=(FONT, 9), bg=BG, fg=MUTED,
                                  activebackground=BG, activeforeground=ACCENT,
                                  relief="flat", bd=0, cursor="hand2")
        self.btn_link.pack(side="right")

        self.lbl_url = tk.Label(foot, text=LAN_URL, bg=BG, fg=MUTED, font=(FONT, 8))
        self.lbl_url.pack(side="right", padx=10)

    def _clear_body(self):
        for w in self.body.winfo_children():
            w.destroy()

    # ---------- 页面
    def _render(self):
        self._build()
        st = self.st or {}
        ok = bool(st.get("ok"))
        if ok:
            self.lbl_cur.configure(text=st["currentLabel"])
            self.lbl_raw.configure(text=st["currentRaw"])
        else:
            self.lbl_cur.configure(text="不可用")
            self.lbl_raw.configure(text="")
        self.btn_manage.configure(text="返回" if self.page != "main" else "管理输入源")
        self._clear_body()
        if self.page == "main":
            self._render_main(st, ok)
        else:
            self._render_manager(st)

        # 高度跟着内容走，别留一大片空白
        try:
            self.root.update_idletasks()
            self.root.geometry("460x%d" % max(330, self.root.winfo_reqheight()))
        except Exception:
            pass

    def _render_main(self, st, ok):
        if not ok:
            box = tk.Frame(self.body, bg="#241417", highlightbackground="#5a2b2e",
                           highlightthickness=1)
            box.pack(fill="x")
            tk.Label(box, text=st.get("error", "读不到状态"), bg="#241417", fg="#ffb3b3",
                     font=(FONT, 9), wraplength=400, justify="left",
                     anchor="w").pack(fill="x", padx=14, pady=12)
            tk.Button(box, text="重新检测", command=self.retry, font=(FONT, 9),
                      bg="#33201f", fg="#ffd0d0", relief="flat", bd=0,
                      cursor="hand2", padx=12, pady=4).pack(anchor="w", padx=14, pady=(0, 12))
            return

        grid = tk.Frame(self.body, bg=BG)
        grid.pack(fill="both", expand=True)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        items = [i for i in st["inputs"] if not i["hidden"]]
        for n, it in enumerate(items):
            card = self._card(grid, it)
            card.grid(row=n // 2, column=n % 2, sticky="nsew", padx=6, pady=6)

        if not items:
            tk.Label(self.body, text="所有输入源都被隐藏了，点「管理输入源」放出来。",
                     bg=BG, fg=MUTED, font=(FONT, 9)).pack(pady=20)

    def _card(self, parent, it):
        active = it["active"]
        bg = CARD_ON if active else CARD
        bd = ACCENT if active else LINE
        f = tk.Frame(parent, bg=bg, highlightbackground=bd, highlightthickness=1,
                     cursor="hand2")
        lab = tk.Label(f, text=it["label"], bg=bg, fg=ACCENT if active else TEXT,
                       font=(FONT, 12, "bold"), anchor="w", justify="left")
        lab.pack(fill="x", padx=13, pady=(11, 0))
        sub = tk.Label(f, text=("当前 · " if active else "") + it["raw"], bg=bg,
                       fg=MUTED, font=(FONT, 8), anchor="w")
        sub.pack(fill="x", padx=13, pady=(2, 11))
        for w in (f, lab, sub):
            w.bind("<Button-1>", lambda e, v=it["value"]: self.switch(v))
        return f

    def _render_manager(self, st):
        tk.Label(self.body, text="改名字、决定哪些口显示在主界面。\n"
                                 "灰色小字是显示器上的原始接口名，改不掉也不会丢。",
                 bg=BG, fg=MUTED, font=(FONT, 9), justify="left", anchor="w",
                 wraplength=410).pack(fill="x", pady=(0, 8))

        for it in st.get("inputs", []):
            row = tk.Frame(self.body, bg=BG)
            row.pack(fill="x", pady=3)

            tk.Label(row, text=it["raw"], width=13, anchor="w", bg=BG, fg=MUTED,
                     font=(FONT, 9)).pack(side="left")

            var = self.drafts.get(it["value"])
            if var is None:
                var = tk.StringVar(value=it["label"])
                self.drafts[it["value"]] = var
            tk.Entry(row, textvariable=var, font=(FONT, 10), width=12, bg="#0f141a",
                     fg=TEXT, insertbackground=TEXT, relief="solid", bd=1).pack(
                side="left", ipady=3)

            vis = tk.BooleanVar(value=it["value"] not in self.hidden_set)
            tk.Checkbutton(row, text="显示", variable=vis, bg=BG, fg=TEXT,
                           selectcolor=CARD, activebackground=BG, activeforeground=TEXT,
                           font=(FONT, 9), bd=0, highlightthickness=0, cursor="hand2",
                           command=lambda v=it["value"], var=vis: self._toggle(v, var)
                           ).pack(side="left", padx=(8, 0))

            tk.Label(row, text=_used_text(it.get("usedAt")), bg=BG, fg=MUTED,
                     font=(FONT, 8)).pack(side="left", padx=8)

        btns = tk.Frame(self.body, bg=BG)
        btns.pack(fill="x", pady=14)
        tk.Button(btns, text="保存", command=self.save_manager, font=(FONT, 10, "bold"),
                  bg=ACCENT, fg="#06110c", activebackground="#34a144",
                  relief="flat", bd=0, cursor="hand2", padx=18, pady=5).pack(side="left")
        tk.Button(btns, text="取消", command=self.toggle_page, font=(FONT, 9),
                  bg="#1b222b", fg=TEXT, relief="flat", bd=0, cursor="hand2",
                  padx=14, pady=5).pack(side="left", padx=8)
        tk.Button(btns, text="隐藏没用过的", command=self.hide_unused, font=(FONT, 9),
                  bg=BG, fg=MUTED, activebackground=BG, activeforeground=TEXT,
                  relief="flat", bd=0, cursor="hand2").pack(side="right")

    # ---------- 动作
    def _toggle(self, value, var):
        if var.get():
            self.hidden_set.discard(value)
        else:
            self.hidden_set.add(value)

    def save_manager(self):
        labels = {}
        for v, var in self.drafts.items():
            t = var.get().strip()
            if t:
                labels[str(v)] = t[:16]
        with _CFG_LOCK:
            CONFIG["labels"].update(labels)
            CONFIG["hidden"] = sorted(self.hidden_set)
        save_config()
        _STATE_CACHE["data"] = None
        self.page = "main"
        self.set_status("已保存")
        self.refresh()

    def hide_unused(self):
        st = self.st or {}
        n = 0
        for it in st.get("inputs", []):
            if not it.get("usedAt") and not it["active"]:
                if it["value"] not in self.hidden_set:
                    self.hidden_set.add(it["value"])
                    n += 1
        self.set_status("已勾掉 %d 个没用过的口，点保存生效" % n)
        self._render_manager(st)

    def toggle_page(self):
        if self.page == "main":
            self.page = "manage"
            st = self.st or {}
            self.drafts = {}
            self.hidden_set = set(i["value"] for i in st.get("inputs", []) if i["hidden"])
        else:
            self.page = "main"
        self._render()

    def switch(self, value):
        if self.busy:
            return
        self.busy = True
        self.set_status("切换中…")

        def work():
            r = switch_to(value)
            UI_QUEUE.put(lambda: self._after_switch(r))

        threading.Thread(target=work, daemon=True).start()

    def _after_switch(self, r):
        self.busy = False
        self.set_status(r.get("message") or r.get("error") or "")
        self.refresh()

    def retry(self):
        reset_breaker()
        _STATE_CACHE["data"] = None
        self.set_status("正在重新检测…")
        self.refresh()

    def refresh(self):
        if self.loading:
            return
        self.loading = True

        def work():
            st = build_state(force=True)
            UI_QUEUE.put(lambda: self._on_state(st))

        threading.Thread(target=work, daemon=True).start()

    def _on_state(self, st):
        self.loading = False
        self.st = st
        self._render()

    def set_status(self, text):
        try:
            self.status.configure(text=text or "")
        except Exception:
            pass

    def _pump(self):
        try:
            while True:
                fn = UI_QUEUE.get_nowait()
                try:
                    fn()
                except Exception:
                    log.exception("界面回调出错")
        except queue.Empty:
            pass
        try:
            self.root.after(120, self._pump)
        except Exception:
            pass

    def show(self, page=None):
        if page:
            self.page = page
        self._build()
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.attributes("-topmost", True)
            self.root.after(400, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass
        self.refresh()

    def hide(self):
        self.root.withdraw()

    def is_visible(self):
        try:
            return self.root.state() != "withdrawn"
        except Exception:
            return False


def main():
    global LAN_URL, TRAY, UI
    console = "--console" in sys.argv or (sys.stderr is not None and not getattr(sys, "frozen", False))
    setup_logging(console)

    cfg = load_config()
    LAN_URL = "http://%s:%d/" % (local_ip(), cfg["port"])

    if not os.path.exists(ICON_PATH):
        try:
            write_icon_file(ICON_PATH)
        except Exception:
            log.exception("生成 icon.ico 失败")

    if not single_instance():
        log.warning("已经有一个实例在跑了，本次退出")
        if console:
            print("已经有一个实例在跑了。")
        return

    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", cfg["port"]), Handler)
        httpd.daemon_threads = True
    except OSError as e:
        log.error("端口 %d 绑定失败：%s", cfg["port"], e)
        if console:
            print("端口 %d 被占用：%s" % (cfg["port"], e))
        return

    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    threading.Thread(target=_refresh_ip, name="ip", daemon=True).start()
    threading.Thread(target=lambda: build_state(force=True), name="warm", daemon=True).start()

    log.info("=" * 56)
    log.info("%s %s 启动", APP_NAME, APP_VERSION)
    log.info("手机访问： %s", LAN_URL)

    if cfg.get("preventSleep", True):
        keep_awake(True)

    root = tk.Tk()
    UI = DesktopUI(root)

    if "--open" in sys.argv:
        root.after(400, lambda: UI.show())

    if cfg.get("tray", True):
        TRAY = TrayIcon("%s · %s" % (APP_NAME, LAN_URL))
        if not TRAY.start():
            log.warning("托盘没起来，改为直接显示主窗口")
            root.after(400, lambda: UI.show())
    else:
        root.after(400, lambda: UI.show())

    if console:
        print("%s 已启动：%s" % (APP_NAME, LAN_URL))
        print("托盘左键开窗口，右键选输入源。")

    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("正在退出…")
        if TRAY:
            try:
                TRAY.stop()
            except Exception:
                pass
        try:
            httpd.shutdown()
        except Exception:
            pass
        if cfg.get("preventSleep", True):
            keep_awake(False)


if __name__ == "__main__":
    main()
