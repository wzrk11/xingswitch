# -*- coding: utf-8 -*-
"""
显示器 DDC/CI 探针
------------------
用途：在写正式程序前，先确认这台电脑 + 这台显示器能不能通过 DDC/CI 读/写输入源，
     以及最关键的：切走之后还能不能切回来。

用法：
    python probe.py                 只读，列出所有显示器、当前输入源、支持的输入源
    python probe.py --switch 0x12   切到指定输入源（不会自动切回，谨慎）
    python probe.py --test 0x12     危险测试：切过去等 15 秒，再自动切回原输入源
"""
import ctypes
import sys
import time
from ctypes import wintypes

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

dxva2 = ctypes.WinDLL("dxva2.dll")
user32 = ctypes.WinDLL("user32.dll")

MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
    ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)


class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [
        ("hPhysicalMonitor", wintypes.HANDLE),
        ("szPhysicalMonitorDescription", wintypes.WCHAR * 128),
    ]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [wintypes.HMONITOR, ctypes.POINTER(wintypes.DWORD)]
dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL
dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [wintypes.HMONITOR, wintypes.DWORD, ctypes.POINTER(PHYSICAL_MONITOR)]
dxva2.GetPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL
dxva2.DestroyPhysicalMonitors.argtypes = [wintypes.DWORD, ctypes.POINTER(PHYSICAL_MONITOR)]
dxva2.DestroyPhysicalMonitors.restype = wintypes.BOOL
dxva2.GetVCPFeatureAndVCPFeatureReply.argtypes = [
    wintypes.HANDLE, ctypes.c_ubyte, ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
dxva2.GetVCPFeatureAndVCPFeatureReply.restype = wintypes.BOOL
dxva2.SetVCPFeature.argtypes = [wintypes.HANDLE, ctypes.c_ubyte, wintypes.DWORD]
dxva2.SetVCPFeature.restype = wintypes.BOOL
dxva2.GetCapabilitiesStringLength.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
dxva2.GetCapabilitiesStringLength.restype = wintypes.BOOL
dxva2.CapabilitiesRequestAndCapabilitiesReply.argtypes = [wintypes.HANDLE, ctypes.c_char_p, wintypes.DWORD]
dxva2.CapabilitiesRequestAndCapabilitiesReply.restype = wintypes.BOOL

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


def label(value):
    return INPUT_NAMES.get(value, f"未知(0x{value:02X})")


def collect():
    """每次调用都重新枚举，不复用句柄 —— 显示器输入源变了以后句柄会失效。"""
    handles = []

    def cb(hmon, hdc, lprect, lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        user32.GetMonitorInfoW(hmon, ctypes.byref(info))
        count = wintypes.DWORD(0)
        if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, ctypes.byref(count)):
            return True
        if count.value == 0:
            return True
        arr = (PHYSICAL_MONITOR * count.value)()
        if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, count.value, arr):
            return True
        for i in range(count.value):
            pm = arr[i]
            handles.append({
                "h": pm.hPhysicalMonitor,
                "desc": pm.szPhysicalMonitorDescription,
                "device": info.szDevice,
                "rect": (info.rcMonitor.left, info.rcMonitor.top,
                         info.rcMonitor.right, info.rcMonitor.bottom),
                "arr": arr,
                "idx": i,
                "count": count.value,
            })
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
    return handles


def read_vcp(h, code):
    cur = wintypes.DWORD(0)
    mx = wintypes.DWORD(0)
    t = wintypes.DWORD(0)
    ok = dxva2.GetVCPFeatureAndVCPFeatureReply(h, code, ctypes.byref(t), ctypes.byref(cur), ctypes.byref(mx))
    return (cur.value, mx.value) if ok else None


def set_vcp(h, code, value):
    return bool(dxva2.SetVCPFeature(h, code, value))


def caps(h):
    ln = wintypes.DWORD(0)
    if not dxva2.GetCapabilitiesStringLength(h, ctypes.byref(ln)) or ln.value == 0:
        return None
    buf = ctypes.create_string_buffer(ln.value + 1)
    if not dxva2.CapabilitiesRequestAndCapabilitiesReply(h, buf, ln.value):
        return None
    try:
        return buf.value.decode("ascii", "ignore")
    except Exception:
        return None


def supported_inputs(cap_str):
    """从 capabilities 里抠出 60(...) 这一段，拿到这台显示器真正支持的输入源值。"""
    if not cap_str:
        return None
    i = cap_str.find("60(")
    if i < 0:
        return None
    j = cap_str.find(")", i)
    if j < 0:
        return None
    body = cap_str[i + 3:j]
    out = []
    token = ""
    for ch in body:
        if ch in " ":
            if token:
                out.append(token)
                token = ""
        elif ch == ")" or ch == "(":
            continue
        else:
            token += ch
    if token:
        out.append(token)
    vals = []
    for t in out:
        try:
            vals.append(int(t, 16) if not t.lower().startswith("0x") else int(t, 16))
        except ValueError:
            pass
    return vals or None


