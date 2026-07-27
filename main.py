# -*- coding: utf-8 -*-
"""
剪贴板自动翻译小工具
------------------------------------
监听剪贴板，复制任意语言文本后自动翻译成中文，并用弹窗显示。
- 翻译方向：自动检测源语言 -> 中文
- 翻译服务：免费 Google 翻译 (deep-translator)
- 显示方式：右下角弹窗，停留时间随译文长度自动调整，也可点击关闭
- 剪贴板监听：使用 Windows 原生剪贴板事件（非轮询），不干扰正常的复制粘贴
"""

import ctypes
import queue
import re
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import font as tkfont

try:
    from deep_translator import GoogleTranslator
except ImportError:
    raise SystemExit(
        "缺少依赖 deep-translator。请先运行: pip install -r requirements.txt"
    )

# ---------------------------------------------------------------------------
# 配置项
# ---------------------------------------------------------------------------
TARGET_LANG = "zh-CN"         # 目标语言：简体中文
MAX_CHARS = 4500              # 单次翻译最大字符数（Google 免费接口约 5000 上限）
POPUP_WIDTH = 420             # 弹窗宽度（像素）

# 弹窗停留时间随字数动态计算：BASE + 每字 PER_CHAR 毫秒，并限制在 [MIN, MAX] 内
POPUP_BASE_MS = 3000          # 基础停留时间
POPUP_MS_PER_CHAR = 100       # 每个字符增加的毫秒数（约相当于 10 字/秒的阅读速度）
POPUP_MIN_MS = 4500           # 最短停留
POPUP_MAX_MS = 40000          # 最长停留（超长段落封顶，可点击提前关闭）
STARTUP_NOTICE_MS = 6000      # 启动提示停留时间


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _is_cjk(ch):
    """是否为中日韩表意文字。"""
    return "一" <= ch <= "鿿"


def is_mostly_chinese(text):
    """判断文本是否已经主要是中文（中文占比 > 50%），若是则跳过翻译。"""
    cjk = 0
    total = 0
    for ch in text:
        if ch.isspace():
            continue
        total += 1
        if _is_cjk(ch):
            cjk += 1
    if total == 0:
        return True
    return cjk / total > 0.5


def unwrap_text(text):
    """
    合并 PDF / 排版文本里的“硬换行”，让翻译更连贯。

    规则：
    - 空行（连续两个及以上换行）视为真正的段落分隔，予以保留。
    - 段落内部的单个换行视为“断行”，合并为连续文本：
      * 行尾是连字符 "-"（英文断词）→ 直接拼接并去掉连字符
      * 相邻两侧都是中文 → 直接相连（中文不需要空格）
      * 其他情况 → 用一个空格连接
    """
    if "\n" not in text:
        return text

    # 统一换行符，按空行切成段落
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = re.split(r"\n[ \t]*\n", normalized)

    result_paras = []
    for para in paragraphs:
        lines = [ln.strip() for ln in para.split("\n") if ln.strip()]
        if not lines:
            continue
        merged = lines[0]
        for line in lines[1:]:
            if merged.endswith("-"):          # 英文断词，如 inter-\nnational
                merged = merged[:-1] + line
            elif merged and (_is_cjk(merged[-1]) and _is_cjk(line[0])):
                merged += line                # 中文之间不加空格
            else:
                merged += " " + line          # 其余用空格连接
        result_paras.append(merged)

    return "\n\n".join(result_paras)


def translate_text(text):
    """调用 Google 翻译，返回译文。失败时返回错误提示字符串。"""
    snippet = text[:MAX_CHARS]
    try:
        result = GoogleTranslator(source="auto", target=TARGET_LANG).translate(snippet)
        return result or "(翻译结果为空)"
    except Exception as exc:  # 网络错误、接口变动等
        return "[翻译失败] " + str(exc)


def calc_popup_timeout(text):
    """根据译文字数动态计算弹窗停留时间（毫秒），字数越多停留越久。"""
    ms = POPUP_BASE_MS + len(text) * POPUP_MS_PER_CHAR
    return max(POPUP_MIN_MS, min(POPUP_MAX_MS, ms))



