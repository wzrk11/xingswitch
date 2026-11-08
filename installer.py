# -*- coding: utf-8 -*-
"""
XingSwitch 安装程序
-------------------
编译成单文件 exe（带 uac-admin），双击即安装：
    python -m PyInstaller --onefile --windowed --uac-admin ...

命令行参数：
    （无）            显示安装向导
    --uninstall      显示卸载确认
    --silent         静默安装（默认选项），用于测试
    --silent --uninstall  静默卸载
"""
import base64
import ctypes
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog
import winreg

APP_NAME = "XingSwitch"
APP_TITLE = "XingSwitch 显示器输入源切换"
APP_VERSION = "1.1.0"
APP_EXE = "XingSwitch.exe"
APP_ID = "XingSwitch"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
UNINST_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\%s" % APP_ID

FROZEN = getattr(sys, "frozen", False)
RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else os.path.dirname(os.path.abspath(__file__))

DEFAULT_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                           "Programs", APP_ID)
START_MENU = os.path.join(os.environ.get("APPDATA", ""),
                          r"Microsoft\Windows\Start Menu\Programs")
DESKTOP = os.path.join(os.path.expanduser("~"), "Desktop")


# ---------------------------------------------------------------- 小工具

def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=0x08000000)
        return p.returncode, (p.stdout or b"").decode("mbcs", "ignore") + \
            (p.stderr or b"").decode("mbcs", "ignore")
    except Exception as e:
        return -1, str(e)


def powershell(script):
    """用 -EncodedCommand 传脚本，彻底绕开引号与中文编码问题。"""
    enc = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return run(["powershell", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc])


