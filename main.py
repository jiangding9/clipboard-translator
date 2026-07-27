# -*- coding: utf-8 -*-
"""
剪贴板自动翻译小工具
------------------------------------
监听剪贴板，复制任意语言文本后自动翻译成中文，并用弹窗显示。
- 翻译方向：自动检测源语言 -> 中文
- 翻译服务：免费 Google 翻译 (deep-translator)
- 显示方式：右下角弹窗，几秒后自动消失，也可点击关闭
"""

import queue
import threading
import time
import tkinter as tk
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
POLL_INTERVAL_MS = 600        # 剪贴板轮询间隔（毫秒）
TARGET_LANG = "zh-CN"         # 目标语言：简体中文
POPUP_TIMEOUT_MS = 8000       # 弹窗自动关闭时间（毫秒）
MAX_CHARS = 4500              # 单次翻译最大字符数（Google 免费接口约 5000 上限）
POPUP_WIDTH = 420             # 弹窗宽度（像素）


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def is_mostly_chinese(text):
    """判断文本是否已经主要是中文（中文占比 > 50%），若是则跳过翻译。"""
    cjk = 0
    total = 0
    for ch in text:
        if ch.isspace():
            continue
        total += 1
        # CJK 统一表意文字基本区
        if "一" <= ch <= "鿿":
            cjk += 1
    if total == 0:
        return True
    return cjk / total > 0.5


def translate_text(text):
    """调用 Google 翻译，返回译文。失败时返回错误提示字符串。"""
    snippet = text[:MAX_CHARS]
    try:
        result = GoogleTranslator(source="auto", target=TARGET_LANG).translate(snippet)
        return result or "(翻译结果为空)"
    except Exception as exc:  # 网络错误、接口变动等
        return "[翻译失败] " + str(exc)



# ---------------------------------------------------------------------------
# 主应用
# ---------------------------------------------------------------------------
class ClipboardTranslator:
    def __init__(self):
        # 主窗口作为隐藏的根，用于承载 tkinter 事件循环
        self.root = tk.Tk()
        self.root.withdraw()  # 隐藏主窗口，只在需要时弹出提示窗

        self.last_text = ""          # 上一次处理过的剪贴板内容，用于去重
        self.result_queue = queue.Queue()  # 后台线程 -> 主线程 传递译文
        self.popup = None            # 当前弹窗（复用同一个）
        self.close_after_id = None   # 弹窗自动关闭定时器

        # 记录启动时剪贴板已有内容，避免一启动就翻译旧内容
        try:
            self.last_text = self.root.clipboard_get()
        except tk.TclError:
            self.last_text = ""

        # 启动两个循环：轮询剪贴板 + 检查翻译结果
        self.root.after(POLL_INTERVAL_MS, self.poll_clipboard)
        self.root.after(150, self.check_results)
        # 启动后弹一个欢迎提示，让用户知道程序已在运行
        self.root.after(400, self.show_startup_notice)

    # ---- 剪贴板轮询 --------------------------------------------------------
    def poll_clipboard(self):
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            text = ""  # 剪贴板为空或非文本（如图片）

        text = text.strip() if text else ""
        if text and text != self.last_text:
            self.last_text = text
            if not is_mostly_chinese(text):
                # 在后台线程翻译，避免阻塞界面
                threading.Thread(
                    target=self._translate_worker, args=(text,), daemon=True
                ).start()

        self.root.after(POLL_INTERVAL_MS, self.poll_clipboard)

    def _translate_worker(self, text):
        translated = translate_text(text)
        self.result_queue.put((text, translated))

    # ---- 翻译结果处理 ------------------------------------------------------
    def check_results(self):
        try:
            while True:
                original, translated = self.result_queue.get_nowait()
                self.show_notice("译文", translated, "原文：", original)
        except queue.Empty:
            pass
        self.root.after(150, self.check_results)

    # ---- 启动提示 ----------------------------------------------------------
    def show_startup_notice(self):
        self.show_notice(
            "✅ 剪贴板翻译工具已启动",
            "复制任意外文文本，这里就会自动弹出中文译文。",
            None, None, accent="#4ec9b0", timeout=6000,
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

        # 内容容器
        container = tk.Frame(popup, bg="#1e1e1e", padx=14, pady=12)
        container.pack(fill="both", expand=True)

        title_font = tkfont.Font(family="Microsoft YaHei UI", size=9, weight="bold")
        body_font = tkfont.Font(family="Microsoft YaHei UI", size=11)
        small_font = tkfont.Font(family="Microsoft YaHei UI", size=9)

        # 顶部标签行
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
        screen_w = popup.winfo_screenwidth()
        screen_h = popup.winfo_screenheight()
        x = screen_w - w - 20
        y = screen_h - h - 60   # 留出任务栏空间
        popup.geometry(f"{w}x{h}+{x}+{y}")

        # 定时自动关闭
        self.close_after_id = self.root.after(
            timeout or POPUP_TIMEOUT_MS, self._destroy_popup)

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