# ---------------------------------------------------------------------------
# 剪贴板读取 + 原生事件监听（Windows）
# ---------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
WM_CLIPBOARDUPDATE = 0x031D
WM_DESTROY = 0x0002
HWND_MESSAGE = wintypes.HWND(-3)

# 指针宽度相关类型（在 32/64 位下自适应）
LRESULT = ctypes.c_ssize_t   # LONG_PTR

WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT,
    wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def _setup_prototypes():
    """显式声明 Win32 API 的参数/返回类型，保证 64 位下指针不被截断。"""
    # 剪贴板读取
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]

    # 窗口 / 消息循环（关键：64 位下句柄和 lparam 是 64 位）
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.AddClipboardFormatListener.argtypes = [wintypes.HWND]
    user32.AddClipboardFormatListener.restype = wintypes.BOOL
    user32.RemoveClipboardFormatListener.argtypes = [wintypes.HWND]
    user32.RemoveClipboardFormatListener.restype = wintypes.BOOL
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE


def get_clipboard_text():
    """读取剪贴板中的文本；非文本或读取失败返回 None。只在收到变更事件后调用。"""
    text = None
    # 复制刚发生时源程序可能仍占用剪贴板，短暂重试几次
    for _ in range(6):
        if user32.OpenClipboard(None):
            try:
                handle = user32.GetClipboardData(CF_UNICODETEXT)
                if handle:
                    ptr = kernel32.GlobalLock(handle)
                    if ptr:
                        text = ctypes.c_wchar_p(ptr).value
                        kernel32.GlobalUnlock(handle)
            finally:
                user32.CloseClipboard()
            break
        time.sleep(0.03)
    return text


class ClipboardListener(threading.Thread):
    """后台线程：注册 Windows 剪贴板事件，变更时回调（不轮询、不占用剪贴板）。"""

    def __init__(self, on_change):
        super().__init__(daemon=True)
        self.on_change = on_change
        self._wndproc = WNDPROC(self._handle_msg)  # 必须持有引用，否则被回收

    def _handle_msg(self, hwnd, msg, wparam, lparam):
        if msg == WM_CLIPBOARDUPDATE:
            try:
                text = get_clipboard_text()
                if text:
                    self.on_change(text)
            except Exception:
                pass
            return 0
        if msg == WM_DESTROY:
            user32.RemoveClipboardFormatListener(hwnd)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self):
        hinst = kernel32.GetModuleHandleW(None)
        cls = WNDCLASS()
        cls.lpfnWndProc = self._wndproc
        cls.hInstance = hinst
        cls.lpszClassName = "ClipTransListenerWnd"
        user32.RegisterClassW(ctypes.byref(cls))
        hwnd = user32.CreateWindowExW(
            0, cls.lpszClassName, "cliptrans", 0, 0, 0, 0, 0,
            HWND_MESSAGE, None, hinst, None
        )
        user32.AddClipboardFormatListener(hwnd)
        # 消息循环
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


_setup_prototypes()