def make_shortcut(lnk_path, target, workdir, icon, desc="", args=""):
    q = lambda s: "'" + str(s).replace("'", "''") + "'"
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        "$s = $ws.CreateShortcut(%s); "
        "$s.TargetPath = %s; "
        "$s.WorkingDirectory = %s; "
        "$s.Arguments = %s; "
        "$s.IconLocation = %s; "
        "$s.Description = %s; "
        "$s.Save()"
    ) % (q(lnk_path), q(target), q(workdir), q(args),
         q("%s,0" % icon if icon else "%s,0" % target), q(desc))
    code, out = powershell(ps)
    return code == 0 and os.path.exists(lnk_path)


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.4)
        s.connect(("223.5.5.5", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def payload(name):
    p = os.path.join(RES_DIR, name)
    if os.path.exists(p):
        return p
    alt = os.path.join(HERE, name)
    if os.path.exists(alt):
        return alt
    alt2 = os.path.join(HERE, "dist", name)
    if os.path.exists(alt2):
        return alt2
    return None


def kill_running():
    run(["taskkill", "/F", "/IM", APP_EXE])


def port_of(target):
    import json
    cfg = os.path.join(target, "config.json")
    try:
        with open(cfg, "r", encoding="utf-8") as f:
            return int(json.load(f).get("port") or 8765)
    except Exception:
        return 8765


# ---------------------------------------------------------------- 安装 / 卸载

def do_install(target, desktop, autostart, firewall, log):
    log("检查运行环境…")
    if not is_admin():
        log("  注意：当前没有管理员权限，防火墙规则会跳过")

    exe_src = payload(APP_EXE)
    if not exe_src:
        raise RuntimeError("安装包损坏：找不到 %s" % APP_EXE)

    log("结束正在运行的旧版本…")
    kill_running()
    time.sleep(0.4)

    log("复制文件到 %s" % target)
    os.makedirs(target, exist_ok=True)
    exe_dst = os.path.join(target, APP_EXE)
    shutil.copy2(exe_src, exe_dst)

    for extra in ("ui.html", "icon.ico"):
        src = payload(extra)
        if src:
            try:
                shutil.copy2(src, os.path.join(target, extra))
            except Exception:
                pass

    log("创建开始菜单快捷方式…")
    start_dir = os.path.join(START_MENU, APP_ID)
    os.makedirs(start_dir, exist_ok=True)
    icon = os.path.join(target, "icon.ico")
    if not os.path.exists(icon):
        icon = exe_dst

    log("生成卸载程序…")
    uninst = os.path.join(target, "Uninstall.exe")
    try:
        if FROZEN:
            shutil.copy2(sys.executable, uninst)
        else:
            uninst = os.path.join(target, "uninstall.cmd")
            with open(uninst, "w", encoding="utf-8") as f:
                f.write('@echo off\r\n"%s" "%s" --uninstall\r\n'
                        % (sys.executable, os.path.abspath(__file__)))
    except Exception as e:
        log("  失败（不影响使用）：%s" % e)

    make_shortcut(os.path.join(start_dir, "XingSwitch.lnk"), exe_dst, target, icon,
                  "显示器输入源切换")
    make_shortcut(os.path.join(start_dir, "卸载 XingSwitch.lnk"), uninst, target, icon,
                  "卸载 XingSwitch")
    if desktop:
        log("创建桌面快捷方式…")
        make_shortcut(os.path.join(DESKTOP, "XingSwitch.lnk"), exe_dst, target, icon,
                      "显示器输入源切换")

    log("登记卸载信息…")
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINST_KEY) as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, APP_TITLE)
            winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
            winreg.SetValueEx(k, "Publisher", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ, target)
            winreg.SetValueEx(k, "DisplayIcon", 0, winreg.REG_SZ, icon)
            winreg.SetValueEx(k, "UninstallString", 0, winreg.REG_SZ,
                              '"%s" --uninstall' % uninst)
            winreg.SetValueEx(k, "QuietUninstallString", 0, winreg.REG_SZ,
                              '"%s" --uninstall --silent' % uninst)
            winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD, 24 * 1024)
    except Exception as e:
        log("  登记失败（不影响使用）：%s" % e)

    if autostart:
        log("设置开机自动启动…")
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, APP_ID, 0, winreg.REG_SZ, '"%s"' % exe_dst)
        except Exception as e:
            log("  设置失败：%s" % e)

    if firewall:
        if is_admin():
            log("放行防火墙（仅家庭/专用网络）…")
            run(["netsh", "advfirewall", "firewall", "delete", "rule", "name=XingSwitch"])
            code, out = run(["netsh", "advfirewall", "firewall", "add", "rule",
                             "name=XingSwitch", "dir=in", "action=allow",
                             "program=%s" % exe_dst, "enable=yes",
                             "profile=private,domain"])
            if code != 0:
                log("  失败：%s" % out.strip()[:120])
        else:
            log("  跳过：没有管理员权限")

    log("启动程序…")
    try:
        subprocess.Popen([exe_dst], cwd=target,
                         creationflags=0x00000008 | 0x00000200)
    except Exception as e:
        log("  启动失败：%s" % e)

    log("完成。")
    return exe_dst


def do_uninstall(log):
    log("结束正在运行的程序…")
    kill_running()
    time.sleep(0.5)

    target = None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINST_KEY) as k:
            target = winreg.QueryValueEx(k, "InstallLocation")[0]
    except Exception:
        pass
    if not target:
        target = DEFAULT_DIR

    log("移除开机启动项…")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP_ID)
    except Exception:
        pass

    log("移除防火墙规则…")
    if is_admin():
        run(["netsh", "advfirewall", "firewall", "delete", "rule", "name=XingSwitch"])
    else:
        log("  跳过：没有管理员权限")

    log("删除快捷方式…")
    for p in (os.path.join(DESKTOP, "XingSwitch.lnk"),
              os.path.join(START_MENU, APP_ID, "XingSwitch.lnk"),
              os.path.join(START_MENU, APP_ID, "卸载 XingSwitch.lnk")):
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
    try:
        os.rmdir(os.path.join(START_MENU, APP_ID))
    except Exception:
        pass

    log("清理注册表…")
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINST_KEY)
    except Exception:
        pass

    log("删除程序文件…")
    try:
        for name in os.listdir(target):
            p = os.path.join(target, name)
            try:
                os.remove(p)
            except Exception:
                pass
    except Exception:
        pass

    # 卸载器自己就在被删目录里，只能交给后台命令延后删除
    log("收尾…")
    try:
        subprocess.Popen(
            'cmd /c ping -n 3 127.0.0.1 >nul & rmdir /s /q "%s"' % target,
            shell=True, creationflags=0x00000008)
    except Exception:
        pass
    log("已卸载。")


