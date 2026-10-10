# -*- coding: utf-8 -*-
"""
银豹全版本下载链接速查工具（本地运行版）
========================================

用途：一次性抓取银豹官网「版本升级信息」页（https://www.pospal.cn/Version.aspx ）
全部 72 个软件分类的所有历史版本；前端左右两栏：左侧分类、右侧版本号；
点击版本号即把完整下载链接复制到剪贴板。

技术栈：Python 3 + Flask + requests + beautifulsoup4（后端）
        单个 HTML + 原生 CSS + 原生 JS（网页前端，无 CDN，离线可用）
        Tkinter（桌面 GUI 前端，见 gui.py）
        pypinyin 为可选依赖：用于分类搜索的拼音首字母/全拼，装不上时退化为内置首字母表

启动：  py -m pip install -r requirements.txt
        py gui.py            桌面窗口（推荐）
        python app.py        网页版（若 python 是 Microsoft Store 别名，请用  py app.py）

功能：  · 32 线程并发抓取 72 个分类（并发上限 32）
        · 左栏顶部两个汇总分类：「⬇ 有效链接」「⬇ 无效链接」（后者默认隐藏，可点按钮显示）
        · 左栏搜索框：模糊 / 汉字拼音首字母（bsyxt）/ 全拼（shouyin）/ 全字
所有抓取与解析都在本进程完成，前端只与本机后端通信，不存在跨域问题。
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List
from urllib.parse import urljoin

try:
    import requests
    from requests.adapters import HTTPAdapter
    from bs4 import BeautifulSoup
    from flask import Flask, Response, render_template, request
    try:
        from pypinyin import lazy_pinyin, Style
    except ModuleNotFoundError:  # pypinyin 为可选依赖：拼音搜索降级为内置首字母表
        lazy_pinyin = None
        Style = None
except ModuleNotFoundError as exc:  # 依赖没装时给出可执行的修复提示
    print("缺少依赖：%s" % exc.name)
    print("请先安装依赖（在当前使用的 Python 解释器下执行）：")
    print("    %s -m pip install -r requirements.txt" % sys.executable)
    print("例如： .venv\\Scripts\\python.exe -m pip install -r requirements.txt")
    raise SystemExit(1)

try:  # 可选依赖：lxml 是 C 实现、能释放 GIL，解析比 BeautifulSoup 快一个数量级
    from lxml import html as _lxml_html
except Exception:  # pragma: no cover - 没装 lxml 时回退 BeautifulSoup
    _lxml_html = None

# --------------------------------------------------------------------------- #
# 常量与配置
# --------------------------------------------------------------------------- #

BASE = "https://www.pospal.cn"
VERSION_PAGE = BASE + "/Version.aspx"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

REQUEST_TIMEOUT = 15      # 单次抓取请求超时（秒）
MAX_WORKERS = 96          # 默认抓取 / 检测并发线程数（可在 1 ~ MAX_WORKERS_LIMIT 之间调整）
MAX_WORKERS_LIMIT = 128   # 并发硬上限（在途网络请求数不得超过该值）
MAX_RETRIES = 2           # 每个分类抓取失败后重试 2 次
RETRY_BACKOFF = (1, 2)    # 抓取退避间隔：1 秒、2 秒
ALL_WAIT_SECONDS = 300    # /api/all 最长等待抓取完成的秒数

# 链接有效性检测（检测任务）
DETECT_TIMEOUT = 10       # 单次探测超时（秒）
DETECT_RETRIES = 2        # 网络错误（超时/DNS/拒绝/SSL）重试 2 次
DETECT_BACKOFF = 1        # 检测重试间隔（秒）
DETECT_METHODS = ("head", "get")  # HEAD 返回 405/501 时退化用 GET（仅读响应头）

# 72 个分类清单（顺序即左侧展示顺序，分类名原样使用）
CATEGORIES: List[str] = [
    "云后台",
    "PC版收银系统(正式版)",
    "PC版收银系统(Beta)",
    "PC版餐饮茶饮收银系统",
    "PC版中餐电脑收银(正式版)",
    "PC版中餐电脑收银(Beta版)",
    "PC版烘焙收银系统",
    "PC版零售收银系统",
    "PC版生鲜称重收银系统",
    "PC版服装收银系统",
    "PC版母婴收银系统",
    "PC版美发电脑收银",
    "PC桌娱电脑收银",
    "PC美业收银台",
    "微店",
    "小程序（新装修模板）",
    "小程序（其他模板）",
    "小程序（装修工具）",
    "CRM/小程序收银",
    "Android版收银系统",
    "Android版收银系统(中餐版)",
    "Android电子厨打",
    "Android移动收银",
    "Android移动收银(中餐版)",
    "Android生产溯源",
    "银豹餐饮排队等位手持端",
    "银豹餐饮排队等位TV端",
    "Android移动排队",
    "Android裱花间",
    "Android现烤间",
    "Android现烤屏显",
    "银豹智能盘点",
    "Android茶饮自助收银",
    "Android零售自助收银（横版）",
    "Android零售自助收银（竖版）",
    "Android AI自助收银",
    "Android盘点",
    "Android自助点餐",
    "Android分拣系统",
    "iPad版收银系统(中餐版)",
    "iOS手机收银系统(中餐版)",
    "银豹收银Mac版",
    "iPad版收银系统",
    "iPad版收银系统（旧版）",
    "iOS手机收银系统",
    "iOS手机收银系统（旧版）",
    "iOS我的店铺V3",
    "Android我的店铺V3",
    "Android银豹刷脸考勤",
    "Android掌上批发",
    "Android AI标签秤",
    "Android AI标签秤(竖版)",
    "银豹商贸批发IOS版",
    "银豹商贸批发安卓版",
    "巡店系统IOS版",
    "巡店系统安卓版",
    "生产工序安卓版",
    "美业app安卓版",
    "美业appIOS版",
    "骑手app安卓版",
    "仓库移动作业app安卓版",
    "银豹门闸系统",
    "餐饮智慧屏TV端",
    "餐饮智能预订电话端",
    "银豹客户数据营销中台(简称CDP)",
    "Android移动拣货",
    "Android轻餐KDS",
    "桌娱App安卓版",
    "Android银豹AI出品",
    "Android裱花KDS",
    "Android正餐KDS",
    "Android门店现烤",
]

# --------------------------------------------------------------------------- #
# 搜索辅助：分类名的拼音首字母 + 两个汇总分类
# --------------------------------------------------------------------------- #

VALID_SUMMARY = "有下载链接"    # 汇总分类：全部分类里带下载链接的版本（按 url 是否存在计）
INVALID_SUMMARY = "无下载链接"    # 汇总分类：全部分类里没有下载链接的历史版本（按 url 是否存在计）
SUMMARY_ICON = "⬇"             # 汇总分类最左侧的下载图标

try:  # 可选依赖：没有它也能用（走下面的离线兜底表）
    from pypinyin import lazy_pinyin as _lazy_pinyin
except Exception:  # pragma: no cover
    _lazy_pinyin = None

_INITIALS_FALLBACK: Dict[str, str] = {
    "有效链接": "yxlj",
    "无效链接": "wxlj",
    "云后台": "yht",
    "PC版收银系统(正式版)": "pcbsyxtzsb",
    "PC版收银系统(Beta)": "pcbsyxtbeta",
    "PC版餐饮茶饮收银系统": "pcbcycysyxt",
    "PC版中餐电脑收银(正式版)": "pcbzcdnsyzsb",
    "PC版中餐电脑收银(Beta版)": "pcbzcdnsybetab",
    "PC版烘焙收银系统": "pcbhbsyxt",
    "PC版零售收银系统": "pcblssyxt",
    "PC版生鲜称重收银系统": "pcbsxczsyxt",
    "PC版服装收银系统": "pcbfzsyxt",
    "PC版母婴收银系统": "pcbmysyxt",
    "PC版美发电脑收银": "pcbmfdnsy",
    "PC桌娱电脑收银": "pczydnsy",
    "PC美业收银台": "pcmysyt",
    "微店": "wd",
    "小程序（新装修模板）": "xcxxzxmb",
    "小程序（其他模板）": "xcxqtmb",
    "小程序（装修工具）": "xcxzxgj",
    "CRM/小程序收银": "crmxcxsy",
    "Android版收银系统": "androidbsyxt",
    "Android版收银系统(中餐版)": "androidbsyxtzcb",
    "Android电子厨打": "androiddzcd",
    "Android移动收银": "androidydsy",
    "Android移动收银(中餐版)": "androidydsyzcb",
    "Android生产溯源": "androidscsy",
    "银豹餐饮排队等位手持端": "ybcypddwscd",
    "银豹餐饮排队等位TV端": "ybcypddwtvd",
    "Android移动排队": "androidydpd",
    "Android裱花间": "androidbhj",
    "Android现烤间": "androidxkj",
    "Android现烤屏显": "androidxkpx",
    "银豹智能盘点": "ybznpd",
    "Android茶饮自助收银": "androidcyzzsy",
    "Android零售自助收银（横版）": "androidlszzsyhb",
    "Android零售自助收银（竖版）": "androidlszzsysb",
    "Android AI自助收银": "androidaizzsy",
    "Android盘点": "androidpd",
    "Android自助点餐": "androidzzdc",
    "Android分拣系统": "androidfjxt",
    "iPad版收银系统(中餐版)": "ipadbsyxtzcb",
    "iOS手机收银系统(中餐版)": "iossjsyxtzcb",
    "银豹收银Mac版": "ybsymacb",
    "iPad版收银系统": "ipadbsyxt",
    "iPad版收银系统（旧版）": "ipadbsyxtjb",
    "iOS手机收银系统": "iossjsyxt",
    "iOS手机收银系统（旧版）": "iossjsyxtjb",
    "iOS我的店铺V3": "ioswddpv3",
    "Android我的店铺V3": "androidwddpv3",
    "Android银豹刷脸考勤": "androidybslkq",
    "Android掌上批发": "androidzspf",
    "Android AI标签秤": "androidaibqc",
    "Android AI标签秤(竖版)": "androidaibqcsb",
    "银豹商贸批发IOS版": "ybsmpfiosb",
    "银豹商贸批发安卓版": "ybsmpfazb",
    "巡店系统IOS版": "xdxtiosb",
    "巡店系统安卓版": "xdxtazb",
    "生产工序安卓版": "scgxazb",
    "美业app安卓版": "myappazb",
    "美业appIOS版": "myappiosb",
    "骑手app安卓版": "qsappazb",
    "仓库移动作业app安卓版": "ckydzyappazb",
    "银豹门闸系统": "ybmzxt",
    "餐饮智慧屏TV端": "cyzhptvd",
    "餐饮智能预订电话端": "cyznyddhd",
    "银豹客户数据营销中台(简称CDP)": "ybkhsjyxztjccdp",
    "Android移动拣货": "androidydjh",
    "Android轻餐KDS": "androidqckds",
    "桌娱App安卓版": "zyappazb",
    "Android银豹AI出品": "androidybaicp",
    "Android裱花KDS": "androidbhkds",
    "Android正餐KDS": "androidzckds",
    "Android门店现烤": "androidmdxk",
}

_initials_cache: Dict[str, str] = {}
_fullpy_cache: Dict[str, str] = {}


def category_initials(name: str) -> str:
    """分类名的搜索键：英文/数字整段保留 + 汉字取拼音首字母（统一小写）。

    例：PC版零售收银系统 -> pcblssyxt ；小程序（新装修模板） -> xcxxzxmb
    """
    if name in _initials_cache:
        return _initials_cache[name]
    value = ""
    if _lazy_pinyin is not None:
        try:
            parts: List[str] = []
            pos = 0
            for match in re.finditer(r"[0-9A-Za-z]+", name):
                chinese = name[pos:match.start()]
                if chinese:
                    parts.append("".join(p[0] for p in _lazy_pinyin(chinese)))
                parts.append(match.group(0).lower())
                pos = match.end()
            tail = name[pos:]
            if tail:
                parts.append("".join(p[0] for p in _lazy_pinyin(tail)))
            value = re.sub(r"[^a-z0-9]", "", "".join(parts))
        except Exception:  # pragma: no cover - 拼音库异常时退化为纯文本匹配
            value = ""
    if not value:
        value = _INITIALS_FALLBACK.get(name, "")
    _initials_cache[name] = value
    return value


def category_fullpy(name: str) -> str:
    """分类名的全拼搜索键（去标点、小写），如 PC版零售收银系统 -> pcbanshouyinxitong。"""
    if name in _fullpy_cache:
        return _fullpy_cache[name]
    value = ""
    if _lazy_pinyin is not None:
        try:
            value = re.sub(r"[^a-z0-9]", "", "".join(_lazy_pinyin(name)).lower())
        except Exception:  # pragma: no cover
            value = ""
    _fullpy_cache[name] = value
    return value


# --------------------------------------------------------------------------- #
# 版本搜索辅助（左栏搜索分类、右栏搜索版本号共用 normalize 规则）
# --------------------------------------------------------------------------- #

_NORMALIZE_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]")


def normalize(text: str) -> str:
    """搜索归一化：转小写，仅保留英文字母、数字、汉字，删除其余全部字符。

    用正则（C 实现）实现，比逐字符的 Python 循环快数倍，避免占用 GIL。
    """
    return _NORMALIZE_RE.sub("", (text or "").lower())


_char_initial_cache: Dict[str, str] = {}


def version_initials(text: str) -> str:
    """版本文本转「拼音首字母形式」：每个汉字取拼音首字母（无声调），非汉字原样保留。

    逐字查表并缓存（约 1500 个不同汉字即可覆盖全部版本文本），比按整段调用 pypinyin
    快两个数量级（实测 28s → 0.4s），避免它在多线程抓取时吃满 GIL。
    """
    if not text:
        return ""
    if _lazy_pinyin is None:
        return text
    out: List[str] = []
    for ch in text:
        if 0x4E00 <= ord(ch) <= 0x9FFF:            # 汉字
            init = _char_initial_cache.get(ch)
            if init is None:
                try:
                    arr = _lazy_pinyin(ch, style=Style.FIRST_LETTER)
                    init = arr[0].lower() if arr and arr[0] else ch
                except Exception:  # pragma: no cover
                    init = ch
                _char_initial_cache[ch] = init
            out.append(init)
        else:
            out.append(ch)                          # 非汉字原样保留
    return "".join(out)


def match_version(keyword: str, version: Dict[str, Any]) -> bool:
    """右栏版本搜索匹配：规则 A（子串）或规则 D（拼音首字母）任一命中即匹配。

    keyword 可含字母/数字间空格（如 'v 4 7 1'），normalize 后自然消失，仅作容错分隔符，
    不作为多关键字 AND。汉字按规则 A 直接子串匹配（已由 normalize 保留汉字）。
    优先复用 fetch 阶段预计算的 hay/haypy（后台线程算好），搜索即时；缺省时再现场算。
    """
    key = normalize(keyword)
    if not key:
        return True
    hay = version.get("hay")
    haypy = version.get("haypy")
    if hay is None or haypy is None:
        haystack = "%s %s %s" % (
            version.get("version", ""), version.get("published", ""),
            " ".join(version.get("notes") or []),
        )
        if hay is None:
            hay = normalize(haystack)
        if haypy is None:
            haypy = normalize(version_initials(haystack))
    return key in hay or key in haypy


# --------------------------------------------------------------------------- #
# 链接有效性检测（检测任务）
# --------------------------------------------------------------------------- #

def probe_link(url: str, session: requests.Session | None = None) -> bool:
    """探测单个下载链接是否有效。

    有效：2xx；或 3xx 跟随重定向后最终为 2xx。HEAD 返回 405/501 时退化用 GET（stream，只读响应头）。
    无效：4xx/5xx、连接超时、DNS 失败、连接被拒、SSL 错误；网络错误重试 2 次（1s 间隔），
    仅 HTTP 响应码算最终结论、不重试。返回 True/False。
    """
    session = session or get_session()
    for attempt in range(DETECT_RETRIES + 1):
        try:
            resp = session.head(url, timeout=DETECT_TIMEOUT, allow_redirects=True)
            try:
                if resp.status_code in (405, 501):
                    resp.close()
                    resp = session.get(url, timeout=DETECT_TIMEOUT, allow_redirects=True, stream=True)
                return resp.status_code < 400
            finally:
                resp.close()
        except Exception:  # 任何网络/解析异常都按无效处理并重试，避免检测线程崩溃
            if attempt < DETECT_RETRIES:
                time.sleep(DETECT_BACKOFF)
    return False


def _augment(entry: Dict[str, Any]) -> Dict[str, Any]:
    """为分类的每个版本预计算搜索键（前端过滤版本号用，避免浏览器端引入拼音库）。

    hay   = 版本号+发布时间+更新说明 归一化后的子串载体（覆盖规则 A/C）
    haypy = 上述文本的拼音首字母形式归一化（覆盖规则 D）
    """
    for v in entry.get("versions") or []:
        if v.get("hay") is not None and v.get("haypy") is not None:
            continue  # 已计算过（如抓取阶段已算），幂等跳过，避免重复跑拼音
        hay = "%s %s %s" % (
            v.get("version", ""), v.get("published", ""),
            " ".join(v.get("notes") or []),
        )
        v["hay"] = normalize(hay)
        v["haypy"] = normalize(version_initials(hay))
    return entry


def detect_category(name: str) -> None:
    """对单个分类的全部下载链接做有效性检测，结果写回缓存（供重抓/重试）。"""
    with _lock:
        entry = _cache.get(name)
        if not entry or entry.get("status") != "ok":
            return
        versions = list(entry.get("versions") or [])
    jobs = [(i, v["url"]) for i, v in enumerate(versions) if v.get("url")]
    if not jobs:
        with _lock:
            e = _cache.get(name)
            if e:
                e["checked"] = True
        return
    # 单分类重试也提交到共享池，避免按版本逐条串行探测。
    futures = {_executor.submit(_detect_task, url): i for i, url in jobs}
    results = {}
    for future in as_completed(futures):
        i = futures[future]
        try:
            results[i] = future.result()
        except Exception:
            results[i] = False
    with _lock:
        e = _cache.get(name)
        if not e:
            return
        for i, alive in results.items():
            if i < len(e.get("versions") or []):
                e["versions"][i]["alive"] = alive
        e["checked"] = True


def build_summary(kind: str) -> Dict[str, Any]:
    """汇总分类：把 72 个分类里带链接 / 不带链接的版本分别汇总成一个列表。

    kind = "valid"（有效链接） 或 "invalid"（无效链接）。
    """
    want_url = kind == "valid"
    items: List[Dict[str, Any]] = []
    with _lock:
        sources = [(name, _cache.get(name)) for name in CATEGORIES]
    for name, entry in sources:
        if not entry or entry.get("status") != "ok":
            continue
        for version in entry.get("versions") or []:
            url = version.get("url") or None
            if bool(url) != want_url:
                continue
            items.append({
                "version": version.get("version", ""),
                "url": url,
                "published": version.get("published", ""),
                "notes": version.get("notes") or [],
                "category": name,          # 来源分类，便于分辨是哪个软件
            })
    name = VALID_SUMMARY if want_url else INVALID_SUMMARY
    desc = ("全部分类中带下载链接的版本，点击版本号即可复制"
            if want_url else "全部分类中没有下载链接的历史版本（不可点击复制）")
    result = {
        "name": name,
        "status": "ok",
        "virtual": True,
        "kind": kind,
        "description": desc,
        "initials": category_initials(name),
        "fullpy": category_fullpy(name),
        "versions": items,
        "error": "",
    }
    _augment(result)  # 后台/请求线程预计算 hay/haypy，供网页端搜索
    return result


def summary_counts() -> Dict[str, int]:
    """统计有效链接 / 无效链接的版本数量（不展开明细，开销很小）。"""
    valid = invalid = 0
    with _lock:
        sources = [_cache.get(name) for name in CATEGORIES]
    for entry in sources:
        if not entry or entry.get("status") != "ok":
            continue
        for version in entry.get("versions") or []:
            if version.get("url"):
                valid += 1
            else:
                invalid += 1
    return {"valid": valid, "invalid": invalid}


def detect_progress(entry: Dict[str, Any]) -> tuple:
    """单个分类的链接检测进度：(已确定数, 总数)。

    「无下载链接」的版本没有可探测的链接，视为已确定；其余版本以 alive 是否已写回判定。
    用于右栏「检测中 x/N」的实时进度显示。
    """
    versions = entry.get("versions") or []
    total = len(versions)
    done = 0
    for v in versions:
        if not v.get("url") or v.get("alive") is not None:
            done += 1
    return done, total


def virtual_templates() -> List[Dict[str, Any]]:
    """汇总分类的元信息（前端左栏用它渲染，明细按需加载）。

    按要求只保留「无下载链接」汇总分类，已移除「有下载链接」分类。
    """
    counts = summary_counts()
    templates = []
    for kind, name, desc in (
        ("invalid", INVALID_SUMMARY, "全部分类中没有下载链接的历史版本（不可点击复制）"),
    ):
        templates.append({
            "name": name, "kind": kind, "description": desc,
            "initials": category_initials(name), "fullpy": category_fullpy(name),
            "total": counts[kind],
        })
    return templates


def fetch_summary(kind: str) -> Dict[str, Any]:
    """阻塞式获取汇总分类明细（供桌面端在后台线程里调用）。"""
    payload = build_summary(kind)
    payload["total"] = len(payload["versions"])
    return payload


def _make_file_handler() -> logging.Handler:
    """无控制台（打包成 --windowed EXE）时落盘日志，便于排查。"""
    candidates = []
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP")
    if base:
        candidates.append(os.path.join(base, "PospalVersion"))
    if getattr(sys, "frozen", False):
        candidates.append(os.path.dirname(sys.executable))
    else:
        candidates.append(os.path.dirname(os.path.abspath(__file__)))
    for directory in candidates:
        try:
            os.makedirs(directory, exist_ok=True)
            return logging.FileHandler(os.path.join(directory, "pospal_version.log"), encoding="utf-8")
        except Exception:
            continue
    return logging.NullHandler()


def _setup_logging() -> None:
    """配置根日志。

    有控制台时输出到 stderr；打包成窗口版（sys.stderr is None）时写日志文件，
    否则 logging 的 StreamHandler 会在 emit 时因 stderr 为 None 反复报错。
    """
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
    handler: logging.Handler = logging.StreamHandler(sys.stderr) if sys.stderr is not None else _make_file_handler()
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers[:] = [handler]


_setup_logging()
log = logging.getLogger("pospal-version")

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
try:  # Flask >= 2.2 / 3.x：让 JSON 直接输出中文，便于人工查看接口
    app.json.ensure_ascii = False
except Exception:  # pragma: no cover - 兼容旧版本 Flask
    pass


class PacedResponse(Response):
    """响应体写完后停顿一小会儿再让服务器关闭连接。

    背景（实测）：Werkzeug 开发服务器会强制 `Connection: close`（它不支持 keep-alive），
    对超过 ~1MB 的响应，写完的瞬间就关闭 socket。本机若装有会接管本地 HTTP 的过滤类软件
    （例如 AdGuard 会向本地页面注入脚本），大响应有很高概率被 RST 掉，客户端报
    `ConnectionResetError(10054)`，而服务端日志仍是 200。加 0.15 秒的收尾停顿后，
    实测 16/16 全部成功（不加时 4/16），对本地单用户使用代价可忽略。
    """

    def __init__(self, body: bytes, *, trailing_pause: float = 0.0, **kwargs: Any) -> None:
        super().__init__(body, **kwargs)
        self._trailing_pause = trailing_pause

    def __iter__(self):
        for chunk in super().__iter__():
            yield chunk
        if self._trailing_pause:
            time.sleep(self._trailing_pause)


def json_response(payload: Any, status: int = 200) -> Response:
    """输出 JSON；数据量大时按需 gzip 压缩（全量数据约 3.8MB，压缩后约 1.1MB）。"""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {"Cache-Control": "no-store"}
    if "gzip" in request.headers.get("Accept-Encoding", "").lower() and len(body) > 4096:
        body = gzip.compress(body, 6)
        headers["Content-Encoding"] = "gzip"
        headers["Vary"] = "Accept-Encoding"
    response = PacedResponse(
        body,
        trailing_pause=0.15 if len(body) > 262144 else 0.0,
        status=status,
        mimetype="application/json",
        headers=headers,
    )
    return response


# --------------------------------------------------------------------------- #
# HTML 解析
# --------------------------------------------------------------------------- #

def _parse_versions_bs(html: str) -> List[Dict[str, Any]]:
    """BeautifulSoup（标准库 html.parser）解析；作为 lxml 不可用时的兜底。"""
    soup = BeautifulSoup(html, "html.parser")
    numbers = soup.select("div.v_number")
    result: List[Dict[str, Any]] = []
    for num in numbers:
        a = num.find("a")
        if a is not None:
            version = a.get_text(" ", strip=True).replace("·", "").replace("版本", "").strip()
            url = urljoin(BASE, a.get("href", ""))
        else:
            version = num.get_text(" ", strip=True).replace("·", "").replace("版本", "").strip()
            url = None
        time_div = num.find_next_sibling("div", class_="v_time")
        area_div = num.find_next_sibling("div", class_="v_area")
        published = time_div.get_text(strip=True).replace("发布于", "") if time_div else ""
        notes: List[str] = []
        if area_div:
            notes = [s.strip() for s in area_div.get_text("\n", strip=True).split("\n") if s.strip()]
        result.append({"version": version, "url": url or None, "published": published, "notes": notes})
    return result


# 预处理：<br>/</br> 归一为换行；把「<」后接非字母（如 <300*300>）转义，避免被当成标签
_BR_RE = re.compile(r"<\s*/?\s*br\s*/?\s*>", re.I)
_LT_RE = re.compile(r"<(?![a-zA-Z/!])")
_V_NUMBER_XPATH = '//div[contains(concat(" ", normalize-space(@class), " "), " v_number ")]'


def _el_text(el) -> str:
    """按 BeautifulSoup get_text(" ", strip=True) 的语义取文本：各文本节点 strip 后用空格连接。"""
    return " ".join(t.strip() for t in el.itertext() if t.strip())


def _parse_versions_lxml(html: str) -> List[Dict[str, Any]]:
    """lxml.html 解析（C 实现、可释放 GIL，多线程下近似线性加速）。

    版本块结构：div.v_number（可能含 <a> 承载下载链接）→ 可选 div.v_time → 可选 div.v_area。
    """
    doc = _lxml_html.fromstring(_LT_RE.sub("&lt;", _BR_RE.sub("\n", html)))
    result: List[Dict[str, Any]] = []
    for num in doc.xpath(_V_NUMBER_XPATH):
        a = num.find("a")
        if a is not None:
            version = _el_text(a).replace("·", "").replace("版本", "").strip()
            url = urljoin(BASE, a.get("href") or "")
        else:
            version = _el_text(num).replace("·", "").replace("版本", "").strip()
            url = None
        time_el = area_el = None
        nxt = num.getnext()
        if nxt is not None:
            cls = (nxt.get("class") or "").split()
            if "v_time" in cls:
                time_el = nxt
                nxt = nxt.getnext()
                if nxt is not None and "v_area" in (nxt.get("class") or "").split():
                    area_el = nxt
            elif "v_area" in cls:      # 有的块没有 v_time，日期直接写在版本号里
                area_el = nxt
        published = _el_text(time_el).replace("发布于", "") if time_el is not None else ""
        notes: List[str] = []
        if area_el is not None:
            notes = [p.strip() for t in area_el.itertext() for p in re.split(r"\r?\n", t) if p.strip()]
        result.append({"version": version, "url": url or None, "published": published, "notes": notes})
    return result


def parse_versions(html: str) -> List[Dict[str, Any]]:
    """解析单个分类页面的 HTML，返回版本列表。

    优先用 lxml（快，且能并行）；未安装或解析异常时回退 BeautifulSoup，保证鲁棒性。
    """
    if _lxml_html is not None:
        try:
            return _parse_versions_lxml(html)
        except Exception:  # 兜底：lxml 解析异常不致命
            log.warning("lxml 解析失败，回退 BeautifulSoup", exc_info=True)
    return _parse_versions_bs(html)


def decode_response(resp: requests.Response) -> str:
    """稳健地把响应字节流解码为文本（优先响应头 charset，其次页面 meta，最后 utf-8）。"""
    raw = resp.content
    candidates: List[str] = []
    declared = (resp.encoding or "").strip()
    if declared and declared.lower() not in ("iso-8859-1", "ascii"):
        candidates.append(declared)
    m = re.search(rb"""charset=["']?([\w\-]+)""", raw[:4096], re.IGNORECASE)
    if m:
        candidates.append(m.group(1).decode("ascii", "ignore"))
    candidates.extend(["utf-8", "gb18030"])
    for enc in candidates:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# 抓取与缓存
