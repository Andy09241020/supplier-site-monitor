#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
供應商官網 — 每日動態日報
每天早上把「昨日」偵測到、且 AI 判定具商業價值的變動，
依 [價格][活動][營業][交通] 分區整合成一封信寄出。

不需要任何額外套件（全部用 Python 內建函式庫）。
帳密與 API token 直接沿用 changedetection 既有設定，本檔不存任何密碼。
"""

import argparse, json, os, re, ssl, smtplib, sys, traceback, urllib.request, urllib.parse
from datetime import datetime, timedelta, time as dtime, date as dtdate
from email.message import EmailMessage
from email.utils import formataddr
from zoneinfo import ZoneInfo

# ──────────────────────────────────────────────
# 設定（要調整的東西都在這一段）
# ──────────────────────────────────────────────
DATA_DIR   = os.path.expanduser("~/changedetection-data")
BASE_URL   = "http://127.0.0.1:5000"
TAG_TITLE  = "廠商官網"          # 只收這個群組底下的 watch
TZ         = ZoneInfo("Asia/Taipei")

# 分區順序＝信裡由上到下的順序。想把「營業異動」擺第一，把它移到最前面即可。
SECTIONS = [
    ("價格", "價格",     "#8f5a10", "#f6ecda", "優惠、折扣、票價調整"),
    ("活動", "活動",     "#3b36a8", "#e6e5f7", "檔期、新品上架、品牌聯名"),
    ("營業", "營業異動", "#a81f39", "#f8e2e6", "會打到已排定的行程，優先確認"),
    ("交通", "交通",     "#0a6379", "#dcedf3", "動線、班次、管制"),
    ("其他", "未分類",   "#5a6570", "#e9ebee", "AI 沒有標上類別，建議人工看一眼"),
]
WEEKDAY_TW = ["一", "二", "三", "四", "五", "六", "日"]


# ──────────────────────────────────────────────
# 讀取既有設定
# ──────────────────────────────────────────────
def load_settings():
    with open(os.path.join(DATA_DIR, "changedetection.json"), encoding="utf-8") as fh:
        app = json.load(fh)["settings"]["application"]
    token = app.get("api_access_token")
    if not token:
        raise RuntimeError("changedetection.json 裡找不到 api_access_token")
    return token


def load_smtp():
    """從群組設定裡取出 Apprise 的 mailtos:// 網址，拆成 SMTP 參數。"""
    for name in os.listdir(DATA_DIR):
        path = os.path.join(DATA_DIR, name, "tag.json")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            tag = json.load(fh)
        if tag.get("title") != TAG_TITLE:
            continue
        for raw in (tag.get("notification_urls") or []):
            if not raw.startswith("mailtos://"):
                continue
            p = urllib.parse.urlparse(raw)
            qs = urllib.parse.parse_qs(p.query)
            return {
                "user": urllib.parse.unquote(p.username or ""),
                "password": urllib.parse.unquote(p.password or ""),
                "host": "smtp.gmail.com",
                "port": 465,
                "sender": f"{urllib.parse.unquote(p.username or '')}@{p.hostname}",
                "display": (qs.get("name") or ["supplier monitor"])[0],
                "to": [a.strip() for a in (qs.get("to") or [""])[0].split(",") if a.strip()],
            }
    raise RuntimeError(f"找不到群組「{TAG_TITLE}」的 mailtos:// 通知設定")


def api(path, token):
    req = urllib.request.Request(BASE_URL + path, headers={"x-api-key": token})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ──────────────────────────────────────────────
# 解析 AI 摘要
# ──────────────────────────────────────────────
BULLET = re.compile(r"^\s*(?:[-*•]\s*)?\[(價格|活動|營業|交通)\]\s*(.+?)\s*$")

def parse_summary(text):
    """把 '[類別] 描述' 的清單拆成 [(類別, 描述), ...]；沒標類別的丟進「其他」。"""
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


# ──────────────────────────────────────────────
# 組信
# ──────────────────────────────────────────────
def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))

FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang TC',"
        "'Microsoft JhengHei','Noto Sans TC',sans-serif")

def build_html(buckets, day, n_items, n_suppliers, warn=None):
    label = f"{day.month}月{day.day}日（{WEEKDAY_TW[day.weekday()]}）"
    h = []
    h.append(f'<div style="margin:0;padding:24px 12px;background:#f4f5f7;font-family:{FONT};">')
    h.append('<div style="max-width:680px;margin:0 auto;background:#ffffff;'
             'border:1px solid #dee2e7;border-radius:10px;padding:28px 26px;">')

    h.append('<div style="font-size:11px;letter-spacing:.14em;text-transform:uppercase;'
             'color:#7d8892;margin-bottom:10px;">供應商官網監測 · 每日情報</div>')
    h.append(f'<div style="font-size:26px;font-weight:700;color:#14181d;'
             f'line-height:1.25;margin-bottom:8px;">{esc(label)} 動態日報</div>')

    if n_items:
        sub = f"{n_suppliers} 家供應商有變動，共 {n_items} 則"
    else:
        sub = "昨日無具商業價值的變動"
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
                     f'<span style="font-size:12px;color:#7d8892;margin-left:10px;">{esc(hint)}</span>'
                     f'</div>')
            for sup in sorted(group.values(), key=lambda x: -len(x["items"])):
                h.append('<div style="padding:12px 0;border-bottom:1px solid #eceef1;">')
                h.append(f'<div style="font-size:14px;font-weight:700;color:#14181d;'
                         f'margin-bottom:5px;">{esc(sup["title"])} '
                         f'<a href="{esc(sup["url"])}" style="font-size:11px;font-weight:400;'
                         f'color:#7d8892;text-decoration:none;">· 官網 &#8599;</a></div>')
                for it in sup["items"]:
                    h.append(f'<div style="font-size:14px;color:#14181d;line-height:1.6;'
                             f'padding-left:14px;position:relative;margin:4px 0;">'
                             f'<span style="color:{colour};">&bull;</span> {esc(it)}</div>')
                h.append('</div>')
            h.append('</div>')

    h.append('<div style="margin-top:26px;padding-top:14px;border-top:1px solid #dee2e7;'
             'font-size:11px;color:#7d8892;line-height:1.7;">'
             '本信由供應商官網監測系統自動彙整，內容經 AI 判讀為具商業價值之變動。<br>'
             '每日固定寄送一封；若某日未收到，代表系統可能異常，請檢查。</div>')
    h.append('</div></div>')
    return "".join(h)


