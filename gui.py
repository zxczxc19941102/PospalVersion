# -*- coding: utf-8 -*-
"""
银豹全版本下载链接速查工具 —— 桌面 GUI 版（Tkinter，原生窗口，不需要浏览器）
==========================================================================

运行：
    py gui.py
    （或使用项目虚拟环境： .venv\\Scripts\\python.exe gui.py）

界面与交互（与网页版一致）：
    · 顶部：标题 + 抓取进度（正在抓取 x/72 → 已加载 72/72）+「刷新」
    · 左栏顶部两个汇总分类：「⬇ 有效链接」（默认选中，跨 72 个分类汇总所有带链接的版本）
      与「⬇ 无效链接」（默认隐藏，点「显示无效链接分类」按钮才出现）
    · 左栏搜索框：支持模糊子串、汉字拼音首字母（bsyxt）、全拼（shouyin）、全字；
      搜索时即使「无效链接」分类处于隐藏状态也会被搜出来
    · 左栏其余 72 个软件分类，每项显示「分类名 · 版本数」，当前项高亮，失败项红色「加载失败」
    · 右栏：选中分类的全部历史版本（版本号 + 发布时间 + 汇总分类里的来源分类），
      无下载链接的版本为灰色不可点
    · 单击版本号：复制该版本的完整下载链接，状态栏提示「复制成功」（2 秒后消失）
    · 「在新标签打开」按钮：用系统默认浏览器打开该版本链接（.exe/.apk 直接下载，Mac 页/App Store 跳转）
    · 下方「更新说明」区域：显示所选版本的更新内容
    · 抓取失败的分类：右栏提示「重新加载」，点击即单独重试，不影响其它分类

依赖：requests、beautifulsoup4（与网页版共用 app.py 的抓取与解析逻辑），pypinyin 为可选依赖
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import ttk

# 打包成窗口版（--windowed）时没有控制台，sys.stdout/stderr 为 None；
# 用空设备兜底，避免任何 print / 日志写入触发异常。
if sys.stdout is None or sys.stderr is None:
    try:
        _devnull = open(os.devnull, "w", encoding="utf-8", errors="ignore")
        if sys.stdout is None:
            sys.stdout = _devnull
        if sys.stderr is None:
            sys.stderr = _devnull
    except Exception:
        pass

import app as backend  # 导入即触发一次后台全量抓取；同时提供解析/缓存/进度

BRAND = "#b81c22"
BRAND_SOFT = "#fdf2f2"
GREY = "#9aa0a8"
OK_GREEN = "#15803d"
ERR_RED = "#b91c1c"
POLL_MS = 400          # 抓取中的进度刷新间隔
POLL_IDLE_MS = 1000    # 空闲时的进度刷新间隔
SEARCH_HINT = "搜索：关键字 / 拼音首字母（如 bsyxt）"
VER_SEARCH_HINT = "全局搜索版本号 / 关键字（跨全部分类，支持拼音首字母、汉字、字母间空格）"

UI_FONT = ("Microsoft YaHei UI", 10)
UI_SMALL = ("Microsoft YaHei UI", 9)
UI_BOLD = ("Microsoft YaHei UI", 10, "bold")
UI_TITLE = ("Microsoft YaHei UI", 13, "bold")
TEXT_COLOR = "#33383f"


def normalize(text: str) -> str:
    """搜索归一化：转小写并去掉空格与标点，只保留字母数字与汉字。"""
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def match_category(keyword: str, entry: dict) -> bool:
    """模糊搜索：原文子串、拼音首字母子串或全拼子串。

    例：「收银」「PC版零售」「bsyxt」（首字母）「pcblssyxt」「shouyin」（全拼）
    都能命中 PC版零售收银系统。
    """
    key = normalize(keyword)
    if not key:
        return True
    return (key in normalize(entry.get("name", ""))
            or key in normalize(entry.get("initials", ""))
            or key in normalize(entry.get("fullpy", "")))


def entry_count(entry: dict) -> int:
    """分类的版本数：汇总分类未展开时用后端统计好的 total。"""
    versions = entry.get("versions")
    if versions:
        return len(versions)
    return int(entry.get("total") or 0)


class DesktopApp:
    """桌面版主窗口。所有网络请求都在后台线程，UI 只通过 root.after 更新。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.entries: list = []        # 全部分类（顺序：有效链接 → 无效链接 → 72 个官方分类）
        self.versions: list = []       # 当前分类的版本列表
        self.current_entry = None
        self.loaded = False
        self.toast_job = None
        self.pending: "queue.Queue" = queue.Queue()  # 后台线程 -> 主线程的结果传递
        self.fast_poll = False                       # 有后台任务时加快轮询
        self.show_invalid = False                    # 左栏「无下载链接」汇总分类默认隐藏
        self.loading_summaries: set = set()          # 正在汇总中的分类，避免重复请求
        self.visible: list = []                      # 当前显示的分类在 self.entries 中的下标
        self.current_row = 0                         # 当前选中分类在左栏的行号
        # 右栏分组与版本搜索状态
        self.groups: list = []                       # [{key,title,expanded,items:[version索引]}
        self.valid_expanded = True                   # 有效链接组默认展开
        self.invalid_expanded = False                # 无效链接组默认折叠
        self.version_keyword = ""                    # 右栏版本搜索关键字
        self.ver_search_var = tk.StringVar()         # 右栏搜索框绑定变量
        self._rendered_checked = False               # 当前分类是否已按检测结果渲染过分组
        self.row_index: dict = {}                    # 右栏 Listbox 行号 -> 版本索引（表头为 None）
        self.row_header: dict = {}                   # 右栏 Listbox 行号 -> 是否分组表头
        self.row_group: dict = {}                    # 右栏 Listbox 表头行号 -> 所属分组序号
        self._search_last_detect = -1                # 全局搜索时上次重绘用的检测进度，用于节流
        self._search_kw = ""                         # 上次全局搜索渲染的关键字（判断是否新搜索）

        root.title("银豹版本下载链接速查")
        root.geometry("1120x740")
        root.minsize(820, 560)
        root.configure(bg="#ffffff")
        self._build()

        # 表格选中样式：浅红底 + 品牌色文字
        self.cat_list.configure(selectbackground=BRAND_SOFT, selectforeground=BRAND)
        self.ver_list.configure(selectbackground=BRAND_SOFT, selectforeground=BRAND)

        self.root.after(POLL_MS, self.poll)

    # ------------------------------------------------------------------ #
    # 界面搭建
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        # 顶部标题栏
        header = tk.Frame(root, bg=BRAND, height=58)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        tk.Label(header, text="银豹版本下载链接速查", bg=BRAND, fg="#ffffff",
                 font=UI_TITLE).pack(side="left", padx=18)
        self.progress_var = tk.StringVar(value="准备中…")
        tk.Label(header, textvariable=self.progress_var, bg=BRAND, fg="#ffe3e4",
                 font=UI_SMALL).pack(side="right", padx=(10, 18))
        tk.Button(header, text="刷新", command=self.on_refresh, relief="flat", bd=0,
                  bg="#ffffff", fg=BRAND, activebackground="#ffe3e4", activeforeground=BRAND,
                  font=UI_SMALL, padx=14, pady=3, cursor="hand2").pack(side="right", padx=6)

        # 顶部全局搜索框（跨全部分类搜索版本号 / 关键字）；最后 pack 以填满中间空白
        self.ver_search_var = tk.StringVar()
        self.ver_search = tk.Entry(header, textvariable=self.ver_search_var, font=UI_SMALL,
                                   bd=0, relief="flat", highlightthickness=0,
                                   bg="#ffffff", fg="#33383f", insertbackground=BRAND)
        self.ver_search.pack(side="left", fill="x", expand=True, padx=(6, 14), pady=14)
        self.ver_search.insert(0, VER_SEARCH_HINT)
        self.ver_search.configure(fg="#9aa0a8")
        self.ver_search.bind("<FocusIn>", self.on_ver_search_focus)
        self.ver_search.bind("<FocusOut>", self.on_ver_search_blur)
        self.ver_search.bind("<KeyRelease>", self.on_ver_search_key)
        self.ver_search.bind("<Escape>", self.on_ver_search_escape)

        # 主体：左分类栏 + 右版本栏
        body = tk.Frame(root, bg="#ffffff")
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=0, minsize=300)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left = tk.Frame(body, bg="#fafafb", highlightbackground="#e8e8ec", highlightthickness=1)
        left.grid(row=0, column=0, sticky="nsew")
        left.rowconfigure(2, weight=1)
        left.columnconfigure(0, weight=1)

        # 左栏第 0 行：标题 + 「无效链接」分类显示开关
        side_top = tk.Frame(left, bg="#fafafb")
        side_top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(8, 2))
        self.side_head = tk.Label(side_top, text="软件分类（共 %d 个）" % len(backend.CATEGORIES),
                                  bg="#fafafb", fg=GREY, font=UI_SMALL, anchor="w")
        self.side_head.pack(side="left")
        self.invalid_btn = tk.Button(side_top, text="显示无效链接", command=self.toggle_invalid_group,
                                     relief="flat", bd=0, bg="#eef0f3", fg="#6b7280",
                                     activebackground="#e2e5ea", activeforeground="#33383f",
                                     font=(UI_FONT[0], 8), padx=8, pady=1, cursor="hand2")
        self.invalid_btn.pack(side="right")

        # 左栏第 1 行：搜索框（模糊 / 拼音首字母 / 全字）
        search_row = tk.Frame(left, bg="#fafafb")
        search_row.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 6))
        search_row.columnconfigure(0, weight=1)
        self.search_var = tk.StringVar()
        self.search_entry = tk.Entry(search_row, textvariable=self.search_var, font=UI_SMALL,
                                     bd=0, highlightthickness=1, highlightbackground="#dcdfe4",
                                     highlightcolor=BRAND, bg="#ffffff", fg="#33383f",
                                     insertbackground=BRAND)
        self.search_entry.grid(row=0, column=0, sticky="ew")
        self.search_entry.insert(0, SEARCH_HINT)
        self.search_entry.configure(fg="#a8adb4")
        self.search_entry.bind("<FocusIn>", self.on_search_focus)
        self.search_entry.bind("<FocusOut>", self.on_search_blur)
        self.search_entry.bind("<KeyRelease>", self.on_search_key)
        self.search_entry.bind("<Escape>", self.on_search_escape)

        self.cat_list = tk.Listbox(left, exportselection=False, activestyle="none",
                                   font=UI_FONT, bd=0, highlightthickness=0, bg="#fafafb")
        self.cat_list.grid(row=2, column=0, sticky="nsew")
        self.cat_list.bind("<<ListboxSelect>>", self.on_cat_select)
        cat_sb = ttk.Scrollbar(left, orient="vertical", command=self.cat_list.yview)
        cat_sb.grid(row=2, column=1, sticky="ns")
        self.cat_list.configure(yscrollcommand=cat_sb.set)

        right = tk.Frame(body, bg="#ffffff")
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        head = tk.Frame(right, bg="#ffffff")
        head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=14, pady=(8, 4))
        self.title_var = tk.StringVar(value="正在加载…")
        tk.Label(head, textvariable=self.title_var, bg="#ffffff", font=UI_TITLE,
                 anchor="w").pack(side="left")
        self.meta_var = tk.StringVar(value="")
        tk.Label(head, textvariable=self.meta_var, bg="#ffffff", fg=GREY,
                 font=UI_SMALL).pack(side="left", padx=10)
        self.open_btn = ttk.Button(head, text="在新标签打开", command=self.on_open_link, state="disabled")
        self.open_btn.pack(side="right")
        self.retry_btn = ttk.Button(head, text="重新加载", command=self.on_retry, state="disabled")
        self.retry_btn.pack(side="right", padx=6)

        self.ver_list = tk.Listbox(right, exportselection=False, activestyle="none",
                                   font=UI_FONT, bd=0, highlightthickness=0, bg="#ffffff")
        self.ver_list.grid(row=1, column=0, sticky="nsew", padx=(14, 0))
        self.ver_list.bind("<ButtonRelease-1>", self.on_version_click)
        self.ver_list.bind("<<ListboxSelect>>", self.on_version_select)
        ver_sb = ttk.Scrollbar(right, orient="vertical", command=self.ver_list.yview)
        ver_sb.grid(row=1, column=1, sticky="ns")
        self.ver_list.configure(yscrollcommand=ver_sb.set)

        notes_box = ttk.LabelFrame(right, text=" 更新说明 ")
        notes_box.grid(row=2, column=0, columnspan=2, sticky="ew", padx=14, pady=(10, 10))
        notes_box.columnconfigure(0, weight=1)
        self.notes = tk.Text(notes_box, height=9, wrap="word", font=UI_SMALL, bd=0,
                             bg="#fafafb", fg="#565c64", padx=8, pady=6)
        self.notes.grid(row=0, column=0, sticky="nsew")
        notes_sb = ttk.Scrollbar(notes_box, orient="vertical", command=self.notes.yview)
        notes_sb.grid(row=0, column=1, sticky="ns")
        self.notes.configure(yscrollcommand=notes_sb.set, state="disabled")

        # 底部状态栏（左侧显示当前链接，右侧为 Toast）
        status = tk.Frame(root, bg="#f5f5f7")
        status.grid(row=3, column=0, sticky="ew")
        self.status_var = tk.StringVar(value="点击版本号即可复制完整下载链接")
        tk.Label(status, textvariable=self.status_var, bg="#f5f5f7", fg="#6b7280",
                 font=UI_SMALL, anchor="w").pack(side="left", padx=14, pady=4)
        self.toast_var = tk.StringVar(value="")
        self.toast_label = tk.Label(status, textvariable=self.toast_var, bg="#f5f5f7",
                                    fg=OK_GREEN, font=UI_BOLD)
        self.toast_label.pack(side="right", padx=14)

    # ------------------------------------------------------------------ #
    # 抓取进度与数据装载
    # ------------------------------------------------------------------ #
    def poll(self) -> None:
        self.drain_pending()
        progress = backend.get_progress()
        total = progress["total"]
        if progress["running"]:
            self.progress_var.set("正在抓取 %d/%d" % (progress["done"], total))
        else:
            text = "已加载 %d/%d · 成功 %d · 失败 %d" % (
                progress["done"], total, progress.get("ok", 0), progress.get("error", 0))
            if progress.get("detecting"):
                text += " · 检测 %d/%d" % (progress.get("detect_done", 0), progress.get("detect_total", 0))
            self.progress_var.set(text)
            if progress["done"] >= total and not self.loaded:
                self.load_all()
                return
            # 检测阶段
            if self.version_keyword:
                # 全局搜索中：按检测进度节流重绘结果（有效/无效分组随检测推进）
                done = progress.get("detect_done", 0)
                detecting = progress.get("detecting")
                if (detecting and done - self._search_last_detect >= 40) or \
                        (not detecting and done != self._search_last_detect):
                    self._search_last_detect = done
                    self._render_search(self.version_keyword)
            elif self.loaded and self.current_entry and not self.current_entry.get("virtual"):
                # 当前软件检测完成时整体重渲染其两个分组；未完成则刷新进度
                live = backend.lookup_category(self.current_entry["name"])
                if live:
                    self.current_entry = live
                    if live.get("checked") and not self._rendered_checked:
                        self._rendered_checked = True
                        self._render_groups(live.get("versions") or [], live)
                    elif not live.get("checked"):
                        done, total = backend.detect_progress(live)
                        self.meta_var.set("共 %d 个版本（检测中 %d/%d）" % (total, done, total))
        busy = progress["running"] or progress.get("detecting") or self.fast_poll
        self.root.after(POLL_MS if busy else POLL_IDLE_MS, self.poll)

    def drain_pending(self) -> None:
        """在主线程里处理后台线程提交的结果（Tk 只能在主线程操作）。"""
        while True:
            try:
                kind, key, payload = self.pending.get_nowait()
            except queue.Empty:
                return
            if kind == "retry":
                self.after_retry(key, payload)
            elif kind == "summary":
                self.after_summary(key, payload)

    # ------------------------------------------------------------------ #
    # 数据装载
    # ------------------------------------------------------------------ #
    def build_entries(self) -> list:
        """左栏数据 = 「有效链接」+「无效链接」两个汇总分类 + 72 个官方分类。"""
        head = []
        for tpl in backend.virtual_templates():
            entry = dict(tpl)
            entry.update({"status": "ok", "virtual": True, "versions": [], "error": ""})
            head.append(entry)
        return head + backend.snapshot_all()

    def load_all(self) -> None:
        self.entries = self.build_entries()
        self.loaded = True
        self.render_sidebar()
        if self.visible:
            self.cat_list.selection_clear(0, "end")
            self.cat_list.selection_set(0)
            self.show_category(0)
        self.root.after(POLL_IDLE_MS, self.poll)

    def render_sidebar(self) -> None:
        self.cat_list.delete(0, "end")
        keyword = self.keyword()
        self.visible = []
        for index, entry in enumerate(self.entries):
            if (entry.get("virtual") and entry["name"] == backend.INVALID_SUMMARY
                    and not self.show_invalid and not keyword):
                continue  # 「无效链接」分类默认隐藏（搜索时例外，方便搜到就能看到）
            if keyword and not match_category(keyword, entry):
                continue
            self.visible.append(index)
            label = entry["name"]
            if entry.get("virtual"):
                label = "%s %s" % (backend.SUMMARY_ICON, label)
            if entry.get("status") == "error":
                self.cat_list.insert("end", "%s · 加载失败" % label)
                self.cat_list.itemconfigure("end", foreground=ERR_RED)
            else:
                self.cat_list.insert("end", "%s · %d" % (label, entry_count(entry)))

        total = len(backend.CATEGORIES)
        if keyword:
            self.side_head.configure(text="匹配 %d / %d 个分类" % (len(self.visible), total))
        else:
            ok_count = sum(1 for e in self.entries if not e.get("virtual") and e.get("status") == "ok")
            self.side_head.configure(text="软件分类（共 %d 个 · 成功 %d）" % (total, ok_count))

        if not self.visible:
            self.cat_list.insert("end", "没有匹配的分类，试试拼音首字母（如 bsyxt）")
            self.cat_list.itemconfigure("end", foreground=GREY)
            return
        row = self.row_of_current()
        if row is not None:
            self.cat_list.selection_clear(0, "end")
            self.cat_list.selection_set(row)
            self.cat_list.see(row)

    def row_of_current(self):
        """当前选中分类在左栏 Listbox 中的行号（不在可见列表里时返回 None）。"""
        if not self.current_entry:
            return None
        for row, index in enumerate(self.visible):
            if self.entries[index]["name"] == self.current_entry["name"]:
                return row
        return None

    # ------------------------------------------------------------------ #
    # 搜索
    # ------------------------------------------------------------------ #
    def keyword(self) -> str:
        text = self.search_var.get()
        return "" if text == SEARCH_HINT else text.strip()

    def on_search_key(self, _event=None) -> None:
        self.render_sidebar()

    def on_search_focus(self, _event=None) -> None:
        if self.search_var.get() == SEARCH_HINT:
            self.search_entry.delete(0, "end")
            self.search_entry.configure(fg="#33383f")

    def on_search_blur(self, _event=None) -> None:
        if not self.search_var.get().strip():
            self.reset_search()

    def on_search_escape(self, _event=None) -> str:
        self.reset_search()
        return "break"

    def reset_search(self) -> None:
        self.search_entry.delete(0, "end")
        self.search_entry.insert(0, SEARCH_HINT)
        self.search_entry.configure(fg="#a8adb4")
        self.render_sidebar()

    def toggle_invalid_group(self) -> None:
        """左栏「显示无效链接」按钮：切换当前右栏列表「无效链接」分组的展开/折叠。

        同时适用于「某个软件的版本列表」与「全局搜索结果」两种右栏内容。
        """
        if len(self.groups) < 2:
            self.toast("链接检测中，暂无可展开分组", GREY)
            return
        invalid = self.groups[1]
        if not invalid["items"]:
            self.toast("没有无效链接", GREY)
            return
        self.invalid_expanded = not self.invalid_expanded
        invalid["expanded"] = self.invalid_expanded
        self.invalid_btn.configure(text="隐藏无效链接" if self.invalid_expanded else "显示无效链接")
        self._paint_list()

    # ------------------------------------------------------------------ #
    # 分类与版本渲染
    # ------------------------------------------------------------------ #
    def on_cat_select(self, _event=None) -> None:
        selection = self.cat_list.curselection()
        if selection:
            self.show_category(selection[0])

    def show_category(self, row: int) -> None:
        """row 为左栏 Listbox 的行号。"""
        if not (0 <= row < len(self.visible)):
            return
        entry = self.entries[self.visible[row]]
        # 真实分类以最新检测结果为准：从后端实时缓存取（含 alive / checked）
        if not entry.get("virtual"):
            live = backend.lookup_category(entry["name"])
            if live:
                entry = live
        self.current_row = row
        self.current_entry = entry
        # 切换分类时清空顶部全局搜索框，右栏显示该分类
        self.version_keyword = ""
        self.ver_search_var.set("")
        self.ver_search.delete(0, "end")
        self.ver_search.insert(0, VER_SEARCH_HINT)
        self.ver_search.configure(fg="#9aa0a8")
        self._search_last_detect = -1
        self._render_current()

    def _render_right(self) -> None:
        """右栏主渲染入口：有全局搜索词时显示全局搜索结果，否则显示当前分类。"""
        if self.version_keyword:
            self._render_search(self.version_keyword)
        else:
            self._search_kw = ""
            self._render_current()

    def _render_current(self) -> None:
        """渲染当前选中分类（真实分类分组展示 / 汇总分类平铺 / 失败 / 检测中）。"""
        entry = self.current_entry
        if not entry:
            return
        self.title_var.set(entry["name"])
        self.ver_list.delete(0, "end")
        self.versions = []
        self.row_index.clear()
        self.row_header.clear()
        self.row_group.clear()
        self.open_btn.configure(state="disabled")
        self.valid_expanded = True          # 有效链接组默认展开
        self.invalid_expanded = False       # 无效链接组默认折叠
        self._rendered_checked = False
        self.invalid_btn.configure(text="显示无效链接")
        self.status_var.set(entry.get("description") or "点击版本号即可复制完整下载链接")
        self.set_notes("")

        # 汇总分类：首次点击时在后台线程汇总明细
        if entry.get("virtual") and not entry.get("versions"):
            self.meta_var.set("共 %d 个版本" % entry.get("total", 0))
            self.retry_btn.configure(state="disabled")
            if entry["name"] in self.loading_summaries:
                return  # 已经在汇总中，避免重复请求
            self.loading_summaries.add(entry["name"])
            self.ver_list.insert("end", "正在汇总全部分类的「%s」，请稍候…" % entry["name"])
            self.ver_list.itemconfigure("end", foreground=GREY)
            self.fast_poll = True
            threading.Thread(target=self._fetch_summary, args=(entry["name"], entry["kind"]),
                             daemon=True).start()
            return

        if entry.get("status") == "error":
            self.meta_var.set("加载失败")
            self.ver_list.insert("end", "该分类抓取失败，点击右上「重新加载」重试")
            self.ver_list.itemconfigure("end", foreground=ERR_RED)
            self.retry_btn.configure(state="normal")
            self.set_notes(("失败原因：%s" % entry.get("error")) if entry.get("error") else "")
            return

        versions = entry.get("versions") or []
        self.versions = versions
        self.retry_btn.configure(state="normal" if not versions else "disabled")

        if entry.get("virtual"):
            self.meta_var.set("共 %d 个版本 · 来自 %d 个分类" % (len(versions), len(backend.CATEGORIES)))
            self._render_flat(versions)
            return

        # 真实分类：检测完成后按「有效 / 无效」两个分组渲染，未检测完显示「检测中」
        if entry.get("checked"):
            self._render_groups(versions, entry)
            self._rendered_checked = True
        else:
            done, total = backend.detect_progress(entry)
            self.meta_var.set("共 %d 个版本（检测中 %d/%d）" % (total, done, total))
            self.ver_list.insert("end", "链接有效性检测中，请稍候…")
            self.ver_list.itemconfigure("end", foreground=GREY)

    def _render_search(self, keyword: str) -> None:
        """全局搜索：跨全部官方分类匹配版本号 / 关键字，结果按「有效 / 无效」分组展示。

        同一个搜索词被重复重绘（如检测进度推进）时，保留用户手动展开/折叠的分组状态。
        """
        new = keyword != self._search_kw
        self._search_kw = keyword
        matched = []
        for entry in self.entries:
            if entry.get("virtual") or entry.get("status") != "ok":
                continue
            for v in entry.get("versions") or []:
                if backend.match_version(keyword, v):
                    vv = dict(v)
                    vv["category"] = entry["name"]      # 结果里标注来源分类
                    matched.append(vv)
        self.versions = matched
        self.ver_list.delete(0, "end")
        self.row_index.clear()
        self.row_header.clear()
        self.row_group.clear()
        self.open_btn.configure(state="disabled")
        self.set_notes("")
        self.title_var.set("全局搜索「%s」" % keyword)
        self.status_var.set("全局搜索：跨全部分类匹配版本，点击版本号即可复制完整下载链接")
        if new:
            self.valid_expanded = True
            self.invalid_expanded = False
        if not matched:
            self.groups = []
            self.meta_var.set("全局未找到匹配版本")
            self.ver_list.insert("end", "未找到匹配版本")
            self.ver_list.itemconfigure("end", foreground=GREY)
            self.invalid_btn.configure(text="显示无效链接")
            return
        saved = (self.valid_expanded, self.invalid_expanded)
        self.build_groups(matched)
        if not new:                       # 保留用户当前的展开/折叠状态
            self.valid_expanded, self.invalid_expanded = saved
            self.groups[0]["expanded"] = saved[0]
            self.groups[1]["expanded"] = saved[1]
        self.invalid_btn.configure(
            text="隐藏无效链接" if self.invalid_expanded else "显示无效链接")
        vn = len(self.groups[0]["items"])
        inv = len(self.groups[1]["items"])
        self.meta_var.set("全局命中 %d 个版本 · 有效 %d · 无效 %d" % (len(matched), vn, inv))
        self._paint_list()

    # ------------------------------------------------------------------ #
    # 右栏渲染：分组 / 列表
    # ------------------------------------------------------------------ #
    def _render_flat(self, versions: list) -> None:
        """汇总分类下的平铺渲染（按来源分类，不做有效/无效分组）。"""
        self.ver_list.delete(0, "end")
        self.row_index.clear()
        self.row_header.clear()
        self.ver_list.configure(fg="#33383f")
        for idx, v in enumerate(versions):
            published = (v.get("published") or "").strip()
            label = "· 版本 %s" % v.get("version", "")
            if published:
                label += "        %s" % published
            if v.get("category"):
                label += "    ← %s" % v["category"]
            self.ver_list.insert("end", label)
            row = self.ver_list.size() - 1
            self.row_index[row] = idx
            if not v.get("url"):
                self.ver_list.itemconfigure(row, foreground=GREY)

    def build_groups(self, versions: list) -> None:
        """根据版本搜索关键字 + 检测状态，划分有效/无效两组。

        有效链接 (n)：有 url 且检测为有效（alive=True）
        无效链接 (m)：有 url 且检测为无效（alive=False）或没有 url（历史版本，无链接可检测）
        搜索命中时，自动展开含命中条目的折叠分组（FR-3.4）。
        """
        keyword = self.version_keyword
        valid_items, invalid_items = [], []
        for idx, v in enumerate(versions):
            if keyword and not backend.match_version(keyword, v):
                continue
            if v.get("url") and v.get("alive") is True:
                valid_items.append(idx)
            else:
                invalid_items.append(idx)
        if keyword:
            if valid_items:
                self.valid_expanded = True
            if invalid_items:
                self.invalid_expanded = True
        self.groups = [
            {"key": "valid", "title": "有效链接", "expanded": self.valid_expanded, "items": valid_items},
            {"key": "invalid", "title": "无效链接", "expanded": self.invalid_expanded, "items": invalid_items},
        ]

    def _render_groups(self, versions: list, entry: dict) -> None:
        self.versions = versions
        self.build_groups(versions)
        valid_n = len(self.groups[0]["items"])
        invalid_n = len(self.groups[1]["items"])
        self.meta_var.set("共 %d 个版本 · 有效 %d · 无效 %d" % (len(versions), valid_n, invalid_n))
        self._paint_list()

    def _paint_list(self) -> None:
        """把两个分组画进右栏 Listbox。"""
        self.ver_list.delete(0, "end")
        self.row_index.clear()
        self.row_header.clear()
        self.row_group.clear()
        has_any = False
        for gi, g in enumerate(self.groups):
            count = len(g["items"])
            header = "%s %s (%d)" % ("▼" if g["expanded"] else "▶", g["title"], count)
            self.ver_list.insert("end", header)
            row = self.ver_list.size() - 1
            self.row_header[row] = True
            self.row_index[row] = None
            self.row_group[row] = gi
            if count == 0:
                # 数量为 0：标题置灰、不可点击展开（FR-2.3）
                self.ver_list.itemconfigure(row, foreground=GREY, selectbackground="#fafafb",
                                            selectforeground=GREY)
                continue
            if not g["expanded"]:
                continue
            for idx in g["items"]:
                v = self.versions[idx]
                published = (v.get("published") or "").strip()
                label = "    · 版本 %s" % v.get("version", "")
                if published:
                    label += "        %s" % published
                if v.get("category"):
                    label += "    ← %s" % v["category"]
                self.ver_list.insert("end", label)
                row = self.ver_list.size() - 1
                self.row_header[row] = False
                self.row_index[row] = idx
                if not v.get("url"):
                    self.ver_list.itemconfigure(row, foreground=GREY)
            has_any = True
        if self.version_keyword and not has_any:
            self.ver_list.insert("end", "未找到匹配版本")
            self.ver_list.itemconfigure("end", foreground=GREY)

    def toggle_group(self, index: int) -> None:
        """点击分组标题行：展开/折叠该分组（数量为 0 时无效）。"""
        gi = self.row_group.get(index)
        if gi is None:
            return
        g = self.groups[gi]
        if not g["items"]:
            return
        g["expanded"] = not g["expanded"]
        if g["key"] == "valid":
            self.valid_expanded = g["expanded"]
        else:
            self.invalid_expanded = g["expanded"]
        self._paint_list()

    def _fetch_summary(self, name: str, kind: str) -> None:
        try:
            data = backend.fetch_summary(kind)
        except Exception as exc:  # 兜底：不让异常打断界面
            data = {"versions": [], "status": "error", "error": "%s: %s" % (type(exc).__name__, exc)}
        self.pending.put(("summary", name, data))

    def after_summary(self, name: str, data: dict) -> None:
        self.fast_poll = False
        self.loading_summaries.discard(name)
        for entry in self.entries:
            if entry.get("virtual") and entry["name"] == name:
                entry["versions"] = data.get("versions") or []
                entry["status"] = data.get("status", "ok")
                entry["error"] = data.get("error", "")
                entry["total"] = len(entry["versions"])
                break
        self.render_sidebar()
        if self.current_entry and self.current_entry["name"] == name:
            self.show_category(self.row_of_current() or 0)
        if data.get("status") == "ok":
            self.toast("已汇总 %d 个版本" % len(data.get("versions") or []), OK_GREEN)
        else:
            self.toast("汇总失败", ERR_RED)

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def on_version_click(self, event) -> None:
        index = self.ver_list.nearest(event.y)
        if index < 0 or index >= self.ver_list.size():
            return
        box = self.ver_list.bbox(index)
        if box is None or not (box[1] <= event.y <= box[1] + box[3]):
            return  # 点在空白处
        if self.row_header.get(index):        # 分组标题行 -> 展开/折叠
            self.toggle_group(index)
            return
        idx = self.row_index.get(index)
        if idx is None:
            return
        self.ver_list.selection_clear(0, "end")
        self.ver_list.selection_set(index)
        self.ver_list.activate(index)
        version = self.versions[idx]
        self.show_notes(version)
        url = version.get("url")
        if url:
            self.copy_to_clipboard(url)
            self.toast("复制成功", OK_GREEN)
        else:
            self.toast("该版本无下载链接", ERR_RED)
        self.open_btn.configure(state="normal" if url else "disabled")

    def on_version_select(self, _event=None) -> None:
        selection = self.ver_list.curselection()
        if not selection:
            return
        index = selection[0]
        idx = self.row_index.get(index)
        if idx is None:
            return
        version = self.versions[idx]
        self.show_notes(version)
        self.open_btn.configure(state="normal" if version.get("url") else "disabled")

    def on_open_link(self) -> None:
        selection = self.ver_list.curselection()
        if not selection:
            return
        index = selection[0]
        idx = self.row_index.get(index)
        if idx is not None and self.versions[idx].get("url"):
            webbrowser.open(self.versions[idx]["url"])

    # ------------------------------------------------------------------ #
    # 右栏版本搜索（与左栏搜索相互独立）
    # ------------------------------------------------------------------ #
    def on_ver_search_key(self, _event=None) -> None:
        text = self.ver_search_var.get()
        self.version_keyword = "" if text == VER_SEARCH_HINT else text.strip()
        self._search_last_detect = -1
        self._render_right()

    def on_ver_search_focus(self, _event=None) -> None:
        if self.ver_search_var.get() == VER_SEARCH_HINT:
            self.ver_search.delete(0, "end")
            self.ver_search.configure(fg="#33383f")

    def on_ver_search_blur(self, _event=None) -> None:
        text = self.ver_search_var.get()
        if not text.strip() or text == VER_SEARCH_HINT:
            self.ver_search.delete(0, "end")
            self.ver_search.insert(0, VER_SEARCH_HINT)
            self.ver_search.configure(fg="#9aa0a8")
            self.version_keyword = ""
        else:
            self.version_keyword = text.strip()
        self._render_right()

    def on_ver_search_escape(self, _event=None) -> None:
        self.ver_search.delete(0, "end")
        self.ver_search.insert(0, VER_SEARCH_HINT)
        self.ver_search.configure(fg="#9aa0a8")
        self.version_keyword = ""
        self._render_right()
        return "break"

    def on_refresh(self) -> None:
        self.loaded = False
        self.entries = []
        self.versions = []
        self.visible = []
        self.current_entry = None
        self.current_row = 0
        self.groups = []
        self.version_keyword = ""
        self._search_last_detect = -1
        self._rendered_checked = False
        self.loading_summaries.clear()
        self.ver_search_var.set("")
        self.ver_search.delete(0, "end")
        self.ver_search.insert(0, VER_SEARCH_HINT)
        self.ver_search.configure(fg="#9aa0a8")
        self.cat_list.delete(0, "end")
        self.ver_list.delete(0, "end")
        self.row_index.clear()
        self.row_header.clear()
        self.row_group.clear()
        self.title_var.set("正在刷新…")
        self.meta_var.set("")
        self.retry_btn.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        self.set_notes("")
        self.status_var.set("已请求重新抓取全部 %d 个分类…" % len(backend.CATEGORIES))
        backend.start_fetch(clear=True)

    def on_retry(self) -> None:
        if not self.current_entry or self.current_entry.get("virtual"):
            return
        name = self.current_entry["name"]
        self.retry_btn.configure(state="disabled")
        self.status_var.set("正在重新加载：%s …" % name)
        self.fast_poll = True

        def worker() -> None:
            try:
                entry = backend.reload_category(name)
            except Exception as exc:  # 兜底：绝不让异常打断界面
                entry = {"name": name, "status": "error", "versions": [], "error": repr(exc)}
            self.pending.put(("retry", name, entry))

        threading.Thread(target=worker, daemon=True).start()

    def after_retry(self, name: str, entry: dict) -> None:
        self.fast_poll = False
        for i, item in enumerate(self.entries):
            if item["name"] == name:
                entry.setdefault("initials", backend.category_initials(name))
                self.entries[i] = entry
                break
        self.render_sidebar()
        row = self.row_of_current()
        if row is not None:
            self.show_category(row)
        if entry.get("status") == "ok":
            self.toast("已重新加载 %d 个版本" % len(entry.get("versions") or []), OK_GREEN)
        else:
            self.toast("重新加载失败", ERR_RED)

    # ------------------------------------------------------------------ #
    # 小工具
    # ------------------------------------------------------------------ #
    def copy_to_clipboard(self, text: str) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
        except tk.TclError:  # 极端情况下退化：让用户手动复制
            from tkinter import simpledialog
            simpledialog.askstring("请手动复制链接", "请手动复制链接：", initialvalue=text)

    def show_notes(self, version: dict) -> None:
        notes = version.get("notes") or []
        self.set_notes("\n".join(notes) if notes else "（该版本没有更新说明）")
        url = version.get("url")
        self.status_var.set(("下载链接：%s" % url) if url else "该版本无下载链接")

    def set_notes(self, text: str) -> None:
        self.notes.configure(state="normal")
        self.notes.delete("1.0", "end")
        if text:
            self.notes.insert("1.0", text)
        self.notes.configure(state="disabled")

    def toast(self, text: str, color: str) -> None:
        self.toast_var.set(text)
        self.toast_label.configure(fg=color)
        if self.toast_job:
            self.root.after_cancel(self.toast_job)
        self.toast_job = self.root.after(2000, lambda: self.toast_var.set(""))


def _quit(root: tk.Tk) -> None:
    """退出：先停止后台抓取/检测线程，再直接结束进程。

    直接 os._exit 是为了跳过解释器退出阶段 concurrent.futures 对**非守护**工作线程的
    join —— 那些线程可能正卡在网络请求里，join 会让程序迟迟退不掉（或再按 Ctrl+C 抛异常）。
    """
    try:
        backend.shutdown()
    except Exception:
        pass
    try:
        root.destroy()
    except Exception:
        pass
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(0)


def main() -> None:
    try:  # Windows 高分屏下文字更清晰
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    DesktopApp(root)
    root.protocol("WM_DELETE_WINDOW", lambda: _quit(root))   # 点右上角关闭
    try:
        root.mainloop()
    except KeyboardInterrupt:                                # 控制台里按 Ctrl+C
        pass
    _quit(root)


if __name__ == "__main__":
    main()
