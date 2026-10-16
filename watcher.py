"""
Study Watcher - theo doi thay doi tren trang web chuong trinh du hoc.

Moi lan chay:
  1. Doc danh sach trang trong watchlist.yaml
  2. Tai tung trang, lay phan chu (text), chuan hoa
  3. So voi ban luu lan truoc (thu muc snapshots/)
     Mac dinh chi giu cac dong co tu khoa quan trong (deadline, hoc phi,
     yeu cau dau vao...) + 1 dong ngay sau, de tranh bao dong gia
     tu lich su kien, tin tuc, loi chung thuc...
  4. Neu khac -> gui Telegram kem doan thay doi
     Neu khong tai duoc -> gui canh bao loi
  5. Luu ban moi vao snapshots/

Chay thu tren may (khong gui Telegram):
  python watcher.py
Bien moi truong can de gui Telegram:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import difflib
import hashlib
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
import yaml
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
WATCHLIST_FILE = Path(os.environ.get("WATCHLIST_FILE", BASE_DIR / "watchlist.yaml"))
SNAPSHOT_DIR = Path(os.environ.get("SNAPSHOT_DIR", BASE_DIR / "snapshots"))

USER_AGENT = "StudyWatcher/1.0 (personal study-abroad page monitor; checks once a day)"
TIMEOUT_SECONDS = 30
DELAY_BETWEEN_PAGES = 3          # giay, de khong lam phien server
MAX_DIFF_LINES = 20              # so dong thay doi toi da trong 1 tin nhan
TELEGRAM_LIMIT = 4000            # Telegram gioi han ~4096 ky tu / tin

VN_TZ = timezone(timedelta(hours=7))

# Tu khoa mac dinh, dung khi watchlist.yaml khong khai bao "keywords".
# So khop ca tu (khong phan biet hoa thuong), chap nhan them duoi -s / -es:
# "fee" khop "fees" nhung khong khop "feedback"; "eur" khong khop "Europe".
DEFAULT_KEYWORDS = [
    "deadline", "application", "apply", "admission", "requirement",
    "tuition", "fee", "€", "eur", "euro", "semester", "scholarship",
    "ielts", "toefl", "gmat", "gre", "gpa", "ects", "credit", "proficiency",
]
CONTEXT_LINES_AFTER = 1          # giu them N dong ngay sau dong co tu khoa

# Danh dau dong dau file snapshot. Khi cach loc thay doi, snapshot cu duoc
# lay lam moc moi thay vi bi bao "thay doi".
SNAPSHOT_HEADER_PREFIX = "# study-watcher filter: "

# Cac the HTML thuong chua noi dung "nhieu" (menu, footer, script...)
NOISE_TAGS = ["script", "style", "noscript", "nav", "footer", "header",
              "svg", "form", "iframe", "button"]


# ---------------------------------------------------------------- Telegram
def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("---- [CHẠY THỬ] Tin nhắn Telegram ----")
        print(text)
        print("-------------------------------------")
        return
    if len(text) > TELEGRAM_LIMIT:
        text = text[:TELEGRAM_LIMIT] + "\n...(đã cắt bớt)"
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"},
        timeout=TIMEOUT_SECONDS,
    )
    if resp.status_code != 200:
        print(f"Lỗi gửi Telegram: {resp.status_code} {resp.text}", file=sys.stderr)


# ---------------------------------------------------------------- Tai trang
_robots_cache: dict = {}


def allowed_by_robots(url: str) -> bool:
    """Ton trong robots.txt cua trang. Neu khong doc duoc robots.txt thi cho phep."""
    parts = urlparse(url)
    root = f"{parts.scheme}://{parts.netloc}"
    if root not in _robots_cache:
        rp = RobotFileParser()
        try:
            resp = requests.get(root + "/robots.txt", timeout=TIMEOUT_SECONDS,
                                headers={"User-Agent": USER_AGENT})
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                rp = None
        except requests.RequestException:
            rp = None
        _robots_cache[root] = rp
    rp = _robots_cache[root]
    return True if rp is None else rp.can_fetch(USER_AGENT, url)


def fetch_html(url: str) -> str:
    resp = requests.get(url, timeout=TIMEOUT_SECONDS,
                        headers={"User-Agent": USER_AGENT,
                                 "Accept-Language": "en"})
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or resp.encoding
    return resp.text


def extract_text(html: str, selector: str | None) -> str:
    """Lay phan chu cua trang, bo menu/footer/script, chuan hoa khoang trang."""
    soup = BeautifulSoup(html, "html.parser")
    if selector:
        nodes = soup.select(selector)
        if not nodes:
            raise ValueError(f"Không tìm thấy vùng '{selector}' trên trang")
        root = BeautifulSoup("".join(str(n) for n in nodes), "html.parser")
    else:
        root = soup.body or soup
    for tag in root.find_all(NOISE_TAGS):
        tag.decompose()
    lines = []
    for line in root.get_text("\n").splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def build_keyword_regex(keywords: list[str]) -> re.Pattern:
    parts = []
    for kw in keywords:
        kw = str(kw).strip().lower()
        if not kw:
            continue
        if re.fullmatch(r"\w[\w\s-]*", kw):
            parts.append(rf"(?<!\w){re.escape(kw)}(?:s|es)?(?!\w)")
        else:  # ky hieu nhu "€": khop o bat ky dau
            parts.append(re.escape(kw))
    return re.compile("|".join(parts), re.IGNORECASE)


def filter_lines(text: str, pattern: re.Pattern) -> str:
    """Chi giu dong co tu khoa + CONTEXT_LINES_AFTER dong ngay sau no."""
    lines = text.splitlines()
    keep = set()
    for i, line in enumerate(lines):
        if pattern.search(line):
            keep.update(range(i, min(i + 1 + CONTEXT_LINES_AFTER, len(lines))))
    return "\n".join(lines[i] for i in sorted(keep))


# ---------------------------------------------------------------- So sanh
def short_diff(old: str, new: str) -> str:
    """Tra ve cac dong bi xoa (-) va them (+), toi da MAX_DIFF_LINES dong."""
    changes = []
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(),
                                     lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        changes.append(line)
    extra = len(changes) - MAX_DIFF_LINES
    changes = changes[:MAX_DIFF_LINES]
    if extra > 0:
        changes.append(f"... và {extra} dòng thay đổi khác")
    return "\n".join(changes)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- Main
def check_page(item: dict, keywords: list[str]) -> dict:
    """Kiem tra 1 trang.
    status = new | rebaseline | same | changed | error | blocked."""
    page_id = item["id"]
    url = item["url"]
    snap_file = SNAPSHOT_DIR / f"{page_id}.txt"
    use_filter = str(item.get("filter", "keywords")).lower() != "none"
    label = ("keywords " + sha("|".join(sorted(k.lower() for k in keywords)))[:8]
             if use_filter else "none")
    label += f" | selector={item.get('selector')}"
    header = SNAPSHOT_HEADER_PREFIX + label

    if not allowed_by_robots(url):
        return {"status": "blocked", "item": item}

    try:
        text = extract_text(fetch_html(url), item.get("selector"))
    except Exception as exc:  # noqa: BLE001 - muon bat moi loi de bao ve Telegram
        return {"status": "error", "item": item, "error": str(exc)[:300]}

    if use_filter:
        text = filter_lines(text, build_keyword_regex(keywords))

    if not text:
        msg = ("Không có dòng nào chứa từ khóa. Kiểm tra lại URL, "
               "hoặc đặt filter: none cho trang này") if use_filter \
            else "Trang không có chữ nào"
        return {"status": "error", "item": item, "error": msg}

    content = header + "\n" + text
    if not snap_file.exists():
        snap_file.write_text(content, encoding="utf-8")
        return {"status": "new", "item": item}

    old_lines = snap_file.read_text(encoding="utf-8").splitlines()
    old_header = old_lines[0] if old_lines and old_lines[0].startswith(
        SNAPSHOT_HEADER_PREFIX) else None
    if old_header != header:
        # Cach loc da doi (hoac snapshot tu phien ban cu): lay lam moc moi
        snap_file.write_text(content, encoding="utf-8")
        return {"status": "rebaseline", "item": item}

    old = "\n".join(old_lines[1:])
    if sha(old) == sha(text):
        return {"status": "same", "item": item}

    snap_file.write_text(content, encoding="utf-8")
    return {"status": "changed", "item": item, "diff": short_diff(old, text)}


def main() -> int:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_load(WATCHLIST_FILE.read_text(encoding="utf-8")) or []
    # Ho tro ca dang cu (danh sach trang) va dang moi (keywords + pages)
    if isinstance(data, dict):
        keywords = data.get("keywords") or DEFAULT_KEYWORDS
        watchlist = data.get("pages") or []
    else:
        keywords, watchlist = DEFAULT_KEYWORDS, data
    today = datetime.now(VN_TZ).strftime("%d/%m/%Y")

    ids = [w["id"] for w in watchlist]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        print(f"Lỗi: id bị trùng trong watchlist: {dupes}", file=sys.stderr)
        return 1

    results = []
    for i, item in enumerate(watchlist):
        if i:
            time.sleep(DELAY_BETWEEN_PAGES)
        result = check_page(item, keywords)
        results.append(result)
        print(f"[{result['status']:>7}] {item['id']}")

    for r in results:
        it = r["item"]
        title = f"{it.get('program', it['id'])} - {it.get('page_type', 'trang')}"
        if r["status"] == "changed":
            send_telegram(f"🔔 {title}\nTrang đã thay đổi ({today})\n\n"
                          f"{r['diff']}\n\n🔗 {it['url']}")
        elif r["status"] == "error":
            send_telegram(f"⚠️ {title}\nKhông tải được trang ({today})\n"
                          f"Lỗi: {r['error']}\n🔗 {it['url']}")
        elif r["status"] == "blocked":
            send_telegram(f"⛔ {title}\nTrang này không cho phép bot đọc "
                          f"(robots.txt). Nên bỏ khỏi watchlist hoặc theo dõi thủ công.\n"
                          f"🔗 {it['url']}")

    new = [r for r in results if r["status"] == "new"]
    if new:
        names = "\n".join(f"- {r['item']['id']}" for r in new)
        send_telegram(f"✅ Bắt đầu theo dõi {len(new)} trang mới ({today}):\n{names}")

    rebased = [r for r in results if r["status"] == "rebaseline"]
    if rebased:
        names = "\n".join(f"- {r['item']['id']}" for r in rebased)
        send_telegram(f"🔄 Cách lọc đã thay đổi, lấy làm mốc so sánh mới "
                      f"cho {len(rebased)} trang ({today}):\n{names}")

    counts = {s: sum(r["status"] == s for r in results)
              for s in ("new", "rebaseline", "same", "changed", "error", "blocked")}
    print("Tổng kết:", counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