# --------------------------------------------------------------------------- #

_local = threading.local()
_worker_config_lock = threading.RLock()
_worker_count = max(1, min(MAX_WORKERS, MAX_WORKERS_LIMIT))

# 全局唯一线程池：抓取任务与检测任务共用，max_workers 即并发硬上限（≤ MAX_WORKERS_LIMIT）。
# 禁止再创建第二个线程池，从而保证在途网络请求总数不超过上限。
_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS_LIMIT)
# 再叠加一个信号量：实际在途网络请求数 = min(_worker_count, MAX_WORKERS_LIMIT)，
# 因此并发=1 时退化为严格串行，并发=上限时峰值也恰好受控。
_sem = threading.BoundedSemaphore(_worker_count)
# 退出时置位：让后台抓取 / 检测线程尽快返回，不再提交新任务
_stop_event = threading.Event()


def get_worker_count() -> int:
    with _worker_config_lock:
        return _worker_count


def set_worker_count(value: int) -> int:
    """设置下一批抓取使用的并发数，始终限制在 1..MAX_WORKERS_LIMIT。"""
    global _worker_count, _sem
    value = max(1, min(int(value), MAX_WORKERS_LIMIT))
    with _worker_config_lock:
        if _progress.get("running"):
            raise RuntimeError("抓取进行中，请等待当前任务完成")
        _worker_count = value
        _sem = threading.BoundedSemaphore(value)
    return value