def send_mail(smtp, subject, html, text_fallback):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((smtp["display"], smtp["sender"]))
    msg["To"] = ", ".join(smtp["to"])
    msg.set_content(text_fallback)
    msg.add_alternative(html, subtype="html")
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(smtp["host"], smtp["port"], context=ctx, timeout=60) as s:
        s.login(smtp["user"], smtp["password"])
        s.send_message(msg)


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────
def parse_args():
    ap = argparse.ArgumentParser(description="供應商官網每日動態日報")
    ap.add_argument("--dry-run", action="store_true",
                    help="不寄信，把信件內容寫成 preview.html 讓你先看")
    ap.add_argument("--to", metavar="EMAIL",
                    help="改寄給指定信箱（測試用，可用逗號分隔多個）")
    ap.add_argument("--date", metavar="YYYY-MM-DD",
                    help="改抓指定日期的變動（預設為昨天）")
    return ap.parse_args()


def main():
    args = parse_args()
    smtp = load_smtp()
    if args.to:
        smtp["to"] = [a.strip() for a in args.to.split(",") if a.strip()]
    try:
        token = load_settings()

        now = datetime.now(TZ)
        if args.date:
            day = dtdate.fromisoformat(args.date)
        else:
            day = (now - timedelta(days=1)).date()
        start = datetime.combine(day, dtime.min, TZ).timestamp()
        end = datetime.combine(day, dtime.max, TZ).timestamp()

        watches = api("/api/v1/watch?tag=" + urllib.parse.quote(TAG_TITLE), token)
        changed = [u for u, w in watches.items()
                   if w.get("last_changed") and start <= float(w["last_changed"]) <= end]

        # 防呆：日報必須在「當天的抓取開始之前」跑完。
        # 若當天已經有網站被重新檢查過，昨日的摘要可能已被覆蓋 → 出聲警告，不要無聲漏掉。
        today_start = datetime.combine(now.date(), dtime.min, TZ).timestamp()
        rechecked = sum(1 for w in watches.values()
                        if w.get("last_checked") and float(w["last_checked"]) >= today_start)
        warn = None
        if rechecked and day == (now - timedelta(days=1)).date():
            warn = (f"本次執行時，今日的抓取已經開始（{rechecked} 個網站已重新檢查），"
                    "部分昨日變動的摘要可能已被覆蓋而未列入。建議把日報排程調早。")

        buckets, n_items, suppliers = {}, 0, set()
        for uuid in changed:
            d = api("/api/v1/watch/" + uuid, token)
            if not (d.get("_llm_result") or {}).get("important"):
                continue                                   # AI 判定沒有商業價值，跳過
            items = parse_summary(d.get("_llm_change_summary"))
            if not items:
                continue
            title = d.get("title") or d.get("page_title") or d.get("url")
            url = d.get("url")
            for cat, text in items:
                slot = buckets.setdefault(cat, {}).setdefault(
                    uuid, {"title": title, "url": url, "items": []})
                slot["items"].append(text)
                n_items += 1
                suppliers.add(uuid)

        label = f"{day.month}/{day.day}（{WEEKDAY_TW[day.weekday()]}）"
        subject = (f"【廠商動態】{label} {len(suppliers)} 家・{n_items} 則"
                   if n_items else f"【廠商動態】{label} 無變動")

        lines = [f"{label} 動態日報", ""]
        for key, t, _c, _b, _h in SECTIONS:
            for sup in (buckets.get(key) or {}).values():
                for it in sup["items"]:
                    lines.append(f"[{t}] {sup['title']}：{it}  {sup['url']}")
        if n_items == 0:
            lines.append("昨日無具商業價值的變動。")

        html = build_html(buckets, day, n_items, len(suppliers), warn)
        if args.dry_run:
            out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preview.html")
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(html)
            print(f"[試跑] {label}  {len(suppliers)} 家 / {n_items} 則")
            print(f"[試跑] 沒有寄信。信件內容已寫到：{out}")
            print(f"[試跑] 收件人原本會是：{', '.join(smtp['to'])}")
            if warn:
                print(f"[試跑] ⚠ {warn}")
            return
        send_mail(smtp, subject, html, "\n".join(lines))
        print(f"OK  {label}  {len(suppliers)} 家 / {n_items} 則 → 已寄給 {', '.join(smtp['to'])}")

    except Exception:
        tb = traceback.format_exc()
        sys.stderr.write(tb)
        if args.dry_run:
            sys.exit(1)
        try:
            send_mail(smtp, "【廠商動態】⚠ 日報產生失敗",
                      f'<pre style="font-size:12px;white-space:pre-wrap;">{esc(tb)}</pre>',
                      tb)
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