# ---------------------------------------------------------------------------
# 主应用
# ---------------------------------------------------------------------------
class ClipboardTranslator:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()  # 隐藏主窗口，只在需要时弹出提示窗

        self.last_text = get_clipboard_text() or ""  # 去重用
        self.result_queue = queue.Queue()  # 后台线程 -> 主线程 传递译文
        self.popup = None
        self.close_after_id = None

        # 启动原生剪贴板监听线程（事件驱动，不轮询）
        self.listener = ClipboardListener(self.on_clipboard_change)
        self.listener.start()

        # 主线程定时检查翻译结果队列，并弹出启动提示
        self.root.after(150, self.check_results)
        self.root.after(400, self.show_startup_notice)

    # ---- 剪贴板变更回调（在监听线程中执行）--------------------------------
    def on_clipboard_change(self, text):
        text = text.strip()
        if not text or text == self.last_text:
            return
        self.last_text = text
        if not is_mostly_chinese(text):
            threading.Thread(
                target=self._translate_worker, args=(text,), daemon=True
            ).start()

    def _translate_worker(self, text):
        cleaned = unwrap_text(text)      # 先合并 PDF 硬换行，让翻译连贯
        translated = translate_text(cleaned)
        self.result_queue.put((text, translated))

    # ---- 翻译结果处理 ------------------------------------------------------
    def check_results(self):
        try:
            while True:
                original, translated = self.result_queue.get_nowait()
                self.show_notice("译文", translated, "原文：", original,
                                 timeout=calc_popup_timeout(translated))
        except queue.Empty:
            pass
        self.root.after(150, self.check_results)

    # ---- 启动提示 ----------------------------------------------------------
    def show_startup_notice(self):
        self.show_notice(
            "✅ 剪贴板翻译工具已启动",
            "复制任意外文文本，这里就会自动弹出中文译文。",
            None, None, accent="#4ec9b0", timeout=STARTUP_NOTICE_MS,
        )

    # ---- 弹窗显示 ----------------------------------------------------------
    def show_notice(self, title, body, sub_label=None, sub_text=None,
                    accent="#4ec9b0", timeout=None):
        # 复用已有弹窗：先销毁旧的，保证只显示最新一条
        if self.popup is not None:
            self._destroy_popup()

        popup = tk.Toplevel(self.root)
        self.popup = popup
        popup.overrideredirect(True)          # 无标题栏
        popup.attributes("-topmost", True)     # 置顶
        popup.configure(bg="#1e1e1e")

        container = tk.Frame(popup, bg="#1e1e1e", padx=14, pady=12)
        container.pack(fill="both", expand=True)

        title_font = tkfont.Font(family="Microsoft YaHei UI", size=9, weight="bold")
        body_font = tkfont.Font(family="Microsoft YaHei UI", size=11)
        small_font = tkfont.Font(family="Microsoft YaHei UI", size=9)

        header = tk.Frame(container, bg="#1e1e1e")
        header.pack(fill="x")
        tk.Label(header, text=title, font=title_font, fg=accent,
                 bg="#1e1e1e").pack(side="left")
        tk.Label(header, text="点击关闭", font=small_font, fg="#666666",
                 bg="#1e1e1e").pack(side="right")

        # 主内容（译文 / 提示语）
        tk.Label(container, text=body, font=body_font, fg="#ffffff",
                 bg="#1e1e1e", wraplength=POPUP_WIDTH - 28, justify="left",
                 anchor="w").pack(fill="x", pady=(6, 8))

        # 副内容（原文），仅在提供时显示
        if sub_text is not None:
            tk.Frame(container, bg="#333333", height=1).pack(fill="x")
            sub_display = sub_text if len(sub_text) <= 120 else sub_text[:120] + "…"
            tk.Label(container, text=(sub_label or "") + sub_display,
                     font=small_font, fg="#888888", bg="#1e1e1e",
                     wraplength=POPUP_WIDTH - 28, justify="left",
                     anchor="w").pack(fill="x", pady=(8, 0))

        # 点击任意处关闭
        for widget in [popup, container] + list(container.winfo_children()):
            widget.bind("<Button-1>", lambda e: self._destroy_popup())

        # 定位到屏幕右下角
        popup.update_idletasks()
        w = POPUP_WIDTH
        h = popup.winfo_reqheight()
        x = popup.winfo_screenwidth() - w - 20
        y = popup.winfo_screenheight() - h - 60   # 留出任务栏空间
        popup.geometry(f"{w}x{h}+{x}+{y}")

        self.close_after_id = self.root.after(
            timeout or POPUP_MIN_MS, self._destroy_popup)

    def _destroy_popup(self):
        if self.close_after_id is not None:
            try:
                self.root.after_cancel(self.close_after_id)
            except Exception:
                pass
            self.close_after_id = None
        if self.popup is not None:
            try:
                self.popup.destroy()
            except Exception:
                pass
            self.popup = None


    def run(self):
        print("剪贴板翻译工具已启动，正在监听剪贴板... (关闭此窗口即退出)")
        self.root.mainloop()


if __name__ == "__main__":
    ClipboardTranslator().run()



