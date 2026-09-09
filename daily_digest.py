#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
供應商官網 — 每日動態日報
每天早上把「昨日」偵測到、且 AI 判定具商業價值的變動，
依 [價格][活動][營業][交通] 分區整合成一封信寄出。

資料來源＝changedetection 每次變動時寫下的摘要檔（change-summary-*.txt），
檔名帶有變動發生的時間，所以不受「後來又變動一次」或「手動按複查」影響，
任何一天都能正確重跑。不需要 API token，也不依賴網頁介面是否正常。

不需要任何額外套件（全部用 Python 內建函式庫）。
帳密沿用 changedetection 既有設定，本檔不存任何密碼。
"""

import argparse, glob, json, os, re, ssl, smtplib, sys, time, traceback, urllib.parse
from datetime import datetime, timedelta, time as dtime, date as dtdate
from email.message import EmailMessage
from email.utils import formataddr
from zoneinfo import ZoneInfo

# ──────────────────────────────────────────────
# 設定
# ──────────────────────────────────────────────
DATA_DIR  = os.path.expanduser("~/changedetection-data")
TAG_TITLE = "廠商官網"
TZ        = ZoneInfo("Asia/Taipei")

# 分區順序＝信裡由上到下的順序。想把「營業異動」擺第一，把它移到最前面即可。
SECTIONS = [
    ("價格", "價格",     "#8f5a10", "#f6ecda", "優惠、折扣、票價調整"),
    ("活動", "活動",     "#3b36a8", "#e6e5f7", "檔期、新品上架、品牌聯名"),
    ("營業", "營業異動", "#a81f39", "#f8e2e6", "會打到已排定的行程，優先確認"),
    ("交通", "交通",     "#0a6379", "#dcedf3", "動線、班次、管制"),
    ("其他", "未分類",   "#5a6570", "#e9ebee", "AI 沒有標上類別，建議人工看一眼"),
]
# 「最終清點時間」。當日這個時刻之後才偵測到的變動，算進隔天的日報。
# (0, 0) 代表以午夜為界＝完整的日曆日。想改成晚上 8 點截止就寫 (20, 0)。
CUTOFF_HOUR, CUTOFF_MIN = 0, 0

# 常駐模式：每天過了這個時間就寄出前一天的日報。
# 機器那時候在睡也沒關係——常駐的程式會在下一次醒來時補寄（跟 changedetection 一樣）。
RUN_AFTER_HOUR, RUN_AFTER_MIN = 8, 40
POLL_SECONDS = 300          # 每 5 分鐘檢查一次
MAX_BACKFILL_DAYS = 7       # 機器關機多天後，最多補寄幾天
STATE_FILE = os.path.join(DATA_DIR, "logs", "dailydigest.state")

WEEKDAY_TW = ["一", "二", "三", "四", "五", "六", "日"]
# 注意：changedetection 的摘要檔名 change-summary-<A>-to-<B>-<hash>.txt 裡，
# A 與 B 都是「變動前那一版快照」的時間，不是變動發生的時間。
# 真正的變動時間＝該網站歷史裡「下一筆」快照的時間。實測 5 個樣本皆吻合。
SUMMARY_RE = re.compile(r"change-summary-\d+-to-(\d+)-[0-9a-f]+\.txt$")


def history_timestamps(watch_dir):
    path = os.path.join(watch_dir, "history.txt")
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if "," in line:
                    try:
                        out.append(int(line.split(",")[0]))
                    except ValueError:
                        pass
    except OSError:
        pass
    return sorted(out)


def change_time(watch_dir, summary_path, prev_ts, hist):
    """把「前一版時間」換算成真正的變動時間。"""
    for t in hist:
        if t > prev_ts:
            return t
    try:                                    # 歷史被裁切時的退路
        return int(os.path.getmtime(summary_path))
    except OSError:
        return prev_ts


# ──────────────────────────────────────────────
# 讀取既有設定
# ──────────────────────────────────────────────
def find_tag():
    for name in os.listdir(DATA_DIR):
        path = os.path.join(DATA_DIR, name, "tag.json")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            tag = json.load(fh)
        if tag.get("title") == TAG_TITLE:
            return name, tag
    raise RuntimeError(f"找不到群組「{TAG_TITLE}」")


def load_smtp(tag):
    for raw in (tag.get("notification_urls") or []):
        if not raw.startswith("mailtos://"):
            continue
        p = urllib.parse.urlparse(raw)
        qs = urllib.parse.parse_qs(p.query)
        user = urllib.parse.unquote(p.username or "")
        return {
            "user": user,
            "password": urllib.parse.unquote(p.password or ""),
            "host": "smtp.gmail.com",
            "sender": f"{user}@{p.hostname}",
            "display": (qs.get("name") or ["supplier monitor"])[0],
            "to": [a.strip() for a in (qs.get("to") or [""])[0].split(",") if a.strip()],
        }
    raise RuntimeError(f"群組「{TAG_TITLE}」裡找不到 mailtos:// 通知設定")


# ──────────────────────────────────────────────
# 解析摘要
# ──────────────────────────────────────────────
BULLET = re.compile(r"^\s*(?:[-*•]\s*)?\[(價格|活動|營業|交通)\]\s*(.+?)\s*$")

def parse_summary(text):
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line in ("無", "Added:", "Removed:", "Changed:"):
            continue
        m = BULLET.match(line)
        if m:
            out.append((m.group(1), m.group(2)))
        else:
            cleaned = re.sub(r"^[-*•]\s*", "", line)
            if len(cleaned) > 4:
                out.append(("其他", cleaned))
    return out


def report_window(day):
    """回傳這份日報涵蓋的時間區間 (start, end)，單位為 unix timestamp。

    清點時間設為午夜時，區間就是完整的 day 當天。
    設成例如 20:00 時，「day 的日報」涵蓋 前一天20:00 → 當天20:00，
    所以當天 20:00 之後你再怎麼複查，都只會進到隔天的日報。
    """
    if (CUTOFF_HOUR, CUTOFF_MIN) == (0, 0):
        end = datetime.combine(day + timedelta(days=1), dtime.min, TZ)
    else:
        end = datetime.combine(day, dtime(CUTOFF_HOUR, CUTOFF_MIN), TZ)
    return (end - timedelta(days=1)).timestamp(), end.timestamp()


def collect(day, tag_uuid):
    """掃過每個 watch 目錄，撿出「變動時間落在清點區間內」的摘要檔。"""
    start, end = report_window(day)
    buckets, n_items, suppliers = {}, 0, set()

    for uuid in os.listdir(DATA_DIR):
        wpath = os.path.join(DATA_DIR, uuid, "watch.json")
        if not os.path.exists(wpath):
            continue
        try:
            with open(wpath, encoding="utf-8") as fh:
                w = json.load(fh)
        except Exception:
            continue
        if tag_uuid not in (w.get("tags") or []):
            continue

        title = w.get("title") or w.get("page_title") or w.get("url")
        url = w.get("url")
        hist = history_timestamps(os.path.join(DATA_DIR, uuid))

        for f in sorted(glob.glob(os.path.join(DATA_DIR, uuid, "change-summary-*.txt"))):
            m = SUMMARY_RE.search(os.path.basename(f))
            if not m:
                continue
            ts = change_time(os.path.join(DATA_DIR, uuid), f, int(m.group(1)), hist)
            if not (start <= ts < end):
                continue
            try:
                with open(f, encoding="utf-8") as fh:
                    items = parse_summary(fh.read())
            except Exception:
                continue
            for cat, text in items:
                slot = buckets.setdefault(cat, {}).setdefault(
                    uuid, {"title": title, "url": url, "items": []})
                if text in slot["items"]:          # 同一天重複偵測到同一則就不重複列
                    continue
                slot["items"].append(text)
                n_items += 1
                suppliers.add(uuid)

    return buckets, n_items, suppliers


def health(tag_uuid):
    """回報目前有多少網站抓取失敗——這是系統健康度，跟當日內容無關。"""
    total = errored = 0
    for uuid in os.listdir(DATA_DIR):
        wpath = os.path.join(DATA_DIR, uuid, "watch.json")
        if not os.path.exists(wpath):
            continue
        try:
            with open(wpath, encoding="utf-8") as fh:
                w = json.load(fh)
        except Exception:
            continue
        if tag_uuid not in (w.get("tags") or []):
            continue
        total += 1
        if w.get("last_error"):
            errored += 1
    return total, errored


# ──────────────────────────────────────────────
# 組信
# ──────────────────────────────────────────────
def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))

FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang TC',"
        "'Microsoft JhengHei','Noto Sans TC',sans-serif")

def build_html(buckets, day, n_items, n_suppliers, warn=None, window=None):
    label = f"{day.month}月{day.day}日（{WEEKDAY_TW[day.weekday()]}）"
    h = []
    h.append(f'<div style="margin:0;padding:24px 12px;background:#f4f5f7;font-family:{FONT};">')
    h.append('<div style="max-width:680px;margin:0 auto;background:#ffffff;'
             'border:1px solid #dee2e7;border-radius:10px;padding:28px 26px;">')
    h.append('<div style="font-size:11px;letter-spacing:.14em;text-transform:uppercase;'
             'color:#7d8892;margin-bottom:10px;">供應商官網監測 · 每日情報</div>')
    h.append(f'<div style="font-size:26px;font-weight:700;color:#14181d;'
             f'line-height:1.25;margin-bottom:8px;">{esc(label)} 動態日報</div>')

    sub = (f"{n_suppliers} 家供應商有變動，共 {n_items} 則" if n_items
           else "昨日無具商業價值的變動")
    h.append(f'<div style="font-size:13px;color:#4a5560;padding-bottom:18px;'
             f'border-bottom:1px solid #dee2e7;">{esc(sub)}</div>')

    if warn:
        h.append('<div style="margin-top:16px;background:#fdf3e3;border:1px solid #e8c98a;'
                 'border-radius:8px;padding:11px 14px;font-size:13px;color:#7a5510;">'
                 f'&#9888; {esc(warn)}</div>')

    if not n_items:
        h.append('<div style="padding:34px 0;text-align:center;color:#7d8892;font-size:14px;">'
                 '系統昨日運作正常，沒有偵測到需要處理的變動。</div>')
    else:
        for key, title, colour, tint, hint in SECTIONS:
            group = buckets.get(key)
            if not group:
                continue
            count = sum(len(v["items"]) for v in group.values())
            h.append('<div style="margin-top:26px;">')
            h.append(f'<div style="border-bottom:2px solid {colour};padding-bottom:8px;margin-bottom:4px;">'
                     f'<span style="font-size:16px;font-weight:700;color:{colour};">{esc(title)}</span>'
                     f'<span style="font-size:12px;font-weight:600;color:{colour};background:{tint};'
                     f'border-radius:5px;padding:1px 7px;margin-left:9px;">{count}</span>'
                     f'<span style="font-size:12px;color:#7d8892;margin-left:10px;">{esc(hint)}</span></div>')
            for sup in sorted(group.values(), key=lambda x: -len(x["items"])):
                h.append('<div style="padding:12px 0;border-bottom:1px solid #eceef1;">')
                h.append(f'<div style="font-size:14px;font-weight:700;color:#14181d;margin-bottom:5px;">'
                         f'{esc(sup["title"])} <a href="{esc(sup["url"])}" '
                         f'style="font-size:11px;font-weight:400;color:#7d8892;text-decoration:none;">'
                         f'· 官網 &#8599;</a></div>')
                for it in sup["items"]:
                    h.append(f'<div style="font-size:14px;color:#14181d;line-height:1.6;'
                             f'padding-left:14px;margin:4px 0;">'
                             f'<span style="color:{colour};">&bull;</span> {esc(it)}</div>')
                h.append('</div>')
            h.append('</div>')

    h.append('<div style="margin-top:26px;padding-top:14px;border-top:1px solid #dee2e7;'
             'font-size:11px;color:#7d8892;line-height:1.7;">'
             '本信由供應商官網監測系統自動彙整，內容經 AI 判讀為具商業價值之變動。<br>'
             f'清點區間：{esc(window or "")}　每日固定寄送一封；若某日未收到，代表系統可能異常。</div>')
    h.append('</div></div>')
    return "".join(h)


# ──────────────────────────────────────────────
# 寄信（退避重試，早上剛喚醒時網路常常還沒穩）
# ──────────────────────────────────────────────
SMTP_ATTEMPTS = [
    (465, "ssl", 0), (587, "starttls", 15), (465, "ssl", 30),
    (587, "starttls", 60), (465, "ssl", 120), (587, "starttls", 240),
]

def _deliver(msg, smtp, port, mode):
    ctx = ssl.create_default_context()
    if mode == "ssl":
        with smtplib.SMTP_SSL(smtp["host"], port, context=ctx, timeout=45) as s:
            s.login(smtp["user"], smtp["password"]); s.send_message(msg)
    else:
        with smtplib.SMTP(smtp["host"], port, timeout=45) as s:
            s.ehlo(); s.starttls(context=ctx); s.ehlo()
            s.login(smtp["user"], smtp["password"]); s.send_message(msg)


def send_mail(smtp, subject, html, text_fallback, attempts=None):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((smtp["display"], smtp["sender"]))
    msg["To"] = ", ".join(smtp["to"])
    msg.set_content(text_fallback)
    msg.add_alternative(html, subtype="html")
    last = None
    for i, (port, mode, wait) in enumerate(attempts or SMTP_ATTEMPTS, start=1):
        if wait:
            time.sleep(wait)
        try:
            _deliver(msg, smtp, port, mode)
            if i > 1:
                sys.stderr.write(f"[重試成功] 第 {i} 次嘗試寄出成功（port {port}/{mode}）\n")
            return
        except Exception as e:
            last = e
            sys.stderr.write(f"[重試] 第 {i} 次寄信失敗（port {port}/{mode}）：{e}\n")
    raise last


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────
def read_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return dtdate.fromisoformat(fh.read().strip())
    except Exception:
        return None


def write_state(day):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        fh.write(day.isoformat())


def due_days(now):
    """算出「還沒寄、而且已經可以寄」的報告日期。"""
    today = now.date()
    latest = today - timedelta(days=1)
    if (now.hour, now.minute) < (RUN_AFTER_HOUR, RUN_AFTER_MIN):
        latest = today - timedelta(days=2)      # 今天還沒到時間，最新只能寄到前天

    last = read_state()
    if last is None:
        return [latest]                          # 第一次跑，只寄最近一份

    days, d = [], last + timedelta(days=1)
    while d <= latest:
        days.append(d)
        d += timedelta(days=1)
    return days[-MAX_BACKFILL_DAYS:]             # 停機太久只補最近幾天


def build_report(day, tag_uuid):
    buckets, n_items, suppliers = collect(day, tag_uuid)
    total, errored = health(tag_uuid)
    ws, we = report_window(day)
    window_txt = (datetime.fromtimestamp(ws, TZ).strftime("%m/%d %H:%M") + " – "
                  + datetime.fromtimestamp(we, TZ).strftime("%m/%d %H:%M"))

    # 健康度警示不再放進日報（避免驚動收件人）；抓取失敗改由後台 changedetection 自行巡視。
    # total/errored 仍保留，供 log 與 --dry-run 檢視。
    warn = None

    label = f"{day.month}/{day.day}（{WEEKDAY_TW[day.weekday()]}）"
    subject = (f"【廠商動態】{label} {len(suppliers)} 家・{n_items} 則"
               if n_items else f"【廠商動態】{label} 無變動")

    lines = [f"{label} 動態日報", f"清點區間：{window_txt}", ""]
    if warn:
        lines += [warn, ""]
    for key, t, _c, _b, _h in SECTIONS:
        for sup in (buckets.get(key) or {}).values():
            for it in sup["items"]:
                lines.append(f"[{t}] {sup['title']}：{it}  {sup['url']}")
    if n_items == 0:
        lines.append("當日無具商業價值的變動。")

    html = build_html(buckets, day, n_items, len(suppliers), warn, window_txt)
    return {"subject": subject, "html": html, "text": "\n".join(lines),
            "label": label, "window": window_txt, "n_items": n_items,
            "n_sup": len(suppliers), "total": total, "errored": errored}


def run_daemon(tag_uuid, smtp):
    sys.stderr.write(f"[常駐] 啟動，每 {POLL_SECONDS} 秒檢查一次，"
                     f"每天 {RUN_AFTER_HOUR:02d}:{RUN_AFTER_MIN:02d} 後寄出前一日日報\n")
    sys.stderr.flush()
    while True:
        try:
            for day in due_days(datetime.now(TZ)):
                r = build_report(day, tag_uuid)
                send_mail(smtp, r["subject"], r["html"], r["text"])
                write_state(day)
                print(f"OK  {r['label']}  {r['n_sup']} 家 / {r['n_items']} 則"
                      f"（{r['errored']}/{r['total']} 抓取失敗）"
                      f" → 已寄給 {', '.join(smtp['to'])}", flush=True)
        except Exception:
            sys.stderr.write(traceback.format_exc())
            sys.stderr.flush()
            # 不寫 state，下一輪會重試
        time.sleep(POLL_SECONDS)


def parse_args():
    ap = argparse.ArgumentParser(description="供應商官網每日動態日報")
    ap.add_argument("--daemon", action="store_true",
                    help="常駐模式：持續執行，時間到就寄，機器睡醒後會自動補寄")
    ap.add_argument("--dry-run", action="store_true", help="不寄信，寫成 preview.html")
    ap.add_argument("--to", metavar="EMAIL", help="改寄給指定信箱（測試用，逗號分隔）")
    ap.add_argument("--date", metavar="YYYY-MM-DD", help="改抓指定日期（預設為昨天）")
    return ap.parse_args()


def main():
    args = parse_args()
    tag_uuid, tag = find_tag()
    smtp = load_smtp(tag)
    if args.to:
        smtp["to"] = [a.strip() for a in args.to.split(",") if a.strip()]

    if args.daemon:
        run_daemon(tag_uuid, smtp)
        return

    try:
        day = (dtdate.fromisoformat(args.date) if args.date
               else (datetime.now(TZ) - timedelta(days=1)).date())
        r = build_report(day, tag_uuid)

        if args.dry_run:
            out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preview.html")
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(r["html"])
            print(f"[試跑] {r['label']}  {r['n_sup']} 家 / {r['n_items']} 則")
            print(f"[試跑] 清點區間：{r['window']}")
            print(f"[試跑] 系統健康度：{r['total']} 個網站，{r['errored']} 個抓取失敗")
            print(f"[試跑] 沒有寄信。內容已寫到：{out}")
            print(f"[試跑] 收件人原本會是：{', '.join(smtp['to'])}")
            return

        send_mail(smtp, r["subject"], r["html"], r["text"])
        print(f"OK  {r['label']}  {r['n_sup']} 家 / {r['n_items']} 則"
              f"（{r['errored']}/{r['total']} 抓取失敗）→ 已寄給 {', '.join(smtp['to'])}")

    except Exception:
        tb = traceback.format_exc()
        sys.stderr.write(tb)
        if args.dry_run:
            sys.exit(1)
        try:
            send_mail(smtp, "【廠商動態】⚠ 日報產生失敗",
                      f'<pre style="font-size:12px;white-space:pre-wrap;">{esc(tb)}</pre>',
                      tb, attempts=[(465, "ssl", 0), (587, "starttls", 20)])
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
