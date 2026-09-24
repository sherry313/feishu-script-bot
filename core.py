# -*- coding: utf-8 -*-
"""共用零件：连表格、调提取API、调 Claude 改写"""
import os, re, sys, io, time, json
from datetime import datetime, timezone, timedelta

import requests, gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

# Windows 控制台中文别乱码
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)

load_dotenv()

API_BASE   = "https://api.transcriptgenerate.com/open-gateway/transcript-api/v1"
BEIJING    = timezone(timedelta(hours=8))
COL = {"链接":1,"标题":2,"标签":3,"简介":4,"时长":5,"原文":6,"改写一":7,"改写二":8,"状态":9,"时间":10}

def cfg(name, default=""):
    return os.getenv(name, default).strip()

def now_str():
    return datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M")

# ---------- 抖音链接 ----------
def pick_url(text):
    """从『7.68 复制打开抖音…https://v.douyin.com/xxx/ …』这坨里把网址抠出来"""
    if not text:
        return None
    m = re.search(r"https?://[^\s，,。、\u4e00-\u9fff]+", text)
    return m.group(0).rstrip("/。，,、") if m else None

# ---------- Google 表格 ----------
def open_sheet():
    key_file = cfg("GOOGLE_KEY_FILE")
    url      = cfg("SHEET_URL")
    tab      = cfg("SHEET_NAME", "Sheet1")
    sid = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url).group(1)
    creds = Credentials.from_service_account_file(key_file, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"])
    book = gspread.authorize(creds).open_by_key(sid)
    try:
        return book.worksheet(tab)
    except gspread.WorksheetNotFound:
        return book.get_worksheet(0)

def pending_rows(ws):
    """返回 [(行号, 原始文本, 网址)]：A列有东西、E列(改写一)还空着的行"""
    rows = ws.get_all_values()
    out = []
    for i, r in enumerate(rows[1:], start=2):      # 跳过表头
        raw = (r[0] if len(r) > 0 else "").strip()
        done = (r[6] if len(r) > 6 else "").strip()   # G列 改写一
        if raw and not done:
            out.append((i, raw, pick_url(raw)))
    return out

# ---------- 文案缓存：同一个链接只扣一次额度 ----------
CACHE_FILE = "transcript_cache.json"
_cache_lock = __import__("threading").Lock()

def cache_get(url):
    try:
        with io.open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f).get(url)
    except Exception:
        return None

def cache_put(url, data):
    with _cache_lock:
        try:
            with io.open(CACHE_FILE, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
        d[url] = data
        with io.open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)

# ---------- 提取文案 API ----------
def _req(method, url, **kw):
    """网络抖一下就重试同一个请求，绝不重新提交任务（重提=重扣额度）"""
    last = None
    for i in range(4):
        try:
            return requests.request(method, url, **kw)
        except requests.exceptions.RequestException as e:
            last = e
            time.sleep(2 * (i + 1))
    raise last

def _headers():
    return {"X-API-Key": cfg("TRANSCRIPT_API_KEY"),
            "X-API-Secret": cfg("TRANSCRIPT_API_SECRET")}

def quota():
    r = _req("GET", f"{API_BASE}/quota/usage", headers=_headers(), timeout=30)
    return r.json().get("data", {})

def concurrency():
    r = _req("GET", f"{API_BASE}/quota/concurrency", headers=_headers(), timeout=30)
    return r.json().get("data", {})

def _dig(d, *names):
    """字段名不确定，挨个试；再不行就在嵌套里找"""
    for n in names:
        if isinstance(d, dict) and d.get(n) not in (None, ""):
            return d[n]
    for v in (d.values() if isinstance(d, dict) else []):
        if isinstance(v, dict):
            got = _dig(v, *names)
            if got not in (None, ""):
                return got
    return ""

def fetch_transcript(work_url, timeout_s=600, debug_dump=None):
    """丢一个链接进去，等它转完，返回 {文案, 标题, 时长}"""
    r = _req("POST", f"{API_BASE}/task/create", headers=_headers(),
                      json={"workUrl": work_url}, timeout=60)
    body = r.json()
    if body.get("code") != 200:
        raise RuntimeError(f"提交失败: {body.get('msg') or body}")
    task_id = _dig(body.get("data", {}), "taskId", "taskID", "id")
    if not task_id:
        raise RuntimeError(f"没拿到 taskId: {body}")

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(4)
        q = _req("GET", f"{API_BASE}/task/query", headers=_headers(),
                 params={"taskId": task_id}, timeout=60).json()
        data = q.get("data", {}) or {}
        status = str(_dig(data, "status", "taskStatus")).upper()
        if status == "SUCCESS":
            if debug_dump:
                with open(debug_dump, "w", encoding="utf-8") as f:
                    json.dump(q, f, ensure_ascii=False, indent=2)
            text = _dig(data, "textContent", "text", "content", "transcript")
            if not text:
                raise RuntimeError(f"转成功了但没文字（可能视频没人说话）")
            return {"文案": text,
                    "标题": _dig(data, "title", "workTitle", "videoTitle") or "",
                    "时长": _dig(data, "duration", "durationSeconds", "videoDuration") or ""}
        if status == "FAILURE":
            raise RuntimeError(f"提取失败: {_dig(data,'errorMsg','msg','message') or q.get('msg') or '未知原因'}")
    raise RuntimeError(f"等了 {timeout_s//60} 分钟还没转完，先跳过")