def _fetch_task(name: str):
    with _sem:
        return fetch_category(name)


def _detect_task(url: str):
    with _sem:
        return probe_link(url)


def shutdown(wait: bool = False) -> None:
    """退出前调用：停止后台抓取 / 检测并释放线程池。

    关键作用：Python 3.9+ 的 ThreadPoolExecutor 工作线程是**非守护**的，解释器退出时
    concurrent.futures 的 atexit 会 join 它们；若此时工作线程正卡在网络请求里，退出就会
    长时间阻塞（再按一次 Ctrl+C 便抛出 join 处的 KeyboardInterrupt）。这里先让批次失效、
    取消尚未开始的任务，再（可选）等待，退出码由调用方决定。
    """
    global _generation
    _stop_event.set()
    with _lock:
        _generation += 1                     # 让在途的抓取 / 检测批次失效
        _progress["running"] = False
        _progress["detecting"] = False
    try:
        _executor.shutdown(wait=wait, cancel_futures=True)
    except TypeError:                        # Python 3.8 不支持 cancel_futures
        try:
            _executor.shutdown(wait=wait)
        except Exception:
            pass
    except Exception:
        pass


_shared_session: Any = None
_session_lock = threading.Lock()


def get_session() -> requests.Session:
    """全局共享 Session（连接池按并发上限配置）。

    跨线程复用同一个连接池，能显著复用 TCP/TLS 连接（免去每线程重复握手），
    实测比「每线程各自一个 Session」快很多（72 个分类从数秒降到亚秒级）。
    requests 的 HTTPAdapter/urllib3 连接池本身是线程安全的。
    """
    global _shared_session
    if _shared_session is None:
        with _session_lock:
            if _shared_session is None:
                session = requests.Session()
                session.headers.update(HEADERS)
                adapter = HTTPAdapter(
                    pool_connections=MAX_WORKERS_LIMIT,
                    pool_maxsize=MAX_WORKERS_LIMIT,
                    max_retries=0,
                )
                session.mount("http://", adapter)
                session.mount("https://", adapter)
                _shared_session = session
    return _shared_session