# ---------------------------------------------------------------- GUI

BG = "#ffffff"
INK = "#16181d"
MUTED = "#6b7280"
LINE = "#e5e7eb"
ACCENT = "#12b981"
DARK = "#111418"


class App:
    def __init__(self, mode):
        self.mode = mode
        self.q = queue.Queue()
        self.root = tk.Tk()
        self.root.title("XingSwitch 安装程序" if mode == "install" else "卸载 XingSwitch")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        w, h = 520, 520
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry("%dx%d+%d+%d" % (w, h, (sw - w) // 2, (sh - h) // 3))
        try:
            ico = payload("icon.ico")
            if ico:
                self.root.iconbitmap(ico)
        except Exception:
            pass

        self.target = tk.StringVar(value=DEFAULT_DIR)
        self.desktop = tk.BooleanVar(value=True)
        self.autostart = tk.BooleanVar(value=True)
        self.firewall = tk.BooleanVar(value=True)
        self.busy = False
        self.done = False

        self._build()
        self.root.after(120, self._pump)

    def _build(self):
        head = tk.Frame(self.root, bg=DARK, height=96)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="XingSwitch", bg=DARK, fg="#ffffff",
                 font=("Microsoft YaHei UI", 20, "bold")).place(x=28, y=18)
        tk.Label(head, text="显示器输入源切换 · 手机可控制", bg=DARK, fg="#9aa4b2",
                 font=("Microsoft YaHei UI", 10)).place(x=29, y=56)
        tk.Label(head, text="v" + APP_VERSION, bg=DARK, fg=ACCENT,
                 font=("Microsoft YaHei UI", 9)).place(x=452, y=22)

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=28, pady=16)

        if self.mode == "install":
            tk.Label(body, text="安装位置", bg=BG, fg=MUTED,
                     font=("Microsoft YaHei UI", 9)).pack(anchor="w")
            row = tk.Frame(body, bg=BG)
            row.pack(fill="x", pady=(4, 12))
            tk.Entry(row, textvariable=self.target, font=("Microsoft YaHei UI", 10),
                     relief="solid", bd=1, bg="#fafafa").pack(side="left", fill="x",
                                                             expand=True, ipady=5)
            tk.Button(row, text="更改", command=self.browse, font=("Microsoft YaHei UI", 9),
                      relief="solid", bd=1, bg="#f3f4f6", cursor="hand2",
                      width=6).pack(side="left", padx=(8, 0))

            for text, var in (
                ("创建桌面快捷方式", self.desktop),
                ("开机自动启动（推荐）", self.autostart),
                ("放行防火墙，让手机能访问（推荐）", self.firewall),
            ):
                tk.Checkbutton(body, text=text, variable=var, bg=BG, fg=INK,
                               activebackground=BG, activeforeground=INK,
                               font=("Microsoft YaHei UI", 10), anchor="w",
                               cursor="hand2", highlightthickness=0,
                               bd=0).pack(fill="x", pady=0)

            tk.Label(body, text="装完常驻托盘；手机连同一个 WiFi，用浏览器打开网址即可切换。",
                     bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 9),
                     wraplength=460, justify="left").pack(anchor="w", pady=(10, 0))
        else:
            tk.Label(body, text="将从这台电脑上移除 XingSwitch。", bg=BG, fg=INK,
                     font=("Microsoft YaHei UI", 12)).pack(anchor="w")
            tk.Label(body, text="快捷方式、开机启动项、防火墙规则、以及你的备注配置都会一并删除。",
                     bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 9),
                     justify="left", wraplength=460).pack(anchor="w", pady=(8, 0))

        self.log = tk.Text(body, height=7, relief="solid", bd=1, bg="#fafafa",
                           fg="#374151", font=("Consolas", 9), state="disabled",
                           wrap="word")
        self.log.pack(fill="both", expand=True, pady=(12, 0))

        foot = tk.Frame(self.root, bg="#f7f8fa", height=64)
        foot.pack(fill="x", side="bottom")
        foot.pack_propagate(False)
        tk.Button(foot, text="取消", command=self._close,
                  font=("Microsoft YaHei UI", 10), bg="#f7f8fa", fg=MUTED,
                  relief="flat", bd=0, cursor="hand2", width=7).pack(side="right",
                                                                     padx=(0, 24), pady=14)
        danger = self.mode != "install"
        self.btn = tk.Button(foot, text="开始安装" if not danger else "确认卸载",
                             command=self.go, font=("Microsoft YaHei UI", 10, "bold"),
                             bg="#dc2626" if danger else ACCENT, fg="#ffffff",
                             activebackground="#b91c1c" if danger else "#0ea371",
                             activeforeground="#ffffff", relief="flat", bd=0,
                             cursor="hand2", width=12)
        self.btn.pack(side="right", padx=(0, 8), pady=14)

        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.write("准备就绪。点「%s」继续。" % self.btn["text"])

    def _close(self):
        self.done = True
        try:
            self.root.destroy()
        except Exception:
            pass

    def write(self, msg):
        self.q.put(("log", msg))

    def _pump(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "log":
                    self._append(item[1])
                elif item[0] == "done":
                    self._done(item[1], item[2])
                elif item[0] == "error":
                    self._fail(item[1])
        except Exception:
            pass
        if not self.done:
            self.root.after(120, self._pump)

    def _append(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def browse(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(initialdir=os.path.dirname(self.target.get()))
        if d:
            self.target.set(os.path.join(d, APP_ID))

    def go(self):
        if self.busy:
            return
        self.busy = True
        self.btn.configure(state="disabled", text="处理中…")
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        try:
            if self.mode == "install":
                do_install(self.target.get().strip(), self.desktop.get(),
                           self.autostart.get(), self.firewall.get(), self.write)
                ip = lan_ip()
                port = port_of(self.target.get().strip())
                self.q.put(("done", "安装完成",
                            "手机浏览器打开：http://%s:%d/\n（手机要和电脑连同一个 WiFi）" % (ip, port)))
            else:
                do_uninstall(self.write)
                self.q.put(("done", "卸载完成", ""))
        except Exception as e:
            self.q.put(("error", str(e)))

    def _done(self, title, extra):
        self.busy = False
        self.done = True
        self.root.title(title)
        self.btn.configure(state="normal", text="完成", command=self._close,
                           bg=DARK, activebackground="#000000")
        if extra:
            self._append("")
            self._append(extra)

    def _fail(self, msg):
        self.busy = False
        self._append("出错：%s" % msg)
        self.btn.configure(state="normal", text="重试")

    def run(self):
        self.root.mainloop()


def _main():
    args = [a.lower() for a in sys.argv[1:]]
    silent = "--silent" in args
    uninstall = "--uninstall" in args

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    if silent:
        out = sys.stdout
        if out is None:
            out = open(os.path.join(HERE, "install.log"), "w", encoding="utf-8")
        log = lambda m: (out.write(m + "\n"), out.flush())
        try:
            if uninstall:
                do_uninstall(log)
            else:
                do_install(DEFAULT_DIR, True, True, True, log)
        except Exception as e:
            log("ERROR %s" % e)
            return 1
        return 0

    App("uninstall" if uninstall else "install").run()
    return 0


def main():
    """任何异常都弹个中文提示，别给用户看裸 traceback。"""
    try:
        return _main()
    except Exception:
        import traceback
        detail = traceback.format_exc()
        try:
            with open(os.path.join(HERE, "installer-error.log"), "w",
                      encoding="utf-8") as f:
                f.write(detail)
        except Exception:
            pass
        try:
            tail = detail.strip().splitlines()[-1]
            ctypes.windll.user32.MessageBoxW(
                None,
                "安装程序出错了，没能继续。\n\n%s\n\n详细信息已写入同目录的 "
                "installer-error.log。" % tail[:300],
                "XingSwitch 安装程序", 0x10)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