# ---------- Claude 改写 ----------
SYSTEM = """你是资深短视频口播文案改写员。用户给你一段从抖音视频转出来的口播稿，你要把它改写成一条能直接对着镜头念的新文案。

【最重要的一条：时长】
成品必须控制在用户指定的字数区间内。这是硬要求，宁可少讲一个要点，也不许超字数。

【原文很长时怎么处理】
不要把所有要点都塞进去。聚焦一个主题，从原文里挑出最重要、最有价值、观众最痛的 2~3 个要点讲清楚，其余直接舍弃，不要用"还有很多"之类的话带过。
判断哪几个最重要：能直接避免损失的 > 最容易踩坑的 > 最反常识的 > 泛泛的常识。
宁可 3 个点讲得有血有肉，也不要 7 个点每个一句话——那听起来像念清单，没人看得下去。

【原文本来就短时怎么处理】
不许为了凑字数注水、展开、加例子。原文多长就写多长，只做改写，不做扩写。

【其余规矩】
1. 只能用原文里有的信息、观点、例子。绝对不许自己编造原文没有的内容、数据、案例。
2. 句子要重新组织：换说法、换句式、调整先后顺序，读起来像另一个人写的，不是同义词替换。
3. 删掉所有口水话和平台话术：家人们、宝子们、废话不多说、双击点赞、关注我、我是XXX、评论区扣1、上链接、看到最后 等等。
4. 口语化，适合对着镜头念，不要写成书面文章。
5. 结构：开头三句之内要有钩子抓住人，中间把选中的要点讲透，结尾给一句干脆的收束。
6. 必须写完整，不许中途停下。
7. 只输出文案正文，不要解释、不要加标题、不要写"以下是改写"这类话。"""


def _text_of(msg):
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("写到一半被长度上限截断了")
    out = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    if not out:
        raise RuntimeError("Claude 没返回文字")
    return out


CPM      = 320      # 口播语速：每分钟约320字（实测值）
HARD_CAP = 480      # 约1.5分钟上限；实际成片仍需按音频时长核验


def _targets(n):
    """目标约1~1.5分钟；短于一分钟的原文不注水扩写。"""
    if n < 320:
        return int(n * 0.85), n
    return 320, min(n, HARD_CAP)


def _one_version(text, client, model, avoid=None, repair=None):
    n = len(text)
    lo, hi = _targets(n)
    aim_hi = min(hi, 430) if n > 480 else hi
    parts = ["这是原文（%d 字，约 %.1f 分钟）：" % (n, n / CPM), "", text, "",
             "请改写成 %d~%d 字，建议目标%d字。标点、空格和换行均计入字数。" % (lo, aim_hi, (lo+aim_hi)//2),
             "字数是硬要求：绝对不能超过 %d 字，超了整篇作废重写。写完自己数一遍再输出。" % hi]
    if repair:
        parts += ["上次草稿实测%d字，不符合%d~%d字。请修改该草稿，目标%d字，保留完整句子，不增加原文没有的信息：" % (len(repair),lo,hi,(lo+aim_hi)//2), repair]
    if n > HARD_CAP:
        parts.append("原文偏长，请聚焦一个核心主题，挑最重要的2~3个要点讲清楚，其余舍弃。目标1~1.5分钟，不要每个点都提一句。")
    else:
        parts.append("原文本来就不长，保持这个长度改写就行，不许注水拉长。")
    if avoid:
        parts += ["",
                  "注意：下面这一版已经写过了。你这一版必须跟它明显不同——"
                  "开头的切入角度要换、句式节奏要换。如果原文要点多，挑的角度也可以不一样。"
                  "不能只是换几个词。但字数上限一样要守住，别要写得不一样就越写越长。",
                  "", avoid]
    msg = client.messages.create(
        model=model, max_tokens=16000, system=SYSTEM,
        messages=[{"role": "user", "content": "\n".join(parts)}])
    out = _text_of(msg)
    if not lo <= len(out) <= hi:
        error = RuntimeError("写了 %d 字，不符合 %d~%d 字（约1~1.5分钟，短原文除外）要求" % (len(out), lo, hi))
        error.draft = out
        raise error
    return out


def rewrite(text, client, model):
    """分两次调用，各写各的，保证两版都完整"""
    v1 = _one_version(text, client, model)
    v2 = _one_version(text, client, model, avoid=v1)
    return v1, v2



# ---------- 标题 / 标签 / 一句话简介 ----------
META_SYSTEM = """你是短视频运营。用户给你一段视频的口播原文，你要产出三样东西。

1. 标题：一句吸引人的短视频标题，目标16~20字，严格15~25字（标点数字也算字数）。要制造好奇、点痛点、或者用数字。
   不要用"分享一下""聊聊"这种平的开头，不要标题党到跟内容不符。
2. 标签：正好 5 个话题标签，每个都带 # 号，用空格隔开。
   从大到小排：先是行业大词，再是细分人群或场景词。
3. 简介：一句话说清这条视频是讲什么的，目标15~20字，严格25字以内（标点数字也算），给人一眼就懂。

严格按下面的格式输出，三行，不要有别的字：
标题: xxx
标签: #xx #xx #xx #xx #xx
简介: xxx"""


def make_meta(text, client, model):
    """从原文里提炼 标题/标签/简介"""
    msg = client.messages.create(
        model=model, max_tokens=2000, system=META_SYSTEM,
        messages=[{"role": "user", "content": "口播原文：\n\n" + text[:6000]}])
    out = _text_of(msg)
    got = {"标题": "", "标签": "", "简介": ""}
    for line in out.splitlines():
        match = re.match(r'^\s*(?:[-*]\s*)?(?:\*\*)?(标题|标签|简介)(?:\*\*)?\s*[:：]\s*(.*)$', line)
        if match:
            got[match.group(1)] = match.group(2).strip().strip('*').strip()
    if not got["标题"]:
        raise RuntimeError("标题没生成出来")
    return got