def fetch_category(name: str, retries: int = MAX_RETRIES) -> Dict[str, Any]:
    """抓取并解析单个分类；失败重试 retries 次，采用 1s / 2s 简单退避。"""
    last_error = ""
    for attempt in range(retries + 1):
        try:
            resp = get_session().get(
                VERSION_PAGE,
                params={"MenuItem": name},
                timeout=REQUEST_TIMEOUT,
            )
            try:
                resp.raise_for_status()
                versions = parse_versions(decode_response(resp))
            finally:
                resp.close()
            for v in versions:
                v.setdefault("alive", None)  # None=未检测
            entry = {"name": name, "status": "ok", "versions": versions,
                     "error": "", "checked": False}
            _augment(entry)  # 后台线程预计算 hay/haypy，避免主线程做拼音计算卡 UI
            log.info("抓取成功 %s：%d 个版本", name, len(versions))
            return entry
        except Exception as exc:  # 网络异常、超时、状态码异常等
            last_error = "%s: %s" % (type(exc).__name__, exc)
            log.warning("抓取失败 %s（第 %d 次）：%s", name, attempt + 1, last_error)
            if attempt < retries:
                time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])
    return {"name": name, "status": "error", "versions": [], "error": last_error,
            "checked": False}


_cache: Dict[str, Dict[str, Any]] = {}
_lock = threading.RLock()
_generation = 0
_finished = threading.Event()
_fetch_thread = None
_progress: Dict[str, Any] = {
    "done": 0,
    "total": len(CATEGORIES),
    "ok": 0,
    "error": 0,
    "running": False,
    # 检测阶段进度
    "detect_total": 0,
    "detect_done": 0,
    "detecting": False,
}