def main():
    args = sys.argv[1:]
    mode = "list"
    target = None
    if "--switch" in args:
        mode = "switch"
        target = int(args[args.index("--switch") + 1], 0)
    elif "--test" in args:
        mode = "test"
        target = int(args[args.index("--test") + 1], 0)

    mons = collect()
    if not mons:
        print("没有枚举到任何物理显示器（可能全是虚拟显示器 / 远程桌面会话）。")
        return 1

    print(f"发现 {len(mons)} 个物理显示器\n" + "=" * 66)
    for n, m in enumerate(mons, 1):
        r = m["rect"]
        print(f"[{n}] {m['desc']}   ({m['device']})  {r[2]-r[0]}x{r[3]-r[1]}")
        cur = read_vcp(m["h"], VCP_INPUT)
        if cur is None:
            print("    输入源读取失败 —— 该显示器不响应 DDC/CI（OSD 里没开 DDC/CI，或线材/转接器不支持）")
        else:
            print(f"    当前输入源：{label(cur[0])}  (0x{cur[0]:02X})")
        sup = supported_inputs(caps(m["h"]))
        if sup:
            print("    支持输入源：" + "、".join(f"{label(v)}(0x{v:02X})" for v in sup))
        else:
            print("    支持输入源：读不到 capabilities，只能逐个试")
        print()

    if mode == "list":
        print("=" * 66)
        print("只读探测完成。下一步建议先跑 --test，验证「切走还能不能切回来」。")
        return 0

    m = mons[0]
    if len(mons) > 1:
        print("!! 有多个显示器，本测试默认操作第 1 个。")

    def find(device):
        """重新枚举并找到同一个显示器，返回句柄；找不到返回 None。"""
        for mm in collect():
            if mm["device"] == device:
                return mm["h"], mm["desc"]
        return None, None

    old = read_vcp(m["h"], VCP_INPUT)
    if old is None:
        print("读不到当前输入源，先解决 DDC/CI 不通的问题。")
        return 2

    print(f"准备把 [{m['desc']}] 从 {label(old[0])} 切到 {label(target)} (0x{target:02X})")
    print("!! 如果切过去以后切不回来，请用显示器实体按键切回。")
    print(">> 发送切换命令")
    ok = set_vcp(m["h"], VCP_INPUT, target)
    print("   写入返回值：" + ("成功" if ok else "失败"))
    if mode == "switch":
        return 0

    time.sleep(3)
    h2, desc2 = find(m["device"])
    if h2 is None:
        print(">> 切走 3 秒后：Windows 已经枚举不到这个显示器（HPD 掉了）")
        print("   —— 仍然尝试原路径切回，看看有没有奇迹")
        still = set_vcp(m["h"], VCP_INPUT, old[0])
        print("   用旧句柄切回：" + ("返回成功" if still else "返回失败"))
        print("=> 结论：链路被断开，纯软件方案在你这台机器上不成立。")
        return 3
    after = read_vcp(h2, VCP_INPUT)
    print(f">> 切走 3 秒后：显示器仍在，当前输入源 = "
          + (label(after[0]) + f" (0x{after[0]:02X})" if after else "读不到"))
    if after and after[0] != target:
        print("   注意：写入没生效，显示器没真的换过去（有些显示器要先在 OSD 里打开 DDC/CI 控制）")

    print(">> 倒数 15 秒，模拟你人在手机端操作前的时间差……")
    for s in range(15, 0, -1):
        sys.stdout.write(f"\r   {s:2d} ")
        sys.stdout.flush()
        time.sleep(1)
    print("\r>> 现在尝试切回原来的输入源")

    h3, _ = find(m["device"])
    if h3 is None:
        print("   重新枚举失败：显示器不在系统里了，没法切回。")
        print("=> 结论：切走后 DDC 通道已失效，纯软件方案不成立。")
        return 3
    wrote = set_vcp(h3, VCP_INPUT, old[0])
    print("   切回命令：" + ("已发送" if wrote else "发送失败"))
    time.sleep(3)
    h4, _ = find(m["device"])
    verify = read_vcp(h4, VCP_INPUT) if h4 else None
    if verify and verify[0] == old[0]:
        print(f"   回读确认：当前输入源 = {label(verify[0])}，切换成功")
        print("=> 结论：DDC/CI 可以跨输入源工作，纯软件方案可行。")
        return 0
    print("   回读确认：" + (f"当前仍是 {label(verify[0])}" if verify else "读不到"))
    print("=> 结论：命令发出去了但显示器没照着做，需要换方案或改写入方式。")
    return 4


if __name__ == "__main__":
    sys.exit(main())
