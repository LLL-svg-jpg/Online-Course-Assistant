"""CourseMate 图形界面。

为什么用 tkinter 而不是 PyQt/Electron：tkinter 是 Python 标准库，
零额外依赖。这台机器装不了 pip 包，而且用户要的是"双击就能开"——
少一个依赖就少一个装不上的理由。

线程模型：
  主线程   tkinter mainloop，只碰界面
  工作线程 asyncio.run(runner.run(...))，只碰浏览器
  两者之间用 queue.Queue 传日志，界面侧用 after() 定时取。
  tkinter 不是线程安全的，所以工作线程绝不直接碰控件。
"""
from __future__ import annotations

import math
import os
import queue
import subprocess
import threading
import time
import uuid
import webbrowser
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .config import SPEED_MAX, SPEED_MAX_UNLOCKED, SPEED_MIN, Config, ConfigError
from .config_writer import save_config
from .logger import Logger
from .paths import app_dir, is_frozen, resource
from . import providers, updates

APP_NAME = "Online Course Assistant"
PROJECT_URL = "https://github.com/LLL-svg-jpg/Online-Course-Assistant"
# 配置必须落在 exe 旁边，不能落在打包解压出来的临时目录
CONFIG_PATH = app_dir() / "config.toml"

# 日志区最多保留的行数。刷课会跑几小时，不限制会把内存吃光。
MAX_LOG_LINES = 2000

# 字号。刷课要盯着看很久，宁可大一点。
FONT = ("Microsoft YaHei UI", 15)
FONT_BOLD = ("Microsoft YaHei UI", 15, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 23, "bold")
FONT_HINT = ("Microsoft YaHei UI", 12)
FONT_LOG = ("Consolas", 14)

# 全部刷完后的动作。挂机刷课的人常需要"刷完自动关机"
FINISH_ACTIONS = {
    "什么都不做": "none",
    "退出本程序": "quit",
    "让电脑睡眠": "sleep",
    "关闭电脑": "shutdown",
}
FINISH_LABELS = {v: k for k, v in FINISH_ACTIONS.items()}

COLORS = {
    "DEBUG": "#8a8a8a",
    "INFO": "#1a1a1a",
    "WARN": "#b8860b",
    "ERROR": "#c62828",
    "PROGRESS": "#0b6e4f",
    "SYSTEM": "#1565c0",
}


class _AccountDialog(simpledialog.Dialog):
    def __init__(self, parent, title, name="", credentials=False):
        self.name = name
        self.credentials = credentials
        super().__init__(parent, title)

    def body(self, parent):
        self.minsize(520, 180)
        frame = ttk.Frame(parent, padding=20)
        frame.pack(fill="both", expand=True)
        self.entries = {}
        fields = [("name", "名称", self.name)]
        if self.credentials:
            fields += [("username", "账号", ""), ("password", "密码", "")]
        for row, (key, label, value) in enumerate(fields):
            ttk.Label(frame, text=label, font=FONT).grid(row=row, column=0, sticky="w", pady=8)
            entry = ttk.Entry(frame, width=30, font=FONT, show="●" if key == "password" else "")
            entry.insert(0, value)
            entry.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=8)
            self.entries[key] = entry
        ttk.Label(frame, text="名称用于区分账号；账号和密码填写网页登录凭据。" if self.credentials
                  else "请输入便于识别的名称：", font=FONT_HINT).grid(
                      row=len(fields), column=0, columnspan=2, sticky="w", pady=(8, 0))
        return self.entries["name"]

    def buttonbox(self):
        box = ttk.Frame(self, padding=(20, 0, 20, 20))
        box.pack(fill="x")
        ttk.Style(self).configure("AccountDialog.TButton", font=FONT, padding=(16, 8))
        ttk.Button(box, text="取消", command=self.cancel, style="AccountDialog.TButton").pack(side="right")
        ttk.Button(box, text="确定", command=self.ok, style="AccountDialog.TButton").pack(
            side="right", padx=(0, 12))
        self.bind("<Return>", self.ok)
        self.bind("<Escape>", self.cancel)

    def validate(self):
        if self.entries["name"].get().strip():
            return True
        messagebox.showwarning("账号名称", "请输入便于识别的名称。", parent=self)
        return False

    def apply(self):
        self.result = {key: entry.get() for key, entry in self.entries.items()}
        self.result["name"] = self.result["name"].strip()
        if self.credentials:
            self.result["username"] = self.result["username"].strip()


class _FontScale(tk.Frame):
    """字号滑块：直接绘制滑槽和滑块，避免本机 EXE 的 Scale 漏绘。"""

    def __init__(self, parent, variable, command, on_release, bg):
        super().__init__(parent, width=100, height=26, bg=bg)
        self.variable, self.command = variable, command
        self.on_release = on_release
        self._drag_offset = None
        self._track = tk.Label(self, text=" ", bg="#d9d9d9", borderwidth=0)
        self._track.place(x=7, y=12, relwidth=1, width=-14, height=2)
        self._thumb = tk.Label(self, text=" ", bg="#0078d7", borderwidth=0)
        self.bind("<Configure>", self._draw)
        for widget in (self, self._track, self._thumb):
            widget.bind("<Button-1>", self._press)
            widget.bind("<B1-Motion>", self._drag)
            widget.bind("<ButtonRelease-1>", self._release)
        self._trace = variable.trace_add("write", self._value_changed)

    def coords(self, value=None):
        value = self.variable.get() if value is None else value
        return (7 + (max(10, min(24, value)) - 10) / 14 * max(1, self.winfo_width() - 14), 13)

    def identify(self, x, y):
        center, _ = self.coords()
        return "slider" if abs(x - center) <= 7 and 1 <= y <= 25 else ""

    def _draw(self, _event=None):
        # 移动子窗口后主动重画背景，擦掉滑块旧位置的残留。
        self.configure(bg=self.cget("bg"))
        self._track.configure(bg="#d9d9d9")
        self._thumb.place(x=round(self.coords()[0] - 7), y=1, width=14, height=24)

    def _value_changed(self, *_args):
        self._draw()
        self.command()

    def _press(self, event):
        x = event.x + (event.widget.winfo_x() if event.widget is not self else 0)
        y = event.y + (event.widget.winfo_y() if event.widget is not self else 0)
        self._drag_offset = x - self.coords()[0] if self.identify(x, y) else None

    def _drag(self, event):
        if self._drag_offset is not None:
            x = event.x + (event.widget.winfo_x() if event.widget is not self else 0)
            value = 10 + (x - self._drag_offset - 7) / max(1, self.winfo_width() - 14) * 14
            self.variable.set(max(10, min(24, value)))

    def _release(self, _event):
        self._drag_offset = None
        self.on_release()

    def destroy(self):
        self.variable.trace_remove("write", self._trace)
        super().destroy()

# Vista 主题会把获得焦点的只读下拉框画成蓝底白字；这和真正可编辑的输入框
# 不同，选完浏览器/服务商后不该还像在编辑。可编辑的模型框不使用这个样式。
READONLY_COMBO_STYLE = "CourseMate.Readonly.TCombobox"



# 关于滚动残影：真正的成因在 Win32 层，不在 tkinter 里。
# Windows 移动一个子窗口时会**立刻把屏幕上的像素搬过去**，而重绘要等
# 下一条 WM_PAINT。这中间的一瞬间屏幕上是搬错位的旧像素，快速滚动时
# 连成一串就是残影。所以 Python 层怎么改都治不到根上——update_idletasks()
# 只能让重绘尽快发生，挡不住"搬运本身已经显示出来了"。
#
# 试过 WS_EX_COMPOSITED（Windows 的窗口级双缓冲，理论上的正解：整帧画完
# 才贴到屏幕，中间态不会被看见）。实测在本机会让界面构建直接卡死，
# 三次不同写法都卡在同一处，已放弃——拿卡死换一点视觉改善不划算。
#
# 也调研过 ttkwidgets / ttkbootstrap 等成熟滚动容器：它们全都用 Canvas，
# 没有一个处理这件事（且 ttkwidgets 是 GPLv3，本项目不能用）。
# 结论是 tkinter 生态里没有现成解法。目前的做法是把可控的部分做到最好：
# 移动后立即同步重绘，让每一帧尽快正确。


def bind_wheel(widget, handler) -> None:
    """把滚轮事件绑到控件及其所有后代上。

    不用 bind_all 的原因：那是全局绑定，鼠标一进入某个滚动区就把
    整个窗口的滚轮劫持走，几个滚动区互相抢，日志区反而滚不动。
    tkinter 的滚轮事件又不会向父容器冒泡，所以只能逐个后代绑。
    """
    widget.bind("<MouseWheel>", handler, add="+")
    for child in widget.winfo_children():
        bind_wheel(child, handler)