def _snapshot_progress() -> Dict[str, Any]:
    with _lock:
        return dict(_progress)


def get_progress() -> Dict[str, Any]:
    """当前抓取进度快照（桌面 GUI 端复用）。"""
    return _snapshot_progress()


def _run_fetch(generation: int) -> None:
    """后台线程：先并发抓取全部 72 个分类，抓取完成后再并发检测全部下载链接有效性。

    抓取任务与检测任务都提交到全局唯一的 _executor，因此任意时刻在途网络请求数
    不会超过其 max_workers（= MAX_WORKERS_LIMIT），满足「禁止嵌套第二个线程池」的要求。
    """
    names = list(CATEGORIES)
    futures = {_executor.submit(_fetch_task, name): name for name in names}
    for future in as_completed(futures):
        if _stop_event.is_set():                 # 应用正在退出，尽快返回
            return
        name = futures[future]
        try:
            entry = future.result()
        except Exception as exc:  # 兜底：单个分类异常不得影响其它分类
            entry = {
                "name": name,
                "status": "error",
                "versions": [],
                "error": "%s: %s" % (type(exc).__name__, exc),
                "checked": False,
            }
        with _lock:
            if generation != _generation:  # 已被新的抓取批次取代，丢弃结果
                continue
            _cache[name] = entry
            _progress["done"] += 1
            _progress["ok" if entry["status"] == "ok" else "error"] += 1
    # 抓取阶段结束：立即置 running=False 并放行 _finished，
    # 让桌面端 / 网页端马上拿到 72 个分类并完成首屏渲染，
    # 而不是等几分钟的链接检测跑完（这正是「抓取完成后界面卡死」的原因）。
    with _lock:
        if generation == _generation:
            _progress["running"] = False
            _finished.set()
    # 链接有效性检测在后台继续（仍在同一个线程池里，受并发上限约束），
    # 通过 detecting / detect_done / detect_total 单独汇报进度。
    if _stop_event.is_set():                     # 应用正在退出，不再启动检测
        return
    if generation == _generation:
        try:
            _run_detect(generation)
        except Exception as exc:  # 兜底：检测阶段无论如何不能让抓取线程崩溃
            log.error("检测阶段异常：%s: %s", type(exc).__name__, exc)
        finally:
            with _lock:
                _progress["detecting"] = False


def _run_detect(generation: int) -> None:
    """检测阶段：对所有「带 url 且尚未检测」的版本做 HEAD/GET 有效性探测。"""
    jobs: List[tuple] = []  # (category_name, version_index, url)
    remaining: Dict[str, int] = {}
    with _lock:
        for name in CATEGORIES:
            entry = _cache.get(name)
            if not entry or entry.get("status") != "ok":
                continue
            versions = entry.get("versions") or []
            n = 0
            for i, v in enumerate(versions):
                if v.get("url") and v.get("alive") is None:
                    jobs.append((name, i, v["url"]))
                    n += 1
            if n:
                remaining[name] = n
            else:
                # 没有可检测的链接（该分类所有版本都无下载链接）或已检测过：
                # 直接标记完成，否则右栏会永远停在「检测中」。
                entry["checked"] = True
    total = len(jobs)
    with _lock:
        _progress["detect_total"] = total
        _progress["detect_done"] = 0
        _progress["detecting"] = total > 0
    if not jobs:
        with _lock:
            _progress["detecting"] = False
        return
    futures = {_executor.submit(_detect_task, url): (name, i) for name, i, url in jobs}
    for future in as_completed(futures):
        if _stop_event.is_set():                 # 应用正在退出，尽快返回
            break
        name, i = futures[future]
        try:
            alive = future.result()
        except Exception:  # 兜底：检测异常视为无效，不阻断其它
            alive = False
        with _lock:
            if generation != _generation:
                continue
            entry = _cache.get(name)
            if entry:
                versions = entry.get("versions") or []
                if i < len(versions):
                    versions[i]["alive"] = alive
                left = remaining.get(name, 1) - 1
                if left <= 0:
                    entry["checked"] = True
                    remaining.pop(name, None)
                else:
                    remaining[name] = left
            _progress["detect_done"] += 1
    with _lock:
        _progress["detecting"] = False