class UrlList(ttk.Frame):
    """课程地址输入区。

    一行一个地址，各自带备注，可拖动排序。
    比多行文本框好在：边界清楚不会看串行、能单独删除、能排序、能加备注。
    """

    ROW_HEIGHT = 52
    VISIBLE_ROWS = 3  # 超过这个数目就出现滚动条，避免把窗口撑爆

    def __init__(self, parent):
        super().__init__(parent)
        self.rows: list[dict] = []
        self.accounts = {"default": "默认账号"}
        self.configure_row = None
        self._drag: dict | None = None

        # 和设置页共用同一套滚动实现：地址一多，这里同样会快速滚，
        # 残影的成因是一模一样的，没必要再写一份 Canvas 版本
        self.scroll = ScrollFrame(self, height=self.ROW_HEIGHT * self.VISIBLE_ROWS)
        self.scroll.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.inner = self.scroll.inner
        self.columnconfigure(0, weight=1)

        add_bar = ttk.Frame(self)
        add_bar.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(add_bar, text="＋  添加一个课程地址", command=self.add_row).pack(side="left")
        self.count_label = ttk.Label(add_bar, text="", style="Hint.TLabel")
        self.count_label.pack(side="left", padx=(12, 0))
        ttk.Label(add_bar, text="按住左边 ⣿ 可上下拖动排序",
                  style="Hint.TLabel").pack(side="left", padx=(12, 0))

        self.add_row()

    # ---- 滚动 ----

    def _on_wheel(self, event) -> None:
        return self.scroll._on_wheel(event)

    # ---- 行管理 ----

    def add_row(self, value: str = "", note: str = "", account_id: str = "default") -> None:
        row = ttk.Frame(self.inner, padding=(2, 4))
        row.pack(fill="x", expand=True)

        # 拖动手柄。只有它响应拖拽，避免在输入框里选文字时误触发排序
        handle = ttk.Label(row, text="⣿", width=2, cursor="fleur",
                           style="Hint.TLabel")
        handle.pack(side="left")
        index_label = ttk.Label(row, text="", width=3, style="Hint.TLabel")
        index_label.pack(side="left")

        var = tk.StringVar(value=value)
        entry = ttk.Entry(row, textvariable=var, font=FONT)
        entry.pack(side="left", fill="x", expand=True, padx=(4, 6))

        note_var = tk.StringVar(value=note)
        note_entry = ttk.Entry(row, textvariable=note_var, font=FONT, width=14)
        note_entry.pack(side="left", padx=(0, 6))

        account_var = tk.StringVar(value=self.accounts.get(account_id, "账号已移除"))
        account_combo = ttk.Combobox(row, textvariable=account_var, width=12,
                                     font=FONT, style=READONLY_COMBO_STYLE,
                                     state="readonly", values=list(self.accounts.values()))
        account_combo.pack(side="left", padx=(0, 6))

        record = {"frame": row, "var": var, "entry": entry, "label": index_label,
                  "note_var": note_var, "note_entry": note_entry, "handle": handle,
                  "account_id": account_id, "account_var": account_var, "account_combo": account_combo}
        account_combo.bind("<<ComboboxSelected>>", lambda e, r=record:
                           r.update(account_id=list(self.accounts)[r["account_combo"].current()]))
        remove_btn = ttk.Button(row, text="✕", width=3,
                                command=lambda r=record: self.remove_row(r))
        remove_btn.pack(side="left")
        record["button"] = remove_btn

        if self.configure_row is not None:
            self.configure_row(row)

        for w in (handle, index_label):
            w.bind("<Button-1>", lambda e, r=record: self._drag_start(r))
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", lambda e: self._drag_end())

        self.rows.append(record)
        bind_wheel(row, self._on_wheel)
        self._refresh()
        if not value:
            entry.focus_set()
        # 新加的一行滚进视野，省得用户以为没加上
        self.after(30, self.scroll.scroll_to_bottom)

    def remove_row(self, record: dict) -> None:
        # 永远至少留一行，否则界面上会出现"没有任何输入框"的空白状态
        if len(self.rows) <= 1:
            record["var"].set("")
            record["note_var"].set("")
            record["account_id"] = "default"
            record["account_var"].set(self.accounts["default"])
            return
        record["frame"].destroy()
        self.rows.remove(record)
        self._refresh()

    # ---- 拖动排序 ----

    def _drag_start(self, record: dict) -> None:
        self._drag = record
        record["frame"].configure(relief="raised", borderwidth=1)

    def _drag_move(self, event) -> None:
        if not self._drag:
            return
        # 用指针在 inner 坐标系里的位置换算目标行号：
        # 每行高度一致，除一下就知道该插到第几个
        y = self.inner.winfo_pointery() - self.inner.winfo_rooty()
        row_h = max(1, self._drag["frame"].winfo_height())
        target = max(0, min(len(self.rows) - 1, int(y // row_h)))
        current = self.rows.index(self._drag)
        if target != current:
            self.rows.insert(target, self.rows.pop(current))
            self._repack()

    def _drag_end(self) -> None:
        if self._drag:
            self._drag["frame"].configure(relief="flat", borderwidth=0)
            self._drag = None
            self._refresh()

    def move(self, record: dict, delta: int) -> None:
        """按钮式移动，作为拖拽之外的精确手段。"""
        i = self.rows.index(record)
        j = max(0, min(len(self.rows) - 1, i + delta))
        if i != j:
            self.rows.insert(j, self.rows.pop(i))
            self._repack()
            self._refresh()

    def _repack(self) -> None:
        for record in self.rows:
            record["frame"].pack_forget()
        for record in self.rows:
            record["frame"].pack(fill="x", expand=True)

    def _refresh(self) -> None:
        for i, record in enumerate(self.rows, 1):
            record["label"].configure(text=f"{i}.")
            # 即使当前只有一条地址，也保留手柄。否则界面还提示“按住左边
            # ⣿”，左边却是空白，用户无法知道该从哪里拖动。
            record["handle"].configure(text="⣿")
        filled = len([r for r in self.rows if r["var"].get().strip()])
        self.count_label.configure(text=f"已填 {filled} 门课程" if filled else "")
        self.scroll.refresh()

    # ---- 取值 / 赋值 ----

    def get_urls(self) -> list[str]:
        return [r["var"].get().strip() for r in self.rows if r["var"].get().strip()]

    def get_items(self) -> list[dict]:
        """返回带备注的完整条目，顺序即界面顺序。"""
        out = []
        for r in self.rows:
            url = r["var"].get().strip()
            if url:
                item = {"url": url, "note": r["note_var"].get().strip()}
                if r["account_id"] != "default":
                    item["account_id"] = r["account_id"]
                out.append(item)
        return out

    def set_items(self, items: list[dict]) -> None:
        for record in list(self.rows):
            record["frame"].destroy()
        self.rows.clear()
        for item in items or [{"url": "", "note": ""}]:
            self.add_row(item.get("url", ""), item.get("note", ""), item.get("account_id", "default"))
        self._refresh()

    def set_accounts(self, accounts: dict[str, dict[str, str]]) -> None:
        self.accounts = {key: value["name"] for key, value in accounts.items()}
        for row in self.rows:
            row["account_combo"].configure(values=list(self.accounts.values()))
            row["account_var"].set(self.accounts.get(row["account_id"], "账号已移除"))

    def set_urls(self, urls: list[str]) -> None:
        self.set_items([{"url": u, "note": ""} for u in urls])


class ScrollFrame(ttk.Frame):
    """可滚动容器。往 `.inner` 里放内容即可。

    残影的成因不是"用了 Canvas"，换成 place 移动一样会有——
    真正的原因是**画面挪得比重绘快**：滚轮事件一秒来几十个，
    每来一个就把内容挪一段，而 Tk 的重绘是攒到空闲时才做的。
    事件比重绘快，屏幕上就同时留着好几帧的字，看起来就是残影。

    治它的关键只有一句：**每挪一次就当场画完**（_apply 里的 update_idletasks），
    不让重绘攒着。实测画完一帧只要 0.5 毫秒，所以这件事一点都不贵。

    这里**不做缓动**。缓动会让一格滚轮拖上十几帧才走完，手感就是跟不住鼠标；
    而且既然每次移动都当场画完了，慢慢挪对防残影毫无意义。
    所以滚轮事件直接驱动画面，一格一步到位，和系统原生滚动一样跟手。
    MAX_FRAME_STEP 只是给极端情况（一次事件要跳很远）留的上限，
    超出的部分交给 16 毫秒一帧的动画补完，避免画面瞬移。
    """

    STEP = 60            # 一格滚轮走多少像素，和系统原生的三行差不多
    MAX_FRAME_STEP = 90  # 单次移动的上限，正常滚一格用不到它
    FRAME_MS = 16        # 约 60 帧/秒，只在补完超出部分时才跑

    def __init__(self, parent, height: int = 380):
        super().__init__(parent)
        self.offset = 0.0
        self.target = 0.0
        self._anim: str | None = None
        self._frame_cost = 0.0      # 上一帧的重绘耗时，测试用来确认追得上
        self._last_metrics = (0, 0)

        self.viewport = ttk.Frame(self, height=height)
        self.viewport.pack(side="left", fill="both", expand=True)
        # 关掉尺寸自适应，否则内层一变高，视口就跟着撑开，等于没有滚动
        self.viewport.pack_propagate(False)

        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self._on_scrollbar)
        self.scrollbar.pack(side="right", fill="y")

        self.inner = ttk.Frame(self.viewport)
        self.inner.place(x=0, y=0, relwidth=1.0)

        self.inner.bind("<Configure>", self._on_content_change)
        self.viewport.bind("<Configure>", self._on_content_change)
        self.viewport.bind("<MouseWheel>", self._on_wheel, add="+")

    # ---- 位置计算 ----

    def _max_offset(self) -> int:
        return max(0, self.inner.winfo_reqheight() - self.viewport.winfo_height())

    def _clamp(self, value: float) -> float:
        return max(0.0, min(float(value), float(self._max_offset())))

    def _apply(self, redraw: bool = True) -> None:
        """把 offset 落到界面上。redraw=True 时当场画完这一帧。"""
        self.inner.place_configure(y=-int(round(self.offset)))
        total = max(1, self.inner.winfo_reqheight())
        view = self.viewport.winfo_height()
        self.scrollbar.set(self.offset / total, min(1.0, (self.offset + view) / total))
        if redraw:
            # 关键的一句：不让重绘攒着。这一帧画完再进下一帧，
            # 屏幕上任何时刻都只有一帧的内容
            start = time.perf_counter()
            self.viewport.update_idletasks()
            self._frame_cost = time.perf_counter() - start

    def _on_content_change(self, _event=None) -> None:
        """内容或视口尺寸变了才重新对齐。

        place_configure 改位置也会发 <Configure>，
        不比对尺寸就会自己触发自己，转成死循环。
        """
        metrics = (self.inner.winfo_reqheight(), self.viewport.winfo_height())
        if metrics == self._last_metrics:
            return
        self._last_metrics = metrics
        self.target = self._clamp(self.target)
        self.offset = self._clamp(self.offset)
        self._apply(redraw=False)

    # ---- 移动 ----

    def _advance(self) -> bool:
        """朝目标走一步。全速走，不缓动——缓动就是"跟不住鼠标"的来源。

        返回是否还没走到，供动画决定要不要再排一帧。
        """
        diff = self.target - self.offset
        if abs(diff) < 0.5:
            self.offset = self.target
            self._apply()
            return False
        self.offset += max(-self.MAX_FRAME_STEP, min(self.MAX_FRAME_STEP, diff))
        self._apply()
        return abs(self.target - self.offset) >= 0.5

    def _start_anim(self) -> None:
        if self._anim is None:
            self._anim = self.after(self.FRAME_MS, self._tick)

    def _tick(self) -> None:
        self._anim = None
        if not self.winfo_exists():
            return
        if self._advance():
            self._anim = self.after(self.FRAME_MS, self._tick)

    def _on_wheel(self, event) -> None:
        if self._max_offset() <= 0:  # 内容没超出，滚了也没反应，别白滚
            return "break"
        self.target = self._clamp(self.target - (event.delta // 120) * self.STEP)
        # 当场就走，鼠标一动画面就动。走不完的（一次要跳很远）才交给动画
        if self._advance():
            self._start_anim()
        return "break"

    def _on_scrollbar(self, *args) -> None:
        if args[0] == "moveto":
            # 拖滚动条是直接定位，必须一步到位跟住鼠标。
            # 走动画的话，拖快了画面就落在鼠标后面，一路补帧——那才是拖影
            self.offset = self.target = self._clamp(
                float(args[1]) * self.inner.winfo_reqheight())
            self._apply()
            return
        if args[0] == "scroll":
            unit = self.STEP if args[2] == "units" else self.viewport.winfo_height()
            self.target = self._clamp(self.target + int(args[1]) * unit)
        if self._advance():
            self._start_anim()

    def bind_wheel_all(self) -> None:
        """内容建好后调一次：滚轮事件不冒泡，只能逐个后代绑。"""
        bind_wheel(self.inner, self._on_wheel)

    def refresh(self) -> None:
        """内容增删之后调一次，重算可滚范围。"""
        self.update_idletasks()
        self._last_metrics = (0, 0)   # 强制重算，别被短路挡掉
        self._on_content_change()

    def scroll_to_bottom(self) -> None:
        self.refresh()
        self.target = float(self._max_offset())
        if self._advance():
            self._start_anim()


class CourseMateGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.logger = Logger()
        self.log_queue: queue.Queue = queue.Queue()
        self._update_events: queue.Queue = queue.Queue()
        self._installing_update = False
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self._log_lines = 0
        self._normal_geometry = ""
        # 跨标签页共用的变量在这里先建好，避免依赖标签页的构建顺序
        self.cache_var = tk.BooleanVar(value=True)
        self._provider_keys: dict[str, str] = {}
        self._active_provider_key = "deepseek"

        root.title(APP_NAME)
        root.minsize(1000, 700)
        self._set_icon()
        self._restore_geometry()

        # 箭头单独放大：它是唯一一个纯靠形状表意的控件，
        # 跟正文一样大就太不起眼，找不到点哪儿能收起日志
        family_arrow, size_arrow = "Microsoft YaHei UI", 18
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure(".", font=FONT)
        style.configure("TLabelframe.Label", font=FONT_BOLD)
        style.configure("TNotebook.Tab", font=FONT, padding=(18, 8))
        # 三个页签保留键盘焦点，只移除围绕文字的虚线绘制元素。
        style.layout("TNotebook.Tab", [("Notebook.tab", {"sticky": "nswe", "children": [
            ("Notebook.padding", {"side": "top", "sticky": "nswe", "children": [
                ("Notebook.label", {"side": "top", "sticky": ""})
            ]})
        ]})])
        style.configure("TButton", font=FONT, padding=(10, 6))
        style.configure("Run.TButton", font=("Microsoft YaHei UI", 16, "bold"),
                        padding=(16, 8))
        style.configure("Hint.TLabel", font=FONT_HINT, foreground="#666")
        style.configure("Warn.TLabel", font=FONT_HINT, foreground="#a33")
        style.configure("Card.TFrame", relief="solid", borderwidth=1)
        style.configure("CardTitle.TLabel", font=FONT_BOLD)
        style.configure("Arrow.TLabel", font=(family_arrow, size_arrow), foreground="#000000")
        style.configure(
            READONLY_COMBO_STYLE,
            font=FONT,
            foreground="#000000",
            selectforeground="#000000",
            selectbackground="#ffffff",
            fieldbackground="#ffffff",
        )
        style.map(
            READONLY_COMBO_STYLE,
            foreground=[("readonly focus", "#000000"), ("readonly", "#000000")],
            selectforeground=[("readonly focus", "#000000"), ("readonly", "#000000")],
            selectbackground=[("readonly focus", "#ffffff"), ("readonly", "#ffffff")],
            fieldbackground=[("readonly focus", "#ffffff"), ("readonly", "#ffffff")],
        )
        if style.theme_use() == "vista":
            # 原生边框和箭头的焦点、悬停、按下状态会变蓝；保持普通灰色绘制。
            for element, part, options in (
                ("border", 4, {}),
                ("rightdownarrow", 6, {"syssize": ("SM_CXVSCROLL", "SM_CYVSCROLL")}),
            ):
                style.element_create(f"CourseMate.Combobox.{element}", "vsapi", "COMBOBOX",
                                     part, [("disabled", 4), ("", 1)], **options)
            for combo_style in ("TCombobox", READONLY_COMBO_STYLE):
                text = [("Combobox.textarea", {"sticky": "nswe"})]
                # 只读控件省略会填蓝文字区的焦点层；可编辑模型保留文字选择。
                if combo_style == "TCombobox":
                    text = [("Combobox.focus", {"sticky": "nswe", "children": text})]
                style.layout(combo_style, [("CourseMate.Combobox.border", {
                    "sticky": "nswe", "children": [
                        ("CourseMate.Combobox.rightdownarrow", {"side": "right", "sticky": "ns"}),
                        ("Combobox.padding", {"sticky": "nswe", "children": [
                            ("Combobox.background", {"sticky": "nswe", "children": text})
                        ]})
                    ]
                })])
            # 补回焦点层原有的内边距，保持下拉框与输入框等高。
            style.configure(READONLY_COMBO_STYLE, padding=3)
        # 设置页左侧分类栏。选中项加粗、变蓝、垫浅蓝底，再配左边一条竖杠
        style.configure("NavItem.TLabel", font=FONT, foreground="#333")
        style.configure("NavItemOn.TLabel", font=FONT_BOLD, foreground="#0b5cad",
                        background="#e5eefa")

        self._build_ui()
        self._tidy_comboboxes(root)
        root.bind_class("ComboboxPopdown", "<Unmap>", self._on_combobox_unmap, add="+")
        self._no_select_on_traverse()
        self._attach_edit_menu()
        self._start_tray()
        self.logger.add_sink(self._on_log)
        self.load_config()
        # 配置文件不存在或损坏时，load_config 会提前返回；仍要按全部标签页
        # 的真实高度设置最小窗口，不能让 AI 页底部说明被日志区裁掉。
        self._update_minsize()
        self._update_browser_hint()
        self._heal_autostart()
        root.bind("<Configure>", self._on_configure)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(120, self._drain_log_queue)
        # 延后一点再自动开始，让界面先画完、日志区能接住启动信息
        if self.start_min_var.get():
            self.root.after(200, self.root.iconify)
        if self.autorun_var.get():
            self._append_log("SYSTEM", "已开启「打开后自动开始」，3 秒后启动...")
            self.root.after(3000, self._autorun_start)

    # ---------------- 界面构建 ----------------

    GEOMETRY_FILE = "runtime/window.txt"

    def _restore_geometry(self) -> None:
        """恢复上次的窗口大小与位置；首次运行则居中打开。

        全屏用起来最舒服，但直接强制最大化会打断只想开个小窗看看的人，
        所以只记住上次的选择。
        """
        try:
            saved = (app_dir() / self.GEOMETRY_FILE).read_text(encoding="utf-8").strip()
            geo, _, state = saved.partition("|")
            if geo:
                self.root.geometry(geo)
            if state == "zoomed":
                self.root.state("zoomed")
            return
        except Exception:
            pass

        # 首次运行：取屏幕的八成，居中放置，避免开在屏幕角落或超出边界
        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = min(1280, int(sw * 0.8)), min(1000, int(sh * 0.85))
        self.root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2 - 20)}")

    def _save_geometry(self) -> None:
        try:
            state = self.root.state()
            # 收进托盘时 state 是 withdrawn，最小化时是 iconic。
            # 把这两个存下去，下次启动就会是一个看不见的窗口——等于软件打不开了
            if state not in ("normal", "zoomed"):
                state = "normal"
            # 最大化状态下取到的 geometry 是最大化后的尺寸，
            # 存下来会导致下次"还原"窗口时还是满屏，所以分开记
            geo = self._normal_geometry or self.root.geometry()
            path = app_dir() / self.GEOMETRY_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{geo}|{state}", encoding="utf-8")
        except Exception:
            pass

    def _on_configure(self, event) -> None:
        # 只在非最大化时记录尺寸，供下次还原用
        try:
            if event.widget is self.root and self.root.state() == "normal":
                self._normal_geometry = self.root.geometry()
        except Exception:
            pass

    def _set_icon(self) -> None:
        """设置窗口与任务栏图标。

        两条路都走：iconbitmap 管标题栏和任务栏，iconphoto 管 Alt-Tab
        和某些主题下的图标，两者缺一都可能露出默认的 Tk 羽毛图标。
        装饰性资源缺失绝不能让程序起不来，所以整段都吞异常。
        """
        assets = resource("assets")
        try:
            ico = assets / "app.ico"
            if ico.exists():
                self.root.iconbitmap(default=str(ico))
        except Exception:
            pass
        try:
            png = assets / "app_64.png"
            if png.exists():
                self._icon_image = tk.PhotoImage(file=str(png))
                self.root.iconphoto(True, self._icon_image)
        except Exception:
            pass
    def _build_ui(self) -> None:
        header = ttk.Frame(self.root, padding=(14, 10, 14, 4))
        header.pack(fill="x")
        try:
            logo_path = resource("assets", "app_64.png")
            if logo_path.exists():
                self._logo = tk.PhotoImage(file=str(logo_path)).subsample(2, 2)
                ttk.Label(header, image=self._logo).pack(side="left", padx=(0, 10))
        except Exception:
            pass
        ttk.Label(header, text=APP_NAME, font=FONT_TITLE).pack(side="left")
        self.status_var = tk.StringVar(value="● 就绪")
        self.status_label = ttk.Label(header, textvariable=self.status_var,
                                      font=FONT_BOLD, foreground="#2e7d32")
        self.status_label.pack(side="right")

        body = ttk.Frame(self.root, padding=(14, 0, 14, 8))
        body.pack(fill="both", expand=True)

        # 用 grid 而不是 pack：设置页改成分类式之后不再有固定高度的滚动区，
        # 内容有多高标签页就要多高，pack 会让它把下面的日志区整个顶出窗口。
        # grid 能明确分配——标签页优先吃空间，日志区始终保底留一块。
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=3)
        body.rowconfigure(2, weight=2)

        notebook = ttk.Notebook(body)
        notebook.grid(row=0, column=0, sticky="nsew")
        notebook.add(self._build_course_tab(notebook), text="  课程  ")
        notebook.add(self._build_answer_tab(notebook), text="  AI 答题  ")
        notebook.add(self._build_settings_tab(notebook), text="  设置  ")

        self._build_controls(body)
        self._build_log(body)
        self.body = body

    def _build_course_tab(self, parent) -> ttk.Frame:
        tab = ttk.Frame(parent, padding=12)

        head = ttk.Frame(tab)
        head.grid(row=0, column=0, columnspan=4, sticky="ew")
        ttk.Label(head, text="课程首页／播放页／学习通考试地址").pack(side="left")
        ttk.Label(head, text="（右侧小框可写备注，方便认出是哪门课）",
                  style="Hint.TLabel").pack(side="left", padx=(6, 0))
        self.url_list = UrlList(tab)
        self.url_list.configure_row = self._configure_url_row
        self.url_list.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(4, 2))

        ttk.Label(tab, text="可填课程章节目录首页或播放页的完整地址；学习通独立考试可填正在作答的考试页地址。",
                  style="Hint.TLabel").grid(row=2, column=0, columnspan=4, sticky="w", pady=(0, 8))

        ttk.Label(tab, text="播放倍速").grid(row=3, column=0, sticky="w")
        self.speed_var = tk.DoubleVar(value=1.5)
        self.speed_scale = ttk.Scale(tab, from_=SPEED_MIN, to=SPEED_MAX,
                                     variable=self.speed_var,
                                     orient="horizontal", command=self._on_speed_change)
        self.speed_scale.grid(row=3, column=1, sticky="ew", padx=(8, 8))
        speed_value = ttk.Frame(tab)
        speed_value.grid(row=3, column=2, sticky="w")
        self.speed_label = ttk.Label(speed_value, text="1.5x", width=6, font=FONT_BOLD)
        self.speed_label.pack(side="left")
        self.speed_input_var = tk.StringVar(value="1.5")
        self._speed_input_job = None
        self._updating_speed_input = False
        self.speed_input_var.trace_add("write", self._queue_speed_input)
        self.speed_input = ttk.Entry(
            speed_value, textvariable=self.speed_input_var, width=5, justify="center")
        self.speed_input.pack(side="left", padx=(4, 0))
        self.speed_input.bind("<Return>", self._apply_speed_input)
        self.speed_input.bind("<FocusOut>", self._apply_speed_input)
        ttk.Label(speed_value, text="×", style="Hint.TLabel").pack(side="left", padx=(2, 0))
        self.mute_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(tab, text="静音播放", variable=self.mute_var).grid(
            row=3, column=3, sticky="w", padx=(12, 0))

        self.high_speed_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(tab, text=f"解锁 2 倍以上（最高 {SPEED_MAX_UNLOCKED:.0f}x）",
                        variable=self.high_speed_var,
                        command=self._apply_speed_ceiling).grid(
            row=4, column=0, columnspan=2, sticky="w")
        ttk.Label(tab, text="多数平台会把高倍速判成异常播放：轻则进度不计（白刷），重则触发人机验证",
                  style="Warn.TLabel").grid(row=5, column=0, columnspan=4,
                                            sticky="w", pady=(0, 8))

        ttk.Label(tab, text="限时（分钟）").grid(row=6, column=0, sticky="w")
        self.limit_var = tk.StringVar(value="0")
        ttk.Entry(tab, textvariable=self.limit_var, width=10).grid(
            row=6, column=1, sticky="w", padx=(8, 0))
        ttk.Label(tab, text="0 = 不限时。答题与验证等待不计入",
                  style="Hint.TLabel").grid(row=6, column=2, columnspan=2, sticky="w")

        sep = ttk.Separator(tab, orient="horizontal")
        sep.grid(row=7, column=0, columnspan=4, sticky="ew", pady=10)

        self._accounts = {"default": {"id": "default", "name": "默认账号", "platform": "",
                                      "username": "", "password": ""}}
        self._active_account_id = "default"
        ttk.Label(tab, text="编辑账号").grid(row=8, column=0, sticky="w")
        self.account_combo = ttk.Combobox(tab, state="readonly", values=["默认账号"], width=24)
        self.account_combo.current(0)
        self.account_combo.grid(row=8, column=1, sticky="w", padx=(8, 12))
        self.account_combo.bind("<<ComboboxSelected>>", self._switch_account)
        account_buttons = ttk.Frame(tab)
        account_buttons.grid(row=8, column=2, columnspan=2, sticky="w")
        for text, command in (("新增", self._add_account), ("改名", self._rename_account),
                              ("移除", self._remove_account)):
            ttk.Button(account_buttons, text=text, command=command).pack(side="left", padx=(0, 4))
        ttk.Label(tab, text="账号").grid(row=9, column=0, sticky="w")
        self.username_var = tk.StringVar()
        ttk.Entry(tab, textvariable=self.username_var, width=24).grid(
            row=9, column=1, sticky="w", padx=(8, 12))
        ttk.Label(tab, text="密码").grid(row=9, column=2, sticky="e")
        pwd_box = ttk.Frame(tab)
        pwd_box.grid(row=9, column=3, sticky="w", padx=(8, 0))
        self.password_var = tk.StringVar()
        self.password_entry = ttk.Entry(pwd_box, textvariable=self.password_var,
                                        width=20, show="●")
        self.password_entry.pack(side="left")
        self.show_pwd_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(pwd_box, text="显示", variable=self.show_pwd_var,
                        command=self._toggle_password_visibility).pack(
            side="left", padx=(6, 0))
        ttk.Label(tab, text="选中账号后填写账号、密码，再保存配置；地址右侧选择使用的账号。\n"
                  "默认账号首次为空；已有凭据来自本机配置。平台按地址自动识别。",
                  style="Hint.TLabel").grid(row=10, column=0, columnspan=4, sticky="w", pady=(4, 0))

        tab.columnconfigure(1, weight=1)
        return tab

    def _build_answer_tab(self, parent) -> ttk.Frame:
        tab = ttk.Frame(parent, padding=12)

        self.answer_enabled_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(tab, text="启用 AI 自动答题", variable=self.answer_enabled_var,
                        command=self._toggle_answer_fields).grid(
            row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(tab, text="关闭后遇到弹题只会暂停并提醒你，不会自动作答",
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 10))

        # 打包版在本机漏绘这些 ttk 字段名，改用 Tk 文本标签。
        self.answer_field_labels = []
        field_bg = ttk.Style().lookup("TFrame", "background")
        for title, row in (("服务商", 2), ("模型", 4), ("API Key", 6), ("接口地址", 8)):
            label = tk.Label(tab, text=title, font=FONT, fg="#000000", bg=field_bg)
            label.grid(row=row, column=0, sticky="w", pady=(8, 0) if row > 2 else 0)
            self.answer_field_labels.append(label)
        self.provider_var = tk.StringVar(value=providers.get("deepseek").label)
        self.provider_box = ttk.Combobox(tab, textvariable=self.provider_var,
                                         state="readonly",
                                         values=providers.labels())
        self.provider_box.grid(row=2, column=1, columnspan=3, sticky="ew", padx=(8, 0))
        self.provider_box.bind("<<ComboboxSelected>>", self._on_provider_change)

        # 标注这家的模型列表是否经过核对——内置列表会过时，得让用户知道
        self.provider_note = ttk.Label(tab, text="", style="Hint.TLabel")
        self.provider_note.grid(row=3, column=1, columnspan=3, sticky="w", padx=(8, 0))

        model_row = ttk.Frame(tab)
        model_row.grid(row=4, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(8, 0))
        model_row.columnconfigure(0, weight=1)
        self.model_var = tk.StringVar(value="deepseek-v4-flash")
        # 不设 readonly：列表里没有的型号可以直接手打
        self.model_box = ttk.Combobox(model_row, textvariable=self.model_var)
        self.model_box.grid(row=0, column=0, sticky="ew")
        self.fetch_models_btn = ttk.Button(model_row, text="获取模型列表",
                                           command=self._fetch_models)
        self.fetch_models_btn.grid(row=0, column=1, padx=(6, 0))
        ttk.Label(tab, text="内置列表只是省得手打，可能已过时；填好 Key 点「获取模型列表」"
                            "才是这家此刻真实提供的型号。也可以直接手输",
                  style="Hint.TLabel").grid(row=5, column=0, columnspan=4, sticky="w")

        self.api_key_var = tk.StringVar()
        self.api_key_entry = ttk.Entry(tab, textvariable=self.api_key_var, show="●")
        self.api_key_entry.grid(row=6, column=1, columnspan=2, sticky="ew",
                                padx=(8, 8), pady=(8, 0))
        self.show_key_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(tab, text="显示", variable=self.show_key_var,
                        command=self._toggle_key_visibility).grid(
            row=6, column=3, sticky="w", pady=(8, 0))
        ttk.Label(tab, text="不填也能播放；视频弹题可按选项重试，独立章节测验不会乱猜",
                  style="Hint.TLabel").grid(row=7, column=0, columnspan=4, sticky="w")

        self.base_url_var = tk.StringVar(value=providers.get("deepseek").base_url)
        self.base_url_entry = ttk.Entry(tab, textvariable=self.base_url_var)
        self.base_url_entry.grid(row=8, column=1, columnspan=3, sticky="ew",
                                 padx=(8, 0), pady=(8, 0))
        ttk.Label(tab, text="选服务商后自动填好。用中转站或本地 Ollama 就选「自定义」自己填",
                  style="Hint.TLabel").grid(row=9, column=0, columnspan=4, sticky="w")

        ttk.Separator(tab, orient="horizontal").grid(
            row=10, column=0, columnspan=4, sticky="ew", pady=10)

        self.retry_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(tab, text="视频弹题答错后自动换答案重试",
                        variable=self.retry_var,
                        command=self._toggle_retry_mode).grid(
            row=11, column=0, columnspan=4, sticky="w")
        ttk.Label(tab, text="只用于视频播放中弹出的题；独立章节测验只采用题库/AI答案一次，绝不枚举试错",
                  style="Hint.TLabel").grid(row=12, column=0, columnspan=4, sticky="w")
        ttk.Label(tab, text="章节测验使用防复制字体时会把渲染截图发给支持图片的模型",
                  style="Hint.TLabel").grid(row=13, column=0, columnspan=4, sticky="w")

        self.auto_submit_var = tk.BooleanVar(value=False)
        self.auto_submit_check = ttk.Checkbutton(
            tab, text="章节测验由 AI 全部填完后自动提交（不自动试错）",
            variable=self.auto_submit_var)
        self.auto_submit_check.grid(row=14, column=0, columnspan=4, sticky="w", padx=(20, 0))

        self.exam_auto_submit_var = tk.BooleanVar(value=False)
        self.exam_auto_submit_check = ttk.Checkbutton(
            tab, text="独立考试自动交卷（仅整卷题目均已作答；默认关闭）",
            variable=self.exam_auto_submit_var)
        self.exam_auto_submit_check.grid(row=15, column=0, columnspan=4, sticky="w", padx=(20, 0))

        ttk.Label(tab, text="题库缓存、日志、开机自启等选项在「设置」页",
                  style="Hint.TLabel").grid(row=16, column=0, columnspan=4,
                                            sticky="w", pady=(12, 0))

        tab.columnconfigure(1, weight=1)
        return tab

    def _build_settings_tab(self, parent) -> ttk.Frame:
        """设置页：左边分类，右边内容，像 Windows 设置那样。

        这么排是为了根治滚动残影。tkinter 在 Windows 上滚动时，
        窗口是先把屏幕像素搬过去、再等下一条消息重绘，
        中间那一瞬间必然拖影，Python 层拦不住（试过 WS_EX_COMPOSITED，会卡死）。
        一次只显示一类、每类都放得下，就根本不需要滚动，也就没有残影。

        分类是并列摊开的，不是折叠——点哪类看哪类，内容一眼看全。
        """
        tab = ttk.Frame(parent, padding=(10, 8))
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(0, weight=1)

        nav = ttk.Frame(tab)
        nav.grid(row=0, column=0, sticky="ns", padx=(0, 14))
        ttk.Separator(tab, orient="vertical").grid(row=0, column=0, sticky="nse")

        holder = ttk.Frame(tab)
        holder.grid(row=0, column=1, sticky="nsew")

        self.setting_pages: dict[str, ttk.Frame] = {}
        self.nav_items: dict[str, dict] = {}
        self.current_section = ""

        def section(title: str):
            page = ttk.Frame(holder)
            page.columnconfigure(1, weight=1)
            self.setting_pages[title] = page

            row = ttk.Frame(nav)
            row.pack(fill="x", pady=1)
            # 选中指示条，跟 Windows 设置一样在左侧亮一条
            bar = tk.Frame(row, width=4, background="#0b5cad")
            # 竖排八项，行高攒起来很可观：padding 每多 2px，整栏就高 32px，
            # 在小窗口 + 大字号下足以把最后一项「关于」挤出去
            label = ttk.Label(row, text=title, style="NavItem.TLabel",
                              padding=(12, 5), anchor="w", width=9)
            label.pack(side="left", fill="x", expand=True)
            for w in (row, label):
                w.bind("<Button-1>", lambda e, t=title: self._show_section(t))
                w.configure(cursor="hand2")
            self.nav_items[title] = {"row": row, "bar": bar, "label": label}
            return page

        # ---- 界面 ----
        box = section("界面")
        # 本机 EXE 切页时，主题控件会漏绘字号栏；这一行使用普通 Tk 控件。
        field_bg = ttk.Style().lookup("TFrame", "background")
        self.font_field_label = tk.Label(box, text="字号", font=FONT,
                                        fg="#000000", bg=field_bg)
        self.font_field_label.grid(row=0, column=0, sticky="w")
        font_row = tk.Frame(box, bg=field_bg)
        font_row.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(8, 0))
        font_row.columnconfigure(1, weight=1)

        self.font_minus = tk.Button(font_row, text="－", width=3, font=FONT,
                                    bg="#ffffff", activebackground="#e5f1fb",
                                    relief="solid", bd=1, padx=6, pady=2,
                                    command=lambda: self._nudge_font(-1))
        self.font_minus.grid(row=0, column=0)
        # 用 DoubleVar：拖动位置本来就是浮点，绑 IntVar 会让滑块在拖动时
        # 反复被取整"拽回"，手感就是拖不动。
        self.font_size_var = tk.DoubleVar(value=15.0)
        self._font_apply_job = None
        # 拖动过程中只更新数字，松手才真正换字体。
        # 每移动一像素就重建 style 会使滑块丢掉鼠标捕获。
        self.font_scale = _FontScale(font_row, self.font_size_var,
                                     self._on_font_slider_move, self._apply_font_size,
                                     bg=field_bg)
        self.font_scale.grid(row=0, column=1, sticky="ew", padx=(6, 6))
        # 只认拖滑块和 －／＋ 按钮，避免点空滑槽时字号意外改变。
        self.font_scale.bind("<Button-1>", self._on_scale_press, add="+")
        self.font_plus = tk.Button(font_row, text="＋", width=3, font=FONT,
                                   bg="#ffffff", activebackground="#e5f1fb",
                                   relief="solid", bd=1, padx=6, pady=2,
                                   command=lambda: self._nudge_font(1))
        self.font_plus.grid(row=0, column=2)
        self.font_size_label = tk.Label(font_row, text="15", width=3, font=FONT_BOLD,
                                        fg="#000000", bg=field_bg)
        self.font_size_label.grid(row=0, column=3, padx=(8, 0))

        ttk.Label(box, text="拖动滑块后松手生效，或用 －／＋ 逐级微调（10~24）",
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=3, sticky="w")

        self.on_top_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="窗口置顶", variable=self.on_top_var,
                        command=self._apply_on_top).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(box, text="刷课时把本窗口压在浏览器上面，方便随时看日志",
                  style="Hint.TLabel").grid(row=3, column=0, columnspan=3, sticky="w")

        # ---- 浏览器 ----
        box = section("浏览器")
        self.maximize_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(box, text="最大化打开浏览器（推荐）",
                        variable=self.maximize_var).grid(
            row=90, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(box, text="小窗下有些平台会切成移动端布局，导致认不出章节",
                  style="Hint.TLabel").grid(row=91, column=0, columnspan=3, sticky="w")
        ttk.Label(box, text="使用哪个").grid(row=0, column=0, sticky="w")
        self.channel_var = tk.StringVar(value="auto")
        self.channel_box = ttk.Combobox(box, textvariable=self.channel_var, width=16,
                                        state="readonly",
                                        values=["auto", "chrome", "edge", "chromium"])
        self.channel_box.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.channel_box.bind("<<ComboboxSelected>>",
                              lambda e: self._update_browser_hint())
        self.browser_hint = ttk.Label(box, text="", style="Hint.TLabel")
        self.browser_hint.grid(row=0, column=2, sticky="w", padx=(10, 0))

        ttk.Label(box, text="安装路径").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.exe_path_var = tk.StringVar()
        self.exe_path_var.trace_add("write", lambda *_: self._update_browser_hint())
        ttk.Entry(box, textvariable=self.exe_path_var).grid(
            row=1, column=1, sticky="ew", padx=(8, 8), pady=(8, 0))
        path_btns = ttk.Frame(box)
        path_btns.grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Button(path_btns, text="选文件", command=self._pick_browser).pack(side="left")
        ttk.Button(path_btns, text="选文件夹", command=self._pick_browser_dir).pack(
            side="left", padx=(4, 0))
        ttk.Label(box, text="留空即可，程序会自动找。填 exe 完整路径或安装文件夹都行",
                  style="Hint.TLabel").grid(row=2, column=0, columnspan=3, sticky="w")

        self.keep_open_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(box, text="出错时保留浏览器窗口（推荐）",
                        variable=self.keep_open_var).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(box, text="有任务失败时不自动关窗，方便看清是哪一步不对",
                  style="Hint.TLabel").grid(row=4, column=0, columnspan=3, sticky="w")

        self.headless_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="无头模式（不显示浏览器窗口）",
                        variable=self.headless_var).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(box, text="不建议开启：多数平台在窗口不可见时不累加播放进度",
                  style="Warn.TLabel").grid(row=6, column=0, columnspan=3, sticky="w")

        # ---- 运行 ----
        box = section("运行")
        self.autostart_var = tk.BooleanVar(value=self._is_autostart_enabled())
        ttk.Checkbutton(box, text="开机时自动启动本程序", variable=self.autostart_var,
                        command=self._apply_autostart).grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(box, text="做法是在「启动」文件夹放一个快捷方式，你随时可以自己删掉",
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=3, sticky="w")

        self.autorun_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="打开软件后自动开始刷课",
                        variable=self.autorun_var).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(box, text="配合开机自启即可开机就开刷。首次使用建议先关着",
                  style="Hint.TLabel").grid(row=3, column=0, columnspan=3, sticky="w")

        self.start_min_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="打开软件时直接最小化", variable=self.start_min_var).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self.beep_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(box, text="出现人机验证时响铃提醒", variable=self.beep_var).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.captcha_popup_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(box, text="并把窗口叫到最前", variable=self.captcha_popup_var).grid(
            row=7, column=0, columnspan=3, sticky="w")
        ttk.Label(box, text="匹配控件尝试本地验证，未通过时提醒人工处理；"
                            "窗口收在托盘里时，光响铃容易错过",
                  style="Hint.TLabel").grid(row=71, column=0, columnspan=3, sticky="w")


        ttk.Label(box, text="全部刷完后").grid(row=8, column=0, sticky="w", pady=(10, 0))
        self.on_finish_var = tk.StringVar(value="什么都不做")
        ttk.Combobox(box, textvariable=self.on_finish_var, width=18, state="readonly",
                     values=list(FINISH_ACTIONS)).grid(
            row=8, column=1, sticky="w", padx=(8, 0), pady=(10, 0))
        ttk.Label(box, text="挂机刷课时可以选关机。执行前有 60 秒倒计时，随时能取消",
                  style="Hint.TLabel").grid(row=9, column=0, columnspan=3, sticky="w")
        ttk.Label(box, text="登录／验证等待（秒）").grid(row=10, column=0, sticky="w", pady=(8, 0))
        self.login_timeout_var = tk.StringVar(value="120")
        ttk.Entry(box, textvariable=self.login_timeout_var, width=10).grid(
            row=10, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Label(box, text="超时继续下一地址",
                  style="Hint.TLabel").grid(row=10, column=2, sticky="w", padx=(8, 0))

        # ---- 网络 ----
        box = section("网络")
        ttk.Label(box, text="代理").grid(row=0, column=0, sticky="w")
        self.proxy_var = tk.StringVar()
        ttk.Entry(box, textvariable=self.proxy_var).grid(
            row=0, column=1, sticky="ew", padx=(8, 8))
        ttk.Button(box, text="测试", command=self._test_proxy).grid(row=0, column=2, sticky="w")
        ttk.Label(box, text="形如 http://127.0.0.1:7890。调用 Claude/GPT/Gemini 等海外模型时需要",
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=3, sticky="w")
        ttk.Label(box, text="只影响 AI 调用，不影响刷课的浏览器（浏览器走系统代理）",
                  style="Hint.TLabel").grid(row=2, column=0, columnspan=3, sticky="w")

        # ---- 日志 ----
        box = section("日志")
        ttk.Label(box, text="记录级别").grid(row=0, column=0, sticky="w")
        self.log_level_var = tk.StringVar(value="INFO")
        ttk.Combobox(box, textvariable=self.log_level_var, width=16, state="readonly",
                     values=["DEBUG", "INFO", "WARN", "ERROR"]).grid(
            row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(box, text="排查问题时调成 DEBUG，日志会详细很多",
                  style="Hint.TLabel").grid(row=0, column=2, sticky="w", padx=(10, 0))

        log_btns = ttk.Frame(box)
        log_btns.grid(row=1, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Button(log_btns, text="打开日志文件夹", command=self.open_logs).pack(side="left")
        ttk.Button(log_btns, text="清理旧日志", command=self._clean_logs).pack(
            side="left", padx=(8, 0))
        self.log_stat_label = ttk.Label(box, text="", style="Hint.TLabel")
        self.log_stat_label.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # ---- 题库 ----
        box = section("本地题库")
        ttk.Checkbutton(box, text="启用题库缓存", variable=self.cache_var).grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(box, text="试错验证出的正确答案会存进来，下次同题直接命中，一次都不用试",
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=3, sticky="w")
        cache_btns = ttk.Frame(box)
        cache_btns.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(cache_btns, text="刷新统计", command=self._refresh_cache_stat).pack(side="left")
        ttk.Button(cache_btns, text="清空题库", command=self._clear_cache).pack(
            side="left", padx=(8, 0))
        self.cache_stat_label = ttk.Label(box, text="", style="Hint.TLabel")
        self.cache_stat_label.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # ---- 数据与重置 ----
        box = section("数据")
        self.data_path_label = ttk.Label(box, text="", style="Hint.TLabel")
        self.data_path_label.grid(row=0, column=0, columnspan=3, sticky="w")
        data_btns = ttk.Frame(box)
        data_btns.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(data_btns, text="恢复默认设置", command=self._restore_defaults).pack(side="left")
        ttk.Button(data_btns, text="打开数据文件夹", command=self._open_data_dir).pack(
            side="left", padx=(8, 0))
        ttk.Button(data_btns, text="清除登录状态", command=self._clear_cookies).pack(
            side="left", padx=(8, 0))
        ttk.Button(data_btns, text="清空全部配置", command=self._reset_config).pack(
            side="left", padx=(8, 0))
        ttk.Label(box, text="「恢复默认设置」只重置各项开关，不会动你的课程地址、账号和 Key",
                  style="Hint.TLabel").grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(box, text="「清空全部配置」会连课程地址和 Key 一起删掉；换账号则点「清除登录状态」",
                  style="Hint.TLabel").grid(row=3, column=0, columnspan=3, sticky="w")

        # ---- 关于 ----
        box = section("关于")
        from . import __version__

        ttk.Label(box, text=f"{APP_NAME}  v{__version__}").grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(box, text="作者：LLL-svg-jpg").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.project_link = ttk.Label(box, text="GitHub：LLL-svg-jpg/Online-Course-Assistant",
                                      foreground="#1565c0", cursor="hand2", takefocus=True)
        self.project_link.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        for event in ("<Button-1>", "<Return>", "<space>"):
            self.project_link.bind(event, self._open_project)
        self.deps_label = ttk.Label(box, text="", style="Hint.TLabel")
        self.deps_label.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Button(box, text="检查运行依赖", command=self._show_deps).grid(
            row=4, column=0, sticky="w", pady=(8, 0))
        self.check_update_button = ttk.Button(box, text="检查更新", command=self._check_updates)
        self.check_update_button.grid(row=4, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        self.update_status_var = tk.StringVar(value="点击检查已发布的最新版本")
        ttk.Label(box, textvariable=self.update_status_var, style="Hint.TLabel").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(box, text="仅供学习研究。是否符合所在平台条款与学校规范，请自行判断",
                  style="Warn.TLabel").grid(row=6, column=0, columnspan=3,
                                            sticky="w", pady=(8, 0))

        self._refresh_settings_info()
        self._show_section("界面")
        return tab

    def _run_update_job(self, event: str, job) -> None:
        self.check_update_button.configure(state="disabled")

        def work():
            try:
                self._update_events.put((event, job()))
            except Exception as exc:
                self._update_events.put(("error", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _check_updates(self) -> None:
        self.update_status_var.set("正在检查更新…")
        self._run_update_job("checked", updates.check_latest)

    def _updates_checked(self, release: updates.Release) -> None:
        from . import __version__

        if not release.newer_than(__version__):
            text = (f"当前 v{__version__} 已是最新版本。" if release.version == __version__ else
                    f"当前 v{__version__} 高于已发布的 v{release.version}，暂无更新。")
            self.update_status_var.set(text)
            messagebox.showinfo("检查更新", text, parent=self.root)
            return
        self.update_status_var.set(f"发现新版 v{release.version}")
        if not is_frozen():
            messagebox.showinfo("发现新版", f"发现 v{release.version}。\n源码运行请从项目 Releases 下载新版，"
                                "不能用 EXE 更新包覆盖源码目录。", parent=self.root)
            return
        if not release.installable:
            messagebox.showinfo("发现新版", f"发现 v{release.version}，但尚无带 SHA-256 的 Windows x64 更新包。"
                                "\n请稍后再检查。", parent=self.root)
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("发现新版", "请先停止当前任务，再检查并安装更新。", parent=self.root)
            return
        if not messagebox.askyesno(
            "是否更新", f"当前 v{__version__}，新版 v{release.version}。\n"
            f"更新包约 {release.size / 1024 ** 2:.1f} MB。\n\n"
            "下载校验后将退出软件，在原目录更新并重新打开。\n"
            "保留账号、配置、登录状态和题库；旧程序另存备份。\n\n是否更新？", parent=self.root,
        ):
            return
        self._installing_update = True
        self.update_status_var.set("正在准备更新…")
        directory = app_dir()
        self._run_update_job("prepared", lambda: updates.prepare_update(
            release, directory, lambda text: self._update_events.put(("progress", text))))

    def _drain_update_events(self) -> None:
        while True:
            try:
                event, value = self._update_events.get_nowait()
            except queue.Empty:
                break
            if event == "progress":
                self.update_status_var.set(value)
                continue
            self.check_update_button.configure(state="normal")
            self._installing_update = False
            if event == "checked":
                self._updates_checked(value)
            elif event == "prepared":
                self.update_status_var.set("校验通过，正在退出并更新…")
                self.quit_app(prepared_update=value)
                if getattr(self, "_closing", False):
                    return
            else:
                self.update_status_var.set("检查或准备更新失败；旧程序未改动")
                messagebox.showerror("更新失败", str(value), parent=self.root)

    def _open_project(self, _event=None) -> None:
        from .config import detect_browser

        _, executable = detect_browser()
        if executable:
            subprocess.Popen([executable, PROJECT_URL])
        else:
            webbrowser.open(PROJECT_URL)

    def _show_section(self, title: str) -> None:
        """切换设置分类。一次只显示一页，所以永远不需要滚动。"""
        for name, page in self.setting_pages.items():
            if name == title:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()
        for name, item in self.nav_items.items():
            on = name == title
            item["label"].configure(
                style="NavItemOn.TLabel" if on else "NavItem.TLabel")
            if on:
                item["bar"].pack(side="left", fill="y", before=item["label"])
            else:
                item["bar"].pack_forget()
        self.current_section = title

    def _build_controls(self, parent) -> None:
        bar = ttk.Frame(parent, padding=(0, 10, 0, 6))
        bar.grid(row=1, column=0, sticky="ew")
        self.start_btn = ttk.Button(bar, text="▶  开始刷课", style="Run.TButton",
                                    command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="■  停止", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="保存配置", command=self.save).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="打开日志文件夹", command=self.open_logs).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="清空日志", command=self.clear_log).pack(side="right")

    def _build_log(self, parent) -> None:
        self.log_frame = ttk.Frame(parent)
        self.log_frame.grid(row=2, column=0, sticky="nsew")

        # 折叠条：整条都可点，收起后把空间让给上面的标签页
        header = ttk.Frame(self.log_frame, style="Card.TFrame", padding=(10, 6))
        header.pack(fill="x")
        self.log_collapsed = False
        self.log_arrow = ttk.Label(header, text="▼", width=3, style="Arrow.TLabel")
        self.log_arrow.pack(side="left")
        self.log_toggle_btn = ttk.Label(header, text="运行日志", style="CardTitle.TLabel")
        self.log_toggle_btn.pack(side="left")
        self.log_tail_label = ttk.Label(header, text="", style="Hint.TLabel")
        self.log_tail_label.pack(side="left", padx=(12, 0))
        for w in (header, self.log_arrow, self.log_toggle_btn, self.log_tail_label):
            w.bind("<Button-1>", lambda e: self._toggle_log())
            w.configure(cursor="hand2")

        frame = ttk.Frame(self.log_frame, padding=(0, 4, 0, 0))
        frame.pack(fill="both", expand=True)
        self.log_body = frame
        self.log_text = tk.Text(frame, font=FONT_LOG, wrap="word", state="disabled",
                                height=6, relief="solid", borderwidth=1,
                                background="#fcfcfc")
        self.log_text.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        sb.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=sb.set)
        for level, color in COLORS.items():
            self.log_text.tag_configure(level, foreground=color)
        self.log_text.tag_configure("WARN", foreground=COLORS["WARN"])
        self._append_log("SYSTEM", f"{APP_NAME} 已启动。填好课程地址后点击「开始刷课」。")

    # ---------------- 交互回调 ----------------

    def _on_speed_change(self, _value) -> None:
        speed = self.speed_var.get()
        self.speed_label.configure(text=f"{speed:.2f}x")
        if hasattr(self, "speed_input_var"):
            self._updating_speed_input = True
            try:
                self.speed_input_var.set(f"{speed:g}")
            finally:
                self._updating_speed_input = False
        # 超过 2 倍就把数字标红，提醒这一档已经不保险了
        self.speed_label.configure(
            foreground="#c62828" if speed > SPEED_MAX + 0.001 else "")

    def _queue_speed_input(self, *_args) -> None:
        if self._updating_speed_input:
            return
        if self._speed_input_job is not None:
            self.root.after_cancel(self._speed_input_job)
            self._speed_input_job = None
        raw = self.speed_input_var.get().strip().lower().rstrip("x×")
        try:
            if not math.isfinite(float(raw)):
                return
        except ValueError:
            return  # 输入中的空串/小数点不是最终值，失焦时再提示
        self._speed_input_job = self.root.after(500, self._apply_speed_input)

    def _apply_speed_input(self, _event=None) -> str | None:
        """把输入框中的数字应用到倍速滑块。"""
        if self._speed_input_job is not None:
            self.root.after_cancel(self._speed_input_job)
            self._speed_input_job = None
        previous = self.speed_var.get()
        raw = self.speed_input_var.get().strip().lower().rstrip("x×")
        try:
            speed = float(raw)
            if not math.isfinite(speed):
                raise ValueError
        except ValueError:
            self.root.bell()
            speed = self.speed_var.get()
        ceiling = SPEED_MAX_UNLOCKED if self.high_speed_var.get() else SPEED_MAX
        speed = max(SPEED_MIN, min(ceiling, speed))
        self.speed_var.set(speed)
        self._on_speed_change(None)
        worker = getattr(self, "worker", None)
        if abs(speed - previous) > 0.001 and worker and worker.is_alive():
            if self.save(silent=True):
                self._append_log("SYSTEM", f"播放倍速已切换为 {speed:g}×（最多 3 秒内应用）。")
        if _event is not None and getattr(_event, "keysym", "") == "Return":
            return "break"
        return None

    def _apply_speed_ceiling(self) -> None:
        """解锁开关变动时，调整倍速滑块的上限。

        关掉时要把已经调上去的倍速压回 2.0——否则开关关了、
        倍速却还留在 3.5，滑块显示和实际行为对不上。
        """
        ceiling = SPEED_MAX_UNLOCKED if self.high_speed_var.get() else SPEED_MAX
        self.speed_scale.configure(to=ceiling)
        if self.speed_var.get() > ceiling:
            self.speed_var.set(ceiling)
        self._on_speed_change(None)

    def _update_browser_hint(self) -> None:
        """把实际会用哪个浏览器显示出来，省得用户猜。"""
        from .config import detect_browser, find_installed_browser, resolve_browser_path

        choice = self.channel_var.get()
        raw_path = self.exe_path_var.get().strip().strip('"')
        if raw_path:
            resolved = resolve_browser_path(raw_path, choice)
            name = Path(resolved).name.lower() if resolved else ""
            expected = {"chrome": "chrome.exe", "edge": "msedge.exe",
                        "chromium": "chromium.exe"}.get(choice)
            if resolved and Path(resolved).is_file() and (not expected or name == expected):
                text = f"将使用 {Path(resolved).name}"
            else:
                text = "路径与选择不匹配或无效，启动会报错"
        elif choice == "auto":
            channel, path = detect_browser()
            text = f"将使用 {channel}" if path else "将尝试 Playwright Chromium"
        else:
            key = "msedge" if choice == "edge" else choice
            path = find_installed_browser(key)
            text = (f"将使用 {key}" if path else
                    f"将尝试 {key}；未安装时会报错，不会改用其他浏览器")
        self.browser_hint.configure(text=text)

    def vendor_key(self) -> str:
        """把界面上的中文显示名换回内部键。"""
        return providers.LABEL_TO_KEY.get(self.provider_var.get(), "custom")

    def _on_provider_change(self, _event=None) -> None:
        """切换服务商时自动带出接口地址与常用模型，省得用户去翻文档。"""
        self._remember_provider_key()
        self._active_provider_key = self.vendor_key()
        self.api_key_var.set(self._provider_keys.get(self._active_provider_key, ""))
        prov = providers.get(self.vendor_key())
        self.model_box.configure(values=prov.models)
        # 换了服务商，旧模型名几乎肯定不适用，一律换掉；
        # 没有内置模型的（自定义、豆包接入点）清空让用户自己填
        self.model_var.set(prov.models[0] if prov.models else "")
        self.base_url_var.set(prov.base_url)
        self.provider_note.configure(
            text=prov.note, style="Hint.TLabel")
        self._toggle_answer_fields()

    def _remember_provider_key(self) -> None:
        value = self.api_key_var.get().strip()
        if value:
            self._provider_keys[self._active_provider_key] = value
        else:
            self._provider_keys.pop(self._active_provider_key, None)

    def _check_model_match(self) -> str:
        """检查服务商与模型是否明显不搭。返回提示语，空串表示没问题。

        存在的理由：provider=anthropic 配 model=deepseek-chat 这种组合
        在界面上看不出毛病，但一调用就报错，不如启动前直接说清楚。
        """
        key = self.vendor_key()
        model = self.model_var.get().strip()
        if not model or not self.answer_enabled_var.get() or not self.api_key_var.get().strip():
            return ""
        is_claude = model.lower().startswith("claude")
        if key == "anthropic" and not is_claude:
            return f"服务商选的是 Claude，模型却填了「{model}」，调用会失败。"
        if key != "anthropic" and is_claude:
            return (f"服务商选的是「{providers.get(key).label}」，"
                    f"模型却填了 Claude 的「{model}」，调用会失败。")
        return ""

    def _fetch_models(self) -> None:
        """用用户自己的 Key 去问服务商现在有哪些模型。

        内置列表必然会过时，这个按钮才是准确来源。
        走后台线程：网络请求可能要几秒，卡住主线程界面会假死。
        """
        prov = providers.get(self.vendor_key())
        api_key = self.api_key_var.get().strip()
        base_url = self.base_url_var.get().strip()

        self.fetch_models_btn.configure(state="disabled", text="获取中...")
        self._append_log("SYSTEM", f"正在向 {prov.label} 查询可用模型...")

        def work() -> None:
            models, err = providers.fetch_models(
                base_url, api_key, prov.protocol, proxy=self.proxy_var.get().strip())
            self.root.after(0, lambda: self._on_models_fetched(models, err))

        threading.Thread(target=work, daemon=True, name="fetch-models").start()

    def _on_models_fetched(self, models: list[str], err: str) -> None:
        self.fetch_models_btn.configure(state="normal", text="获取模型列表")
        if err:
            self._append_log("ERROR", f"获取模型列表失败：{err}")
            messagebox.showerror("获取失败", err, parent=self.root)
            return

        self.model_box.configure(values=models)
        self._append_log("SYSTEM", f"获取成功，共 {len(models)} 个模型。")
        # 当前填的型号如果不在返回列表里，多半是过时了，明确告知
        current = self.model_var.get().strip()
        if current and current not in models:
            self._append_log("WARN", f"当前填的「{current}」不在该服务商的列表里，可能已下线。")
        if not current:
            self.model_var.set(models[0])
        self.provider_note.configure(
            text=f"已从服务商实时获取 {len(models)} 个模型", style="Hint.TLabel")

    def _toggle_key_visibility(self) -> None:
        self.api_key_entry.configure(show="" if self.show_key_var.get() else "●")

    def _toggle_password_visibility(self) -> None:
        self.password_entry.configure(show="" if self.show_pwd_var.get() else "●")

    def _toggle_answer_fields(self) -> None:
        state = "normal" if self.answer_enabled_var.get() else "disabled"
        self.api_key_entry.configure(state=state)
        needs_url = providers.get(self.vendor_key()).needs_base_url
        self.base_url_entry.configure(
            state="normal" if (needs_url and self.answer_enabled_var.get()) else "disabled")
        self.model_box.configure(state="normal" if self.answer_enabled_var.get() else "disabled")
        self.provider_box.configure(
            state="readonly" if self.answer_enabled_var.get() else "disabled")
        self._toggle_retry_mode()

    def _toggle_retry_mode(self) -> None:
        """视频弹题试错与章节测验提交互不影响。"""
        state = "normal" if self.answer_enabled_var.get() else "disabled"
        self.auto_submit_check.configure(state=state)
        self.exam_auto_submit_check.configure(state=state)

    # ---------------- 界面与网络设置 ----------------

    def _on_scale_press(self, event) -> str | None:
        """只有按在滑块本身上才放行。

        字号是全局设置，误触的代价是整个界面重排，所以宁可点不动。
        """
        if "slider" not in self.font_scale.identify(event.x, event.y):
            return "break"
        return None

    def _on_font_slider_move(self, _value=None) -> None:
        """拖动过程中只刷新数字，不动字体——重绘会把滑块从鼠标下拽走。"""
        self.font_size_label.configure(text=str(int(round(self.font_size_var.get()))))

    def _nudge_font(self, delta: int) -> None:
        """－／＋ 逐级微调。滑块拖不准时用这个。"""
        size = int(round(self.font_size_var.get())) + delta
        size = max(10, min(24, size))
        self.font_size_var.set(float(size))
        self._apply_font_bar(size)
        if self._font_apply_job is not None:
            self.root.after_cancel(self._font_apply_job)
        # 连点只调整字号栏；停止点击后再统一重排全部页面。
        self._font_apply_job = self.root.after(120, lambda: self._apply_font_size(defer_layout=True))

    def _apply_font_bar(self, size: int) -> None:
        for widget in (self.font_field_label, self.font_minus, self.font_plus):
            widget.configure(font=("Microsoft YaHei UI", size))
        self.font_size_label.configure(text=str(size), font=("Microsoft YaHei UI", size, "bold"))

    def _on_font_size_change(self, _value=None) -> None:
        """兼容旧调用点，等同于立即应用。"""
        self._apply_font_size()

    def _apply_font_size(self, *, defer_layout: bool = False) -> None:
        """真正换字体。

        ttk 控件的字体走 Style，改 Style 就能一次性影响全部；
        但 tk.Text、tk.Label 和 Entry 不吃 Style，得单独设。
        """
        if self._font_apply_job is not None:
            self.root.after_cancel(self._font_apply_job)
            self._font_apply_job = None
        size = int(round(self.font_size_var.get()))
        self._apply_font_bar(size)
        family = "Microsoft YaHei UI"
        style = ttk.Style()
        style.configure(".", font=(family, size))
        style.configure("TLabelframe.Label", font=(family, size, "bold"))
        style.configure("TNotebook.Tab", font=(family, size), padding=(18, 8))
        style.configure("TButton", font=(family, size), padding=(10, 6))
        style.configure("Run.TButton", font=(family, size + 1, "bold"), padding=(16, 8))
        style.configure("Hint.TLabel", font=(family, max(9, size - 3)), foreground="#666")
        style.configure("Warn.TLabel", font=(family, max(9, size - 3)), foreground="#a33")
        # 这几个是显式指定字体的，不跟着 "." 走，得一起改，否则调字号时它们纹丝不动
        style.configure("CardTitle.TLabel", font=(family, size, "bold"))
        style.configure("Arrow.TLabel", font=(family, max(18, size + 3)),
                        foreground="#000000")
        style.configure("NavItem.TLabel", font=(family, size), foreground="#333")
        style.configure("NavItemOn.TLabel", font=(family, size, "bold"),
                        foreground="#0b5cad", background="#e5eefa")
        style.configure(READONLY_COMBO_STYLE, font=(family, size))
        for label in self.answer_field_labels:
            label.configure(font=(family, size))
        try:
            self.log_text.configure(font=("Consolas", max(9, size - 1)))
        except Exception:
            pass
        # 输入框和下拉框的高度不吃 Style 的字体，vista 主题给它们钉死了 23px，
        # 只有显式 configure(font=) 才会长高。不统一设的话，
        # 课程页那个设过字体的地址框有 33px，别处的框还是 23px，一眼就参差不齐
        self._apply_font_to_inputs(self.root, (family, size))
        for menu in (getattr(self, "_edit_menu", None), getattr(self, "_tray_menu_tk", None)):
            if menu is not None:
                try:
                    menu.configure(font=(family, size))
                except tk.TclError:
                    pass
        if defer_layout:
            # 让 Tk 先处理正常布局，避免把整窗绘制挤进一次按钮事件。
            self.root.after(30, self._update_minsize)
        else:
            self._update_minsize()

    def _apply_font_to_inputs(self, widget, font) -> None:
        family, size = font
        # 下拉列表要的是 Tcl 的字体写法，家族名有空格必须用花括号裹住
        tcl_font = "{%s} %d" % (family, size)
        for child in widget.winfo_children():
            if isinstance(child, (ttk.Entry, ttk.Combobox, ttk.Spinbox)):
                try:
                    child.configure(font=font)
                except tk.TclError:
                    pass
            if isinstance(child, ttk.Combobox):
                # 点开后弹出来的那个列表是原生 Listbox，不归 ttk 管，
                # 默认一直用系统的 TkTextFont，所以调字号时它纹丝不动、小得突兀
                try:
                    popdown = child.tk.call("ttk::combobox::PopdownWindow", child)
                    child.tk.call(popdown + ".f.l", "configure", "-font", tcl_font)
                except tk.TclError:
                    pass
            self._apply_font_to_inputs(child, font)

    # ---------------- 系统托盘 ----------------

    def _start_tray(self) -> None:
        """在系统托盘放一个图标。

        软件跑起来后托盘里能看见它，也就知道它还活着；
        窗口最小化或被别的窗口盖住时，从这里能直接叫回来。
        托盘线程是 daemon，主线程一退它就跟着没，不会留后台。
        """
        self.tray = None
        try:
            import pystray
            from PIL import Image
        except ImportError:
            return          # 没装就安静地不做托盘，不影响主功能

        try:
            path = resource("assets", "app_64.png")
            image = Image.open(str(path)) if path.exists() else None
        except Exception:
            image = None
        if image is None:
            return

        def call(fn):
            # 托盘菜单的回调跑在托盘线程里，碰 tkinter 必须切回主线程
            return lambda *_: self.root.after(0, fn)

        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口", call(self._show_window), default=True),
            pystray.MenuItem("开始刷课", call(self.start)),
            pystray.MenuItem("停止", call(self.stop)),
            pystray.Menu.SEPARATOR,
            # 这里必须是 quit_app 而不是 on_close：on_close 只是收进托盘，
            # 接到托盘上就成了「点退出还是退不掉」
            pystray.MenuItem("退出", call(self.quit_app)),
        )
        try:
            self.tray = pystray.Icon("OnlineCourseAssistant", image, APP_NAME, menu)
            threading.Thread(target=self.tray.run, daemon=True,
                             name="coursemate-tray").start()
        except Exception:
            self.tray = None

    def _show_window(self) -> None:
        """从托盘把窗口叫回来并置于最前。"""
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass

    def _stop_tray(self) -> None:
        """移除托盘图标。

        不主动 stop 的话，图标会一直赖在托盘里，
        直到鼠标划过去才消失——看着就像没退干净。
        """
        tray = getattr(self, "tray", None)
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                pass
            self.tray = None

    # ---------------- 右键编辑菜单 ----------------

    MENU_CUT, MENU_COPY, MENU_PASTE, MENU_ALL = "剪切", "复制", "粘贴", "全选"

    def _attach_edit_menu(self) -> None:
        """给所有输入框和日志区加右键菜单。

        只能 Ctrl+C / Ctrl+V 太不直观——右键点下去什么都没有，
        很容易以为这软件压根不支持复制粘贴。

        用类绑定而不是逐个绑：后来点「＋」加出来的地址行也自动就有。
        """
        # tk.Menu 是原生菜单，不吃 ttk 的 Style，字体得自己给，
        # 否则右键弹出来的一小块字比界面上其他地方明显小一号
        m = self._edit_menu = tk.Menu(self.root, tearoff=0, font=FONT)
        self._menu_target = None
        m.add_command(label=self.MENU_CUT, command=lambda: self._edit_action("Cut"))
        m.add_command(label=self.MENU_COPY, command=lambda: self._edit_action("Copy"))
        m.add_command(label=self.MENU_PASTE, command=lambda: self._edit_action("Paste"))
        m.add_separator()
        m.add_command(label=self.MENU_ALL, command=self._edit_select_all)

        for cls in ("TEntry", "TCombobox", "TSpinbox", "Entry", "Text"):
            self.root.bind_class(cls, "<Button-3>", self._popup_edit_menu, add="+")

    @staticmethod
    def _widget_text(widget) -> str:
        if isinstance(widget, tk.Text):
            return widget.get("1.0", "end-1c")
        try:
            return widget.get()
        except tk.TclError:
            return ""

    @staticmethod
    def _widget_has_selection(widget) -> bool:
        try:
            if isinstance(widget, tk.Text):
                return bool(widget.tag_ranges("sel"))
            return bool(widget.selection_present())
        except tk.TclError:
            return False

    def _sync_edit_menu(self, widget) -> None:
        """按目标控件的当前状态决定哪几项可点。

        灰掉比留着更好：点了没反应会让人以为软件坏了。
        """
        state = str(widget.cget("state"))
        # 日志区是 disabled 的只读文本，但复制必须能用
        editable = state not in ("readonly", "disabled")
        has_sel = self._widget_has_selection(widget)
        try:
            can_paste = bool(self.root.clipboard_get())
        except tk.TclError:
            can_paste = False       # 剪贴板是空的，或者里面不是文本

        m = self._edit_menu
        m.entryconfigure(self.MENU_CUT,
                         state="normal" if (has_sel and editable) else "disabled")
        m.entryconfigure(self.MENU_COPY, state="normal" if has_sel else "disabled")
        m.entryconfigure(self.MENU_PASTE,
                         state="normal" if (can_paste and editable) else "disabled")
        m.entryconfigure(self.MENU_ALL,
                         state="normal" if self._widget_text(widget) else "disabled")

    def _popup_edit_menu(self, event) -> None:
        widget = event.widget
        self._menu_target = widget
        self._sync_edit_menu(widget)
        try:
            self._edit_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._edit_menu.grab_release()

    def _edit_action(self, action: str) -> None:
        widget = self._menu_target
        if widget is None:
            return
        if action == "Paste" and not isinstance(widget, tk.Text):
            # 选中一段再粘贴时，本意是替换掉它。
            # 有的 Tk 版本会把新内容插在旁边而不是覆盖，这里先删干净
            try:
                if widget.selection_present():
                    widget.delete("sel.first", "sel.last")
            except tk.TclError:
                pass
        try:
            widget.focus_set()
        except tk.TclError:
            pass
        widget.event_generate(f"<<{action}>>")

    def _edit_select_all(self) -> None:
        widget = self._menu_target
        if widget is None:
            return
        try:
            if isinstance(widget, tk.Text):
                widget.tag_add("sel", "1.0", "end-1c")
            else:
                widget.select_range(0, "end")
                widget.icursor("end")
        except tk.TclError:
            pass

    def _no_select_on_traverse(self) -> None:
        """Tab 键或切换标签页时，别把输入框的内容整条选中。

        Tk 给 TEntry 的默认类绑定就是 `%W selection range 0 end; %W icursor end`
        ——焦点一 traverse 进来就全选。于是每次切到课程页，
        那条长长的课程地址整条泛蓝，非得再点一下才褪掉。
        这里把它换成只把光标挪到末尾，不选中。

        用类绑定而不是逐个绑：以后动态加的地址行也自动生效。
        """
        def on_traverse(event):
            try:
                event.widget.selection_clear()
                event.widget.icursor("end")
            except tk.TclError:
                pass    # 只读的下拉框没有光标，忽略即可

        for cls in ("TEntry", "TCombobox", "TSpinbox"):
            self.root.bind_class(cls, "<<TraverseIn>>", on_traverse)

    def _configure_url_row(self, row) -> None:
        self._apply_font_to_inputs(row, (FONT[0], int(round(self.font_size_var.get()))))
        self._tidy_comboboxes(row)

    def _tidy_comboboxes(self, widget) -> None:
        """选完之后清掉文字选区；取消下拉由原生列表的收起事件处理。"""
        for child in widget.winfo_children():
            if isinstance(child, ttk.Combobox):
                if child.instate(("readonly",)):
                    child.configure(style=READONLY_COMBO_STYLE)
                child.bind("<<ComboboxSelected>>",
                           lambda e: self._clear_combobox_highlight(e.widget), add="+")
            self._tidy_comboboxes(child)

    def _on_combobox_unmap(self, event) -> None:
        path = str(event.widget)
        if not path.endswith(".popdown"):
            return
        try:
            widget = self.root.nametowidget(path.removesuffix(".popdown"))
        except (tk.TclError, KeyError):
            return
        self._clear_combobox_highlight(widget)
        self.root.after_idle(self._clear_combobox_highlight, widget)

    def _clear_combobox_highlight(self, widget) -> None:
        try:
            if widget.instate(("pressed",)):
                return
            if str(self.root.tk.call("focus")) == str(widget):
                self.root.focus_set()
            widget.selection_clear()
        except tk.TclError:
            pass

    def _update_minsize(self) -> None:
        """字号越大，所有标签页和日志区都必须完整可见。

        不能只按设置页估算：AI 答题页底部的说明文字也会被日志区挤住。
        直接使用 Tk 已计算的整窗请求高度，才能同时覆盖课程、AI 和设置页。
        """
        size = int(round(self.font_size_var.get()))
        self.root.update_idletasks()
        want = max(700, 660 + size * 9, self.root.winfo_reqheight())
        # 但别把最小高度顶到超出屏幕：小屏笔记本上那样会连窗口都摆不下。
        # 到那一步只能请用户自己把字号调小，总比窗口拖不动强
        cap = max(700, self.root.winfo_screenheight() - 90)
        self.root.minsize(1000, min(want, cap))

    def _apply_on_top(self) -> None:
        try:
            self.root.attributes("-topmost", bool(self.on_top_var.get()))
        except Exception:
            pass

    def _test_proxy(self) -> None:
        """测试代理是否可用。空代理则测直连。"""
        proxy = self.proxy_var.get().strip()
        self._append_log("SYSTEM", f"正在测试{'代理 ' + proxy if proxy else '直连'}...")

        def work() -> None:
            msg = self._probe_network(proxy)
            self.root.after(0, lambda: self._append_log("SYSTEM", msg))
            self.root.after(0, lambda: messagebox.showinfo("网络测试", msg, parent=self.root))

        threading.Thread(target=work, daemon=True, name="proxy-test").start()

    @staticmethod
    def _probe_network(proxy: str) -> str:
        try:
            import httpx
        except ImportError:
            return "缺少 httpx，无法测试。" + CourseMateGUI._dependency_repair_hint()
        kwargs = {"timeout": 12}
        if proxy:
            kwargs["proxy"] = proxy
        results = []
        for name, url in (("国内（DeepSeek）", "https://api.deepseek.com"),
                          ("海外（Anthropic）", "https://api.anthropic.com")):
            try:
                r = httpx.get(url, **kwargs)
                results.append(f"{name}：可达（HTTP {r.status_code}）")
            except Exception as exc:
                results.append(f"{name}：不可达（{type(exc).__name__}）")
        return "\n".join(results)

    def _run_finish_action(self) -> None:
        """全部刷完后的收尾动作。关机这类事必须留够反悔的余地。"""
        action = FINISH_ACTIONS.get(self.on_finish_var.get(), "none")
        if action == "none":
            return
        label = self.on_finish_var.get()
        self._append_log("WARN", f"全部完成，即将执行：{label}（60 秒后）", shift=True)
        self._countdown_then(action, label, 60)

    def _countdown_then(self, action: str, label: str, seconds: int) -> None:
        win = tk.Toplevel(self.root)
        win.title("即将执行")
        win.transient(self.root)
        win.grab_set()
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=20)
        frame.pack()
        ttk.Label(frame, text=f"刷课已全部完成，即将{label}。",
                  font=FONT_BOLD).pack(anchor="w")
        tip = ttk.Label(frame, text="", font=FONT)
        tip.pack(anchor="w", pady=(8, 12))
        state = {"left": seconds, "cancelled": False}

        def cancel() -> None:
            state["cancelled"] = True
            self._append_log("SYSTEM", f"已取消「{label}」。")
            win.destroy()

        ttk.Button(frame, text="取消", command=cancel).pack(anchor="e")
        win.protocol("WM_DELETE_WINDOW", cancel)

        def tick() -> None:
            if state["cancelled"]:
                return
            if state["left"] <= 0:
                win.destroy()
                self._execute_finish(action)
                return
            tip.configure(text=f"{state['left']} 秒后执行，点「取消」可中止。")
            state["left"] -= 1
            win.after(1000, tick)

        tick()

    def _execute_finish(self, action: str) -> None:
        import subprocess

        try:
            if action == "quit":
                # 「刷完退出本程序」要的是真退出。
                # 走 on_close 只会收进托盘，人回来一看软件还在，等于没生效
                self.quit_app()
            elif action == "sleep":
                # 先关掉休眠才会真的睡眠，否则会进休眠
                subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                               check=False)
            elif action == "shutdown":
                subprocess.run(["shutdown", "/s", "/t", "5"], check=False)
                self._append_log("SYSTEM", "已发出关机指令。想反悔可在命令行执行 shutdown /a")
        except Exception as exc:
            self._append_log("ERROR", f"执行收尾动作失败：{exc}")

    # ---------------- 设置页功能 ----------------

    def _startup_link(self) -> Path:
        """「启动」文件夹里的快捷方式路径。

        用快捷方式而不是写注册表 Run 键：用户能在开始菜单的启动项里
        直接看到并自己删掉，不会变成一个偷偷摸摸的自启动。
        """
        startup = Path(os.path.expandvars(
            r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"))
        legacy = startup / "CourseMate 刷课助手.lnk"
        return legacy if legacy.exists() else startup / f"{APP_NAME}.lnk"

    def _is_autostart_enabled(self) -> bool:
        try:
            return self._startup_link().exists()
        except Exception:
            return False

    def _heal_autostart(self) -> None:
        """程序被挪过位置后，修好开机自启的快捷方式。

        快捷方式里存的是绝对路径，用户把整个文件夹搬走之后，
        开机自启会静默失效——开机时什么都不发生，也没有任何提示。
        所以每次启动检查一遍，指向不对就按当前位置重建。
        """
        if not self._is_autostart_enabled():
            return
        try:
            import subprocess
            import sys

            link = self._startup_link()
            ps = f'$w=New-Object -ComObject WScript.Shell;'                 f'$s=$w.CreateShortcut("{link}");'                 f'Write-Output $s.TargetPath;Write-Output $s.Arguments'
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               capture_output=True, text=True, timeout=15)
            lines = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
            target = lines[0] if lines else ""
            expected = sys.executable if getattr(sys, "frozen", False) else str(
                Path(sys.executable).with_name("pythonw.exe"))
            # 打包版直接比 exe 路径；源码版还要看参数里的脚本路径对不对
            stale = target.lower() != expected.lower()
            if not stale and not getattr(sys, "frozen", False):
                args = lines[1] if len(lines) > 1 else ""
                stale = str(app_dir() / "OnlineCourseAssistant.pyw").lower() not in args.lower()
            if stale:
                self._apply_autostart()
                self._append_log("SYSTEM", "检测到程序位置变化，已更新开机自启动的快捷方式。")
        except Exception:
            pass

    def _apply_autostart(self) -> None:
        link = self._startup_link()
        want = self.autostart_var.get()
        try:
            if not want:
                link.unlink(missing_ok=True)
                self._append_log("SYSTEM", "已取消开机自启动。")
                return

            import sys

            link.parent.mkdir(parents=True, exist_ok=True)
            # 打包后直接指向 exe；源码运行时指向 pythonw + 脚本
            if getattr(sys, "frozen", False):
                target, args = sys.executable, ""
            else:
                target = str(Path(sys.executable).with_name("pythonw.exe"))
                args = f'"{app_dir() / "OnlineCourseAssistant.pyw"}"'

            import subprocess

            ps = (
                f'$w = New-Object -ComObject WScript.Shell; '
                f'$s = $w.CreateShortcut("{link}"); '
                f'$s.TargetPath = "{target}"; '
                f'$s.Arguments = \'{args}\'; '
                f'$s.WorkingDirectory = "{app_dir()}"; '
                f'$s.Save()'
            )
            result = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                    capture_output=True, timeout=20)
            if result.returncode:
                raise RuntimeError("创建开机自启快捷方式失败")
            if link.exists():
                self._append_log("SYSTEM", f"已设置开机自启动：{link}")
            else:
                raise RuntimeError("快捷方式未生成")
        except Exception as exc:
            self.autostart_var.set(self._is_autostart_enabled())
            messagebox.showerror("设置失败", f"无法修改开机自启动：\n{exc}", parent=self.root)

    def _clean_logs(self) -> None:
        log_dir = app_dir() / "runtime" / "logs"
        files = sorted(log_dir.glob("*.log")) if log_dir.exists() else []
        # 保留最近 5 个，其余删掉——日志只在出问题时有用，留太多纯占地方
        stale = files[:-5] if len(files) > 5 else []
        dumps = list((app_dir() / "runtime").glob("page_dump_*.txt"))
        crashes = list((app_dir() / "runtime").glob("crash_*.log"))
        total = len(stale) + len(dumps) + len(crashes)
        if not total:
            messagebox.showinfo("清理日志", "没有需要清理的旧文件。", parent=self.root)
            return
        if not messagebox.askokcancel(
                "清理日志", f"将删除 {total} 个旧文件（最近 5 份日志会保留）。继续吗？",
                parent=self.root):
            return
        removed = 0
        for f in stale + dumps + crashes:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
        self._append_log("SYSTEM", f"已清理 {removed} 个旧文件。")
        self._refresh_settings_info()

    def _cache_db(self) -> Path:
        return app_dir() / "runtime" / "answers.db"

    def _refresh_cache_stat(self) -> None:
        try:
            from .answer.cache import AnswerCache

            cache = AnswerCache(self._cache_db(), enabled=True)
            total, hits = cache.stats()
            cache.close()
            self.cache_stat_label.configure(
                text=f"已收录 {total} 道题，累计命中 {hits} 次")
        except Exception as exc:
            self.cache_stat_label.configure(text=f"读取失败：{exc}")

    def _clear_cache(self) -> None:
        db = self._cache_db()
        if not db.exists():
            messagebox.showinfo("清空题库", "题库还是空的。", parent=self.root)
            return
        if not messagebox.askokcancel(
                "清空题库",
                "将删除本地题库里的全部题目和答案。\n\n"
                "这些是试错验证过的正确答案，删了下次遇到同样的题要重新试一遍。\n"
                "确定要清空吗？", parent=self.root):
            return
        try:
            db.unlink()
            self._append_log("SYSTEM", "本地题库已清空。")
            self._refresh_cache_stat()
        except OSError as exc:
            messagebox.showerror("清空失败", str(exc), parent=self.root)

    def _open_data_dir(self) -> None:
        try:
            os.startfile(str(app_dir()))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showinfo("数据位置", f"{app_dir()}\n\n({exc})", parent=self.root)

    def _clear_cookies(self) -> None:
        from .browser import account_storage_path

        path = account_storage_path(self._accounts[self._active_account_id])
        prefix = path.name.split("-", 1)[0]
        paths = list(path.parent.glob(f"{prefix}-*.json"))
        legacy = app_dir() / "runtime" / "cookies.json"
        if self._active_account_id == "default" and legacy.exists():
            paths.append(legacy)
        if not paths:
            messagebox.showinfo("清除登录状态", "当前没有保存的登录状态。", parent=self.root)
            return
        if not messagebox.askokcancel(
                "清除登录状态",
                "将清除当前编辑账号的登录状态；下次需要重新登录。确定吗？", parent=self.root):
            return
        try:
            for path in paths:
                path.unlink()
            self._append_log("SYSTEM", "登录状态已清除，下次运行需重新登录。")
        except OSError as exc:
            messagebox.showerror("清除失败", str(exc), parent=self.root)

    # 各项设置的出厂值。集中放一处，免得改了默认值忘了同步这里。
    DEFAULTS = {
        "font_size": 15, "always_on_top": False, "channel": "auto",
        "executable_path": "", "maximize": True, "keep_open_on_failure": True,
        "headless": False, "autostart": False, "autorun": False,
        "start_minimized": False, "beep_on_captcha": True, "on_finish": "none",
        "proxy": "", "log_level": "INFO", "cache": True,
        "speed": 1.5, "mute": True, "limit_max_minutes": 0,
        "allow_high_speed": False, "captcha_popup": True,
        "answer_enabled": True, "retry_until_correct": True, "auto_submit": False,
        "exam_auto_submit": False,
        "login_timeout_seconds": 120,
    }

    def _restore_defaults(self, confirm: bool = True) -> None:
        """把各项设置恢复出厂值，但保留用户自己填的内容。

        课程地址、账号密码、API Key 属于用户数据而不是"设置"，
        恢复默认时把它们一并清掉是很讨厌的行为，所以一律不动。
        要连数据一起清，用旁边的「清空全部配置」。
        """
        if confirm and not messagebox.askokcancel(
                "恢复默认设置",
                "会把字号、浏览器、答题、日志、网络等各项开关恢复到出厂值。\n\n"
                "你的课程地址、账号密码、API Key 都会保留。\n\n"
                "确定吗？", parent=self.root):
            return

        d = self.DEFAULTS
        self.font_size_var.set(float(d["font_size"]))
        self._apply_font_size()
        self.on_top_var.set(d["always_on_top"])
        self._apply_on_top()

        self.channel_var.set(d["channel"])
        self.exe_path_var.set(d["executable_path"])
        self.maximize_var.set(d["maximize"])
        self.keep_open_var.set(d["keep_open_on_failure"])
        self.headless_var.set(d["headless"])
        self._update_browser_hint()

        self.autorun_var.set(d["autorun"])
        self.start_min_var.set(d["start_minimized"])
        self.beep_var.set(d["beep_on_captcha"])
        self.on_finish_var.set(FINISH_LABELS.get(d["on_finish"], "什么都不做"))
        self.proxy_var.set(d["proxy"])
        self.log_level_var.set(d["log_level"])
        self.cache_var.set(d["cache"])

        self.speed_var.set(d["speed"])
        self._on_speed_change(None)
        self.mute_var.set(d["mute"])
        self.high_speed_var.set(d["allow_high_speed"])
        self.captcha_popup_var.set(d["captcha_popup"])
        self.login_timeout_var.set(str(d["login_timeout_seconds"]))
        self._apply_speed_ceiling()
        self.limit_var.set(str(int(d["limit_max_minutes"])))

        self.answer_enabled_var.set(d["answer_enabled"])
        self.retry_var.set(d["retry_until_correct"])
        self.auto_submit_var.set(d["auto_submit"])
        self.exam_auto_submit_var.set(d["exam_auto_submit"])
        self._toggle_answer_fields()

        # 开机自启是系统层面的，单独处理：只在当前确实开着时才去掉
        if self.autostart_var.get() and not d["autostart"]:
            self.autostart_var.set(False)
            self._apply_autostart()

        if confirm:
            self._append_log("SYSTEM", "各项设置已恢复默认（课程地址与账号未改动）。")
            messagebox.showinfo("已恢复", "各项设置已恢复默认值。\n点「保存配置」后生效。",
                                parent=self.root)

    def _reset_config(self) -> None:
        if not messagebox.askokcancel(
                "恢复默认设置",
                "会清空所有设置，包括课程地址、账号密码、API Key。\n\n"
                "本地题库和登录状态不受影响。确定吗？", parent=self.root):
            return
        try:
            CONFIG_PATH.unlink(missing_ok=True)
        except OSError as exc:
            messagebox.showerror("重置失败", str(exc), parent=self.root)
            return
        self._restore_defaults(confirm=False)
        self.url_list.set_items([])
        self.username_var.set("")
        self.password_var.set("")
        self._accounts = {"default": {"id": "default", "name": "默认账号", "platform": "",
                                      "username": "", "password": ""}}
        self._active_account_id = "default"
        self._refresh_accounts()
        self._load_current_account()
        self.api_key_var.set("")
        self._provider_keys.clear()
        self._active_provider_key = "anthropic"
        self.provider_var.set(providers.get("anthropic").label)
        self.model_var.set("")
        self.base_url_var.set("")
        messagebox.showinfo("已清空", "配置已删除，界面中的课程和凭据也已清空。", parent=self.root)
        self._append_log("SYSTEM", "配置文件已删除。")

    @staticmethod
    def _dependency_version(module_name: str) -> str:
        """以实际能否导入为准；打包版不一定保留 pip 的 metadata。"""
        try:
            import importlib
            import importlib.metadata as md

            module = importlib.import_module(module_name)
            try:
                return md.version(module_name)
            except Exception:
                return str(getattr(module, "__version__", "") or "已内置")
        except Exception:
            return ""

    @staticmethod
    def _dependency_repair_hint() -> str:
        if is_frozen():
            return ("打包版依赖已内置；若仍报告缺失，请重新下载完整发布包。"
                    "安装依赖.bat 仅供源码运行，不能修复 EXE。")
        return ("源码运行缺少依赖时，请运行「安装依赖.bat」，或执行 "
                "python -m pip install -r requirements.txt。")

    def _show_deps(self) -> None:
        rows = []
        missing = False
        for mod, why in (("playwright", "浏览器驱动，必需"),
                         ("anthropic", "调用 Claude"),
                         ("httpx", "调用国内大模型")):
            version = self._dependency_version(mod)
            if version:
                rows.append(f"  ✓ {mod} {version}  —— {why}")
            else:
                missing = True
                rows.append(f"  ✗ {mod} 未安装  —— {why}")
        text = "\n".join(rows)
        self.deps_label.configure(text=text.replace("  ", "").replace("\n", "   "))
        if self.answer_enabled_var.get():
            key_state = "已填写" if self.api_key_var.get().strip() else "未填写（无法实际调用）"
            text += f"\n\n当前 AI：{providers.get(self.vendor_key()).label}\nAPI Key：{key_state}"
        if missing:
            text += "\n\n" + self._dependency_repair_hint()
        messagebox.showinfo("运行依赖", text, parent=self.root)

    def _refresh_settings_info(self) -> None:
        """刷新设置页上的动态信息。"""
        try:
            self.data_path_label.configure(
                text=f"配置与数据都存放在：{app_dir()}")
        except Exception:
            pass
        try:
            log_dir = app_dir() / "runtime" / "logs"
            n = len(list(log_dir.glob("*.log"))) if log_dir.exists() else 0
            extra = len(list((app_dir() / "runtime").glob("page_dump_*.txt")))
            msg = f"当前有 {n} 份日志"
            if extra:
                msg += f"，{extra} 份页面诊断文件"
            self.log_stat_label.configure(text=msg)
        except Exception:
            pass
        self._refresh_cache_stat()
        parts = []
        for mod in ("playwright", "anthropic", "httpx"):
            version = self._dependency_version(mod)
            parts.append(f"{mod} {version}" if version else f"{mod} 未装")
        self.deps_label.configure(text="   ".join(parts))

    def _pick_browser(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 chrome.exe、msedge.exe 或 chromium.exe",
            filetypes=[("浏览器主程序", "*.exe"), ("所有文件", "*.*")],
            parent=self.root,
        )
        if path:
            self.exe_path_var.set(path)

    def _pick_browser_dir(self) -> None:
        path = filedialog.askdirectory(title="选择浏览器安装文件夹", parent=self.root)
        if not path:
            return
        from .config import resolve_browser_path

        resolved = resolve_browser_path(path, self.channel_var.get())
        if resolved and resolved.lower().endswith(".exe"):
            self.exe_path_var.set(resolved)
            self._append_log("SYSTEM", f"已在该文件夹中找到浏览器：{resolved}")
        else:
            self.exe_path_var.set(path)
            messagebox.showwarning(
                "没找到浏览器主程序",
                f"在这个文件夹里没找到所选浏览器的主程序：\n{path}\n\n"
                "路径已填入，但启动时可能失败。\n"
                "建议改用「选文件」直接指定 chrome.exe，"
                "或者干脆清空让程序自动查找。",
                parent=self.root,
            )

    # ---------------- 配置读写 ----------------

    def _store_current_account(self) -> None:
        self._accounts[self._active_account_id].update(
            username=self.username_var.get().strip(), password=self.password_var.get())

    def _load_current_account(self) -> None:
        account = self._accounts[self._active_account_id]
        self.username_var.set(account["username"])
        self.password_var.set(account["password"])

    def _refresh_accounts(self) -> None:
        self.account_combo.configure(values=[value["name"] for value in self._accounts.values()])
        self.account_combo.current(list(self._accounts).index(self._active_account_id))
        self.url_list.set_accounts(self._accounts)

    def _switch_account(self, _event=None) -> None:
        selected = self.account_combo.current()
        self._store_current_account()
        self._active_account_id = list(self._accounts)[selected]
        self._load_current_account()

    def _account_name(self, initial="") -> str | None:
        result = _AccountDialog(self.root, "账号改名", name=initial).result
        if result is None:
            return None
        name = result["name"]
        if any(account["name"] == name and key != self._active_account_id
               for key, account in self._accounts.items()):
            messagebox.showwarning("账号名称重复", "请使用不同的账号名称。", parent=self.root)
            return None
        return name

    def _add_account(self) -> None:
        result = _AccountDialog(self.root, "新增账号", credentials=True).result
        if result is None:
            return
        name = result["name"]
        if any(account["name"] == name for account in self._accounts.values()):
            messagebox.showwarning("账号名称重复", "请使用不同的账号名称。", parent=self.root)
            return
        self._store_current_account()
        account_id = uuid.uuid4().hex
        self._accounts[account_id] = {"id": account_id, "name": name, "platform": "",
                                      "username": result["username"], "password": result["password"]}
        self._active_account_id = account_id
        self._refresh_accounts()
        self._load_current_account()

    def _rename_account(self) -> None:
        if self._active_account_id == "default":
            messagebox.showinfo("默认账号", "默认账号保留固定名称，以兼容旧配置。", parent=self.root)
            return
        name = self._account_name(self._accounts[self._active_account_id]["name"])
        if name:
            self._accounts[self._active_account_id]["name"] = name
            self._refresh_accounts()

    def _remove_account(self) -> None:
        if self._active_account_id == "default":
            messagebox.showinfo("默认账号", "默认账号不能移除。", parent=self.root)
            return
        if any(row["account_id"] == self._active_account_id and row["var"].get().strip()
               for row in self.url_list.rows):
            messagebox.showwarning("账号仍在使用", "请先为相关地址选择其他账号。", parent=self.root)
            return
        del self._accounts[self._active_account_id]
        self._active_account_id = "default"
        self._refresh_accounts()
        self._load_current_account()

    def _collect(self) -> dict:
        self._store_current_account()
        self._remember_provider_key()
        items = self.url_list.get_items()
        urls = [i["url"] for i in items]
        try:
            limit = float(self.limit_var.get() or 0)
        except ValueError:
            limit = 0.0
        return {
            "username": self._accounts["default"]["username"],
            "password": self._accounts["default"]["password"],
            "accounts": list(self._accounts.values()),
            "login_timeout_seconds": self._login_timeout_value(),
            "channel": self.channel_var.get(),
            "executable_path": self.exe_path_var.get().strip(),
            "window_size": (1440, 900),
            "keep_open_on_failure": self.keep_open_var.get(),
            "headless": self.headless_var.get(),
            "urls": urls,
            "items": items,
            "speed": round(self.speed_var.get(), 2),
            "mute": self.mute_var.get(),
            "allow_high_speed": self.high_speed_var.get(),
            "captcha_popup": self.captcha_popup_var.get(),
            "limit_max_minutes": limit,
            "answer_enabled": self.answer_enabled_var.get(),
            "retry_until_correct": self.retry_var.get(),
            "auto_submit": self.auto_submit_var.get(),
            "exam_auto_submit": self.exam_auto_submit_var.get(),
            "provider": self.vendor_key(),
            "api_key": self.api_key_var.get().strip(),
            "api_keys": dict(self._provider_keys),
            "model": self.model_var.get().strip(),
            "base_url": self.base_url_var.get().strip(),
            "timeout": 45,
            "cache": self.cache_var.get(),
            "log_level": self.log_level_var.get(),
            "beep_on_captcha": self.beep_var.get(),
            "autorun": self.autorun_var.get(),
            "font_size": int(round(self.font_size_var.get())),
            "proxy": self.proxy_var.get().strip(),
            "on_finish": FINISH_ACTIONS.get(self.on_finish_var.get(), "none"),
            "always_on_top": self.on_top_var.get(),
            "start_minimized": self.start_min_var.get(),
            "maximize": self.maximize_var.get(),
        }

    def _login_timeout_value(self) -> float:
        try:
            value = float(self.login_timeout_var.get())
        except ValueError:
            raise ValueError("登录／验证等待时间必须是至少 1 秒的数字。") from None
        if not math.isfinite(value) or value < 1:
            raise ValueError("登录／验证等待时间必须是至少 1 秒的数字。")
        return value

    def load_config(self) -> None:
        if not CONFIG_PATH.exists():
            self._apply_font_size()
            self._append_log("SYSTEM", "未找到 config.toml，将使用默认设置。填好后点「保存配置」。")
            self._toggle_answer_fields()
            return
        try:
            cfg = Config(CONFIG_PATH)
        except ConfigError as exc:
            self._apply_font_size()
            self._append_log("ERROR", f"配置文件读取失败：{exc}")
            self._toggle_answer_fields()
            return
        self._accounts = {key: dict(account, platform="") for key, account in cfg.accounts.items()}
        self._active_account_id = "default"
        self._refresh_accounts()
        self._load_current_account()
        self.login_timeout_var.set(f"{cfg.login_timeout_seconds:g}")
        self.channel_var.set(cfg.channel_raw)
        self.exe_path_var.set(cfg.executable_path_raw)
        self.keep_open_var.set(cfg.keep_browser_open)
        self.headless_var.set(cfg.headless)
        self.url_list.set_items(cfg.course_items)
        self.speed_var.set(cfg.speed)
        self._on_speed_change(None)
        self.mute_var.set(cfg.mute)
        self.high_speed_var.set(cfg.allow_high_speed)
        self.captcha_popup_var.set(cfg.captcha_popup)
        self._apply_speed_ceiling()   # 上限要跟着开关走，否则滑块还停在 2.0
        self.limit_var.set(str(int(cfg.limit_max_minutes)))
        self.answer_enabled_var.set(cfg.answer_enabled)
        self.retry_var.set(cfg.retry_until_correct)
        self.auto_submit_var.set(cfg.auto_submit)
        self.exam_auto_submit_var.set(cfg.exam_auto_submit)
        prov = providers.get(cfg.answer_provider)
        self.provider_var.set(prov.label)
        self._provider_keys = cfg.api_keys
        self._active_provider_key = prov.key
        self.model_box.configure(values=prov.models)
        self.provider_note.configure(
            text=prov.note, style="Hint.TLabel")
        # 从配置读 Key 时不要把环境变量里的值回填进输入框，否则一保存就落盘了
        self.api_key_var.set(self._provider_keys.get(prov.key, ""))
        self.model_var.set(cfg.model)
        self.base_url_var.set(cfg.base_url or prov.base_url)
        self.cache_var.set(cfg.answer_cache)
        self.log_level_var.set(cfg.log_level)
        self.beep_var.set(cfg.beep_on_captcha)
        self.autorun_var.set(cfg.autorun)
        self.font_size_var.set(cfg.font_size)
        self._on_font_size_change()
        self.proxy_var.set(cfg.proxy)
        self.on_finish_var.set(FINISH_LABELS.get(cfg.on_finish, "什么都不做"))
        self.on_top_var.set(cfg.always_on_top)
        self._apply_on_top()
        self.start_min_var.set(cfg.start_minimized)
        self.maximize_var.set(cfg.maximize)
        self._toggle_answer_fields()
        self._update_browser_hint()
        self._append_log("SYSTEM", f"已载入配置，共 {len(cfg.course_urls)} 门课程。")

    def save(self, silent: bool = False) -> bool:
        try:
            data = self._collect()
        except ValueError as exc:
            messagebox.showerror("设置有误", str(exc), parent=self.root)
            return False
        try:
            save_config(CONFIG_PATH, data)
        except OSError as exc:
            messagebox.showerror("保存失败", f"无法写入 {CONFIG_PATH}：{exc}", parent=self.root)
            return False
        if not silent:
            self._append_log("SYSTEM", f"配置已保存到 {CONFIG_PATH.resolve()}")
        return True

    # ---------------- 运行控制 ----------------

    def _autorun_start(self) -> None:
        """自动开始。只在还没跑起来、且确实配了课程时才动作。"""
        if self.worker and self.worker.is_alive():
            return
        if not self.url_list.get_urls():
            self._append_log("WARN", "没有填课程地址，自动开始已取消。")
            return
        self.start()

    def start(self) -> None:
        if self._installing_update:
            messagebox.showinfo("正在准备更新", "请等待更新完成后再开始任务。", parent=self.root)
            return
        if self.worker and self.worker.is_alive():
            return
        if not self.url_list.get_urls():
            messagebox.showerror("缺少任务地址", "请至少填写一个课程首页、播放页或考试页地址。", parent=self.root)
            return
        if not self.save(silent=True):
            return

        mismatch = self._check_model_match()
        if mismatch:
            if not messagebox.askokcancel(
                "服务商与模型不匹配",
                mismatch + "\n\n到「AI 答题」页重新选一次服务商即可自动配好。\n\n"
                "也可以点确定继续 —— AI 调用会失败，但程序会自动改为"
                "逐个尝试作答，照样能刷课。",
                parent=self.root,
            ):
                return

        blocking, degraded = self._check_dependencies()
        if blocking:
            messagebox.showerror(
                "缺少必需依赖",
                "无法启动，缺少：\n\n" + "\n".join(f"  · {m}" for m in blocking)
                + "\n\n" + self._dependency_repair_hint(),
                parent=self.root,
            )
            return
        if degraded:
            proceed = messagebox.askokcancel(
                "AI 组件不可用",
                "缺少：\n" + "\n".join(f"  · {m}" for m in degraded)
                + "\n\nAI 并不是刷课的必需品 —— 开启了「答错自动重试」后，"
                "程序会直接按选项顺序逐个尝试，一样能把题答对，只是多点几次。\n\n"
                "是否以「纯试错」方式继续？",
                parent=self.root,
            )
            if not proceed:
                return
            self._append_log("WARN", "AI 组件不可用，本次以纯试错方式作答。")

        try:
            config = Config(CONFIG_PATH)
        except ConfigError as exc:
            messagebox.showerror("配置有误", str(exc), parent=self.root)
            return
        for problem in config.validate():
            self._append_log("WARN", problem)

        self.logger.set_level(config.log_level)
        self.stop_event.clear()
        self._set_running(True)
        self._append_log("SYSTEM", "=" * 40)
        self._append_log("SYSTEM", "开始刷课。浏览器即将打开，请勿最小化窗口。")

        self.worker = threading.Thread(target=self._run_worker, args=(config,),
                                       name="coursemate-worker", daemon=True)
        self.worker.start()

    def _check_dependencies(self) -> tuple[list[str], list[str]]:
        """返回 (阻塞性缺失, 可降级缺失)。

        只有 playwright 是真必需的——没有浏览器驱动就没法刷课。
        AI 的 SDK 缺了只是少一个"第一次就猜对"的加速器，试错照样能答对题。
        """
        blocking: list[str] = []
        degraded: list[str] = []
        try:
            import playwright  # noqa: F401
        except ImportError:
            blocking.append("playwright —— 浏览器驱动，没有它无法刷课")

        if self.answer_enabled_var.get() and self.api_key_var.get().strip():
            if self.vendor_key() == "anthropic":
                try:
                    import anthropic  # noqa: F401
                except ImportError:
                    degraded.append("anthropic —— 调用 Claude 所需")
            else:
                try:
                    import httpx  # noqa: F401
                except ImportError:
                    degraded.append("httpx —— 调用国内大模型所需")
        return blocking, degraded

    def _run_worker(self, config: Config) -> None:
        """工作线程：跑 asyncio 事件循环。绝不直接碰任何 tkinter 控件。"""
        import asyncio

        completed = False
        try:
            from .runner import run

            completed = asyncio.run(run(config, should_stop=self.stop_event.is_set))
        except Exception as exc:
            self.logger.log_exception("刷课过程出现未处理异常。", exc)
        finally:
            self.logger.save()
            # 回到主线程再改界面状态
            self.root.after(0, lambda: self._set_running(False))
            self.root.after(0, lambda: self._append_log("SYSTEM", "运行结束。"))
            # 用户主动停止时不该触发关机之类的收尾动作
            if completed and not self.stop_event.is_set():
                self.root.after(500, self._run_finish_action)

    def stop(self) -> None:
        if not (self.worker and self.worker.is_alive()):
            return
        self.stop_event.set()
        self.stop_btn.configure(state="disabled")
        self.status_var.set("● 正在停止...")
        self.status_label.configure(foreground="#b8860b")
        self._append_log("SYSTEM", "已发出停止指令，正在收尾（最多几秒）...")

    def _set_running(self, running: bool) -> None:
        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        if running:
            self.status_var.set("● 运行中")
            self.status_label.configure(foreground="#1565c0")
        else:
            self.status_var.set("● 就绪")
            self.status_label.configure(foreground="#2e7d32")

    # ---------------- 日志 ----------------

    def _on_log(self, level: str, message: str, ts: str) -> None:
        """日志订阅回调。可能来自工作线程，因此只入队，不碰界面。"""
        self.log_queue.put((level, message, ts))

    def _drain_log_queue(self) -> None:
        self._drain_update_events()
        if getattr(self, "_closing", False):
            return
        drained = 0
        # 每轮最多取 200 条，避免刷屏时界面卡死
        while drained < 200:
            try:
                level, message, ts = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log(level, message, ts)
            if "[需要你处理]" in message:
                self._call_user_over(message)
            drained += 1
        self.root.after(120, self._drain_log_queue)

    def _call_user_over(self, message: str) -> None:
        """把人叫回来处理需要人工的事（目前只有人机验证）。

        本地验证未通过时需要人工处理，"及时被发现"就是
        这条路上唯一能优化的地方——窗口收在托盘里或被别的程序挡住时，
        光响一声铃很容易错过，而错过的每一秒都是白等。
        """
        beep = bool(getattr(self, "beep_var", None) and self.beep_var.get())
        popup = bool(getattr(self, "captcha_popup_var", None)
                     and self.captcha_popup_var.get())
        if not beep and not popup:
            return
        if beep:
            try:
                import winsound

                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except Exception:
                try:
                    self.root.bell()
                except tk.TclError:
                    pass
        if not popup:
            return
        try:
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            # 短暂置顶再取消：不这样的话，别的程序全屏时窗口仍然浮不上来。
            # 但一直置顶会挡住浏览器——而用户正要去浏览器里点验证码
            self.root.attributes("-topmost", True)
            self.root.focus_force()
            # Windows 有时会拦截普通 lift；显式请求前台并闪烁任务栏作兜底。
            try:
                import ctypes

                hwnd = int(self.root.winfo_id())
                ctypes.windll.user32.ShowWindow(hwnd, 9)
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                ctypes.windll.user32.FlashWindow(hwnd, True)
            except Exception:
                pass
            self.root.after(1200, lambda: self._drop_topmost())
        except tk.TclError:
            pass
        tray = getattr(self, "tray", None)
        if tray is not None:
            try:
                tray.notify(message.replace("[需要你处理] ", ""), APP_NAME)
            except Exception:
                pass

    def _drop_topmost(self) -> None:
        """取消临时置顶——除非用户自己在设置里开了「窗口置顶」。"""
        try:
            if not self.on_top_var.get():
                self.root.attributes("-topmost", False)
        except tk.TclError:
            pass

    def _append_log(self, level: str, message: str, ts: str | None = None) -> None:
        if ts is None:
            from datetime import datetime

            ts = datetime.now().strftime("%H:%M:%S")
        tag = level if level in COLORS else "INFO"
        view = self.log_text.yview()
        at_bottom = not view or view[1] >= 0.999
        anchor = ""
        if not at_bottom:
            anchor = self.log_text.index("@0,0")
            self.log_text.mark_set("_log_view_anchor", anchor)
            self.log_text.mark_gravity("_log_view_anchor", "left")
        self.log_text.configure(state="normal")

        if level == "PROGRESS":
            # 进度行原地更新，不追加新行，否则几小时下来会有几十万行
            last = self.log_text.get("end-2l", "end-1l")
            if last.startswith("[进度]"):
                self.log_text.delete("end-2l", "end-1l")
                self._log_lines -= 1
            self.log_text.insert("end", f"[进度] {message}\n", tag)
        else:
            self.log_text.insert("end", f"[{ts}] {message}\n", tag)
        self._log_lines += 1

        if self._log_lines > MAX_LOG_LINES:
            trim = self._log_lines - MAX_LOG_LINES
            self.log_text.delete("1.0", f"{trim + 1}.0")
            self._log_lines -= trim

        self.log_text.configure(state="disabled")
        if at_bottom:
            self.log_text.see("end")
        elif anchor:
            # 用户正在翻旧日志时保持原位置；只有本来就在底部才追随新日志。
            self.log_text.yview("_log_view_anchor")
            self.log_text.mark_unset("_log_view_anchor")
        if getattr(self, "log_collapsed", False):
            self.log_tail_label.configure(text=f"最新：{message[:46]}")

    def _toggle_log(self) -> None:
        """收起/展开日志区。收起时把最后一条日志显示在标题旁，
        这样即使折叠着也能瞄一眼当前状态。"""
        self.log_collapsed = not self.log_collapsed
        if self.log_collapsed:
            self.log_body.pack_forget()
            self.log_arrow.configure(text="▶")
            # 让出这一行的权重，剩余空间全归上面的标签页
            self.body.rowconfigure(2, weight=0)
        else:
            self.log_body.pack(fill="both", expand=True)
            self.log_arrow.configure(text="▼")
            self.body.rowconfigure(2, weight=2)
            self.log_tail_label.configure(text="")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._log_lines = 0

    def open_logs(self) -> None:
        path = app_dir() / "runtime" / "logs"
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showinfo("日志位置", f"{path}\n\n({exc})", parent=self.root)

    # ---------------- 退出 ----------------

    # 留给浏览器收尾的时间。Playwright 要关页面、关浏览器、停 node driver，
    # 机器慢的时候几秒不够；等不到就只能强杀，那会留下浏览器进程
    SHUTDOWN_WAIT = 20

    def on_close(self) -> None:
        """点窗口右上角的 ✕：收进托盘，不退出。

        刷课动辄挂一两个小时，误点一下 ✕ 就前功尽弃。
        收进托盘既不打断正在跑的活，又随时能叫回来；
        真要退出，从托盘图标右键点「退出」。

        但托盘用不了的时候（pystray 没装、图标创建失败）必须老实退出——
        否则窗口关了又没有托盘图标，等于把软件弄丢了，只能去任务管理器杀。
        """
        if getattr(self, "tray", None) is None:
            self.quit_app()
            return
        self._hide_to_tray()

    def _hide_to_tray(self) -> None:
        if not self._save_before_leaving():
            return
        self._save_geometry()       # 先存，withdraw 之后就取不到真实尺寸了
        try:
            self.root.withdraw()
        except tk.TclError:
            return
        if not getattr(self, "_tray_hint_shown", False):
            self._tray_hint_shown = True
            # 只提示第一次。窗口凭空消失而任务栏又没有它，
            # 不说一声的话，用户会以为软件崩了
            try:
                self.tray.notify(
                    "软件收进了托盘，仍在后台运行。\n"
                    "双击托盘图标可以叫回窗口，右键点「退出」才是真的退出。",
                    APP_NAME)
            except Exception:
                pass

    def _save_before_leaving(self) -> bool:
        """收进托盘或真正退出前保存当前界面，避免地址回退到旧配置。"""
        if self.save(silent=True):
            return True
        self._append_log("ERROR", "配置未保存，已取消退出或收进托盘。")
        return False

    def quit_app(self, prepared_update: Path | None = None) -> None:
        """真正退出。托盘菜单的「退出」走这条。"""
        if getattr(self, "_closing", False):
            return              # 托盘和窗口可能同时触发，别走两遍
        if prepared_update is not None and self.worker and self.worker.is_alive():
            messagebox.showinfo("更新已准备", "请先停止当前任务，再安装更新。", parent=self.root)
            return
        if self.worker and self.worker.is_alive():
            if not messagebox.askokcancel(
                "确认退出", "刷课正在进行中，确定要退出吗？\n\n"
                "浏览器会一并关闭，当前小节的进度可能不会被平台记录。",
                parent=self.root,
            ):
                return
        if not self._save_before_leaving():
            return
        if prepared_update is not None:
            try:
                updates.launch_update(prepared_update)
            except Exception as exc:
                self.update_status_var.set("更新未启动，软件继续运行")
                messagebox.showerror("更新未启动", str(exc), parent=self.root)
                return
        self._closing = True
        self._save_geometry()
        self._stop_tray()

        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self.status_var.set("● 正在关闭浏览器...")
            self.status_label.configure(foreground="#b8860b")
            # 分段等待而不是 join()：join 会把界面卡死成白板，
            # 用户不知道在等什么，只会以为程序挂了
            deadline = time.monotonic() + self.SHUTDOWN_WAIT
            while self.worker.is_alive() and time.monotonic() < deadline:
                try:
                    self.root.update()
                except tk.TclError:
                    break
                time.sleep(0.05)

        self.logger.remove_sink(self._on_log)
        self.logger.save()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

        # Playwright 的浏览器和 node driver 都是本进程的子进程。
        # worker 没能在限时内收尾时它们还活着，而 daemon 线程被强杀是
        # 不会执行 async with 的清理的——那就真留下一堆后台进程了。
        # 这里连同自己的进程树一起收干净；只动自己的子进程，
        # 碰不到用户自己开的浏览器。
        if self.worker and self.worker.is_alive():
            self._kill_own_process_tree()

    @staticmethod
    def _kill_own_process_tree() -> None:
        if os.name != "nt":
            return
        try:
            subprocess.run(
                ["taskkill", "/PID", str(os.getpid()), "/T", "/F"],
                capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            os._exit(0)     # taskkill 都不成，只能自己了断，总之不能留着


def main() -> int:
    # Windows 必须在创建顶层窗口前指定应用 ID；否则任务栏可能继续沿用 Tk/Python 的默认图标。
    try:
        from ctypes import windll

        windll.shell32.SetCurrentProcessExplicitAppUserModelID("OnlineCourseAssistant.App")
    except Exception:
        pass
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    if os.name == "nt":
        # 保持原有字号；DPI 感知先于 Tk 创建，避免初始化后再改变缩放度量。
        root.tk.call("tk", "scaling", 4 / 3)
    CourseMateGUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