def start_fetch(clear: bool = False) -> bool:
    """启动一次全量抓取；若已在抓取中或正在退出则返回 False。"""
    global _generation, _fetch_thread
    if _stop_event.is_set():        # 应用正在退出，不再接受新的抓取
        return False
    with _lock:
        if _progress["running"]:
            return False
        if clear:
            _cache.clear()
        _generation += 1
        generation = _generation
        _progress.update({
            "done": 0, "ok": 0, "error": 0, "running": True,
            "detect_total": 0, "detect_done": 0, "detecting": False,
        })
        _finished.clear()
        _fetch_thread = threading.Thread(
            target=_run_fetch, args=(generation,), name="pospal-fetch", daemon=True
        )
        thread = _fetch_thread
    thread.start()
    log.info("开始抓取全部 %d 个分类", len(CATEGORIES))
    return True


def lookup_category(name: str) -> Dict[str, Any]:
    """按分类名取缓存；命中失败结果时返回 None 以便重新抓取。"""
    with _lock:
        return _cache.get(name)


def _normalize(name: str) -> str:
    return (name or "").strip().replace("（", "(").replace("）", ")").replace(" ", "")


def resolve_category(name: str) -> str:
    """把请求里的分类名匹配回官方分类名（容忍全角/半角括号与空格差异）。"""
    name = (name or "").strip()
    if name in CATEGORIES:
        return name
    target = _normalize(name)
    for category in CATEGORIES:
        if _normalize(category) == target:
            return category
    return ""


def snapshot_all() -> List[Dict[str, Any]]:
    """按 CATEGORIES 顺序返回全部分类的数据快照（网页端与桌面端共用）。

    每个分类附加 `initials`（拼音首字母搜索键）与 `virtual=False` 标记。
    """
    with _lock:
        result = []
        for name in CATEGORIES:
            entry = _cache.get(name)
            if entry is None:
                entry = {"name": name, "status": "error", "versions": [], "error": "尚未抓取"}
            else:
                entry = dict(entry)
            entry["virtual"] = False
            entry["initials"] = category_initials(name)
            entry["fullpy"] = category_fullpy(name)
            entry.setdefault("description", "")
            result.append(entry)
        return result


def reload_category(name: str) -> Dict[str, Any]:
    """强制重新抓取单个分类并重新检测其链接，写回缓存（供「点击重试」使用）。"""
    entry = fetch_category(name)
    with _lock:
        cached = _cache.get(name)
        if entry["status"] == "ok" or cached is None or cached.get("status") != "ok":
            _cache[name] = entry
        else:
            entry = cached  # 重试失败时保留原有成功结果，避免把好数据冲掉
    if entry["status"] == "ok":
        detect_category(name)  # 重新检测该分类的链接有效性
    return entry


# --------------------------------------------------------------------------- #
# 路由 / 接口
# --------------------------------------------------------------------------- #

@app.route("/")
def index():
    return render_template("index.html", total=len(CATEGORIES))


@app.route("/api/progress")
def api_progress():
    return json_response(get_progress())


@app.route("/api/config")
def api_config():
    """公开当前抓取并发配置，网页端可据此显示实际线程数。"""
    return json_response({
        "workers": get_worker_count(),
        "max_workers": MAX_WORKERS_LIMIT,
        "request_timeout": REQUEST_TIMEOUT,
        "detect_timeout": DETECT_TIMEOUT,
    })


@app.route("/api/config", methods=["POST"])
def api_config_update():
    """更新下一批抓取的并发数；网页端输入值会再次经过 1..MAX_WORKERS_LIMIT 限制。"""
    payload = request.get_json(silent=True) or {}
    try:
        workers = set_worker_count(int(payload.get("workers", get_worker_count())))
    except (TypeError, ValueError):
        return json_response({"error": "workers 必须是 1 到 %d 的整数" % MAX_WORKERS_LIMIT}, status=400)
    except RuntimeError as exc:
        return json_response({"error": str(exc), "workers": get_worker_count()}, status=409)
    return json_response({"ok": True, "workers": workers, "max_workers": MAX_WORKERS_LIMIT})


@app.route("/api/all")
def api_all():
    """返回全部数据。若抓取尚未结束，会等待其完成（最多 ALL_WAIT_SECONDS 秒）。"""
    _finished.wait(timeout=ALL_WAIT_SECONDS)
    with _lock:
        payload = {
            "ready": not _progress["running"] and _progress["done"] >= len(CATEGORIES),
            "progress": dict(_progress),
            "summary": summary_counts(),
            "virtual": virtual_templates(),
            "categories": snapshot_all(),
        }
    return json_response(payload)


@app.route("/api/category/<path:name>", methods=["GET"])
@app.route("/api/category", methods=["GET"])
def api_category(name: str = ""):
    """按需返回单个分类；缓存命中直接返回，失败项会重新抓取（供「点击重试」）。"""
    raw = name or request.args.get("name", "")
    if raw in (VALID_SUMMARY, INVALID_SUMMARY):
        # 汇总分类不是官网分类，直接在内存里汇总
        return json_response(build_summary("valid" if raw == VALID_SUMMARY else "invalid"))
    resolved = resolve_category(raw)
    if not resolved:
        return json_response({"error": "未知分类：%s" % raw}, status=404)

    force = request.args.get("force", "") in ("1", "true", "yes")
    if not force:
        cached = lookup_category(resolved)
        if cached is not None and cached.get("status") == "ok":
            return json_response(_augment(dict(cached)))

    return json_response(_augment(reload_category(resolved)))


@app.route("/api/summary")
def api_summary():
    """汇总分类：kind=valid（有效链接） / kind=invalid（无效链接）。

    前端首次点击这两个汇总分类时才调用，结果由前端缓存。
    """
    kind = (request.args.get("kind") or "valid").strip().lower()
    if kind not in ("valid", "invalid"):
        return json_response({"error": "kind 只能是 valid 或 invalid"}, status=400)
    _finished.wait(timeout=ALL_WAIT_SECONDS)
    payload = build_summary(kind)
    payload["total"] = len(payload["versions"])
    payload = _augment(payload)
    return json_response(payload)


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    started = start_fetch(clear=True)
    return json_response({"ok": True, "started": started, "progress": get_progress()})


# 进程启动即开始后台全量抓取（并发的 72 个分类请求），前端轮询进度即可
start_fetch()


if __name__ == "__main__":
    ADDRESS = "http://127.0.0.1:5000"
    print("=" * 66)
    print("  银豹全版本下载链接速查工具（网页版）")
    print("  共 %d 个分类，已在后台并发抓取" % len(CATEGORIES))
    print("  访问地址：%s" % ADDRESS)
    print("=" * 66)
    if "--no-browser" not in sys.argv:
        # 启动后自动弹出浏览器窗口（相当于直接打开 GUI），1.5 秒后再开，等服务先起来
        threading.Timer(1.5, lambda: webbrowser.open(ADDRESS)).start()
        print("  已为你自动打开浏览器窗口（加 --no-browser 参数可禁用）")
    try:
        app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
    except KeyboardInterrupt:
        pass
    finally:
        # 直接退出，避免解释器在 atexit 里 join 非守护的工作线程而卡住
        print("  正在退出…")
        shutdown()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
