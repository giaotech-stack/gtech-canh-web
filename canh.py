#!/usr/bin/env python3
"""canh-web: gác cổng sức khỏe (health endpoint) và báo Telegram khi đổi trạng thái.

Chỉ dùng thư viện chuẩn Python 3. Cấu hình hoàn toàn qua biến môi trường (secret):
  TARGETS             nhiều dòng, mỗi dòng `nhãn|https://.../health`
  TELEGRAM_BOT_TOKEN  token bot
  TELEGRAM_CHAT_ID    id chat nhận báo
  THU_BAO_DONG        "true" = chế độ thử báo động (xem README)
  STATE_FILE          đường dẫn file state (hoặc truyền làm đối số dòng lệnh)

AN TOÀN: repo này công khai và log Actions cũng công khai. Tuyệt đối không in URL,
token hay nhãn ra log; chỉ in "target #N: UP/DOWN (mã)" và "alert sent: yes/no".

Mã thoát: 0 = ổn (kể cả khi có target DOWN, vì tin nhắn Telegram mới là tín hiệu);
          1 = gửi Telegram thất bại (để run đỏ, GitHub gửi mail cho chủ repo);
          2 = thiếu/sai cấu hình.
"""

import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:  # thiếu tzdata -> UTC+7 cố định (VN không có giờ mùa hè)
    VN_TZ = timezone(timedelta(hours=7))

TIMEOUT = 20          # giây cho mỗi lần gọi
RETRIES = 2           # thử lại thêm tối đa 2 lần trước khi kết luận DOWN
RETRY_DELAY = 20      # giây giữa các lần thử
TARGET_BUDGET = 75    # giây tối đa cho MỘT target (mọi lần thử + chờ cộng lại)
MIN_ATTEMPT = 3       # không bắt đầu lần thử mới nếu còn ít hơn ngần này giây
REMINDER_MIN = 60     # nhắc lại mỗi 60 phút khi vẫn DOWN
# Lịch GitHub trễ/lệch vài phút, nên 4 chu kỳ 15' có thể ra 58-59'. Cho phép lệch
# để nhắc không bị trượt sang chu kỳ kế (75').
REMINDER_TOLERANCE_MIN = 5
MAX_BODY = 65536
SUFFIX_TEST = "-thu-loi"


class ConfigError(Exception):
    """Lỗi cấu hình. Thông điệp KHÔNG được chứa nội dung secret."""


# ---------------------------------------------------------------- tiện ích

_TOKEN_RE = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_DIGITS_RE = re.compile(r"\d{9,}")


def redact(text):
    """Lưới an toàn: che token bot, URL và chuỗi >= 9 chữ số (SĐT/ID)."""
    text = _TOKEN_RE.sub("[đã ẩn]", text)
    text = _URL_RE.sub("[đã ẩn]", text)
    text = _DIGITS_RE.sub("[đã ẩn]", text)
    return text


def parse_targets(raw):
    """`nhãn|url` mỗi dòng; bỏ dòng trống. Lỗi chỉ nêu số dòng, không lộ nội dung."""
    targets = []
    for no, line in enumerate((raw or "").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        label, sep, url = line.rpartition("|")
        label, url = label.strip(), url.strip()
        if not sep or not label or not url:
            raise ConfigError(f"TARGETS dòng {no}: không đúng dạng `nhãn|https://...`")
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ConfigError(f"TARGETS dòng {no}: URL phải bắt đầu bằng http(s)://")
        targets.append((label, url))
    if not targets:
        raise ConfigError("TARGETS rỗng: cần ít nhất 1 dòng `nhãn|https://...`")
    return targets


def derive_test_url(url):
    """Thêm `-thu-loi` vào cuối path -> chắc chắn 404."""
    parts = urllib.parse.urlsplit(url)
    path = parts.path.rstrip("/") + SUFFIX_TEST
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def fmt_time(dt):
    return dt.astimezone(VN_TZ).strftime("%H:%M %d/%m/%Y (giờ VN)")


def fmt_duration(delta):
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "dưới 1 phút"
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    out = []
    if days:
        out.append(f"{days} ngày")
    if hours:
        out.append(f"{hours} giờ")
    if mins:
        out.append(f"{mins} phút")
    return " ".join(out)


# ---------------------------------------------------------------- kiểm tra

def fetch(url, timeout=TIMEOUT):
    """Trả (status, body_bytes). Lỗi mạng -> ném ngoại lệ (check_target xử lý)."""
    req = urllib.request.Request(url, headers={"User-Agent": "gtech-canh-web/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(MAX_BODY)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(MAX_BODY)
        except Exception:
            body = b""
        return exc.code, body


def _classify_exception(exc):
    # Không dùng str(exc): có thể chứa URL.
    reason = getattr(exc, "reason", exc)
    if isinstance(exc, (socket.timeout, TimeoutError)) or isinstance(
        reason, (socket.timeout, TimeoutError)
    ):
        return "timeout"
    return "lỗi kết nối"


def check_once(url, fetch_fn, timeout=TIMEOUT):
    """Một lần gọi -> (up, detail). detail dùng cho log và lý do trong tin nhắn.

    KHÔNG BAO GIỜ ném ngoại lệ: lỗi bất ngờ nào cũng thành DOWN "lỗi kiểm tra"
    (chỉ log tên lớp ngoại lệ, vì thông điệp có thể chứa URL).
    """
    try:
        try:
            status, body = fetch_fn(url, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - mọi lỗi mạng đều = không phản hồi
            return False, _classify_exception(exc)
        if status != 200:
            return False, f"HTTP {status}"
        try:
            # utf-8-sig: chấp nhận BOM đầu phản hồi. Bắt Exception (kể cả RecursionError
            # khi JSON lồng quá sâu) -> coi là phản hồi không hợp lệ.
            data = json.loads(body.decode("utf-8-sig"))
        except Exception:  # noqa: BLE001
            return False, "phản hồi không hợp lệ"
        if not isinstance(data, dict):
            return False, "phản hồi không hợp lệ"
        if data.get("status") != "ok":
            return False, "status không phải ok"
        return True, "HTTP 200"
    except Exception as exc:  # noqa: BLE001 - lưới cuối: không để lọt ra vòng chính
        print(f"lỗi kiểm tra bất ngờ: {type(exc).__name__}")
        return False, "lỗi kiểm tra"


def check_target(url, fetch_fn=fetch, sleep_fn=time.sleep, retries=RETRIES,
                 delay=RETRY_DELAY, budget=TARGET_BUDGET, clock=time.monotonic):
    """Thử tối đa retries+1 lần, nhưng tổng thời gian cho target <= ~budget giây.

    Mỗi lần thử có timeout = min(TIMEOUT, thời gian còn lại). Không đủ thời gian cho
    (chờ + 1 lần thử tối thiểu) thì dừng thử tiếp và kết luận theo lần cuối.
    """
    deadline = clock() + budget
    result = (False, "lỗi kết nối")
    for attempt in range(retries + 1):
        remaining = deadline - clock()
        if attempt > 0 and remaining < MIN_ATTEMPT:
            break
        result = check_once(url, fetch_fn, min(TIMEOUT, max(remaining, MIN_ATTEMPT)))
        if result[0]:
            return result
        if attempt < retries:
            if deadline - clock() - delay < MIN_ATTEMPT:
                break
            sleep_fn(delay)
    return result


# ---------------------------------------------------------------- trạng thái

def decide(prev, up, now):
    """Hàm thuần. Trả (kind, new_state).

    kind: None | "down" | "reminder" | "up". new_state là trạng thái SẼ LƯU nếu
    việc báo (nếu có) thành công; nếu báo thất bại, người gọi bỏ new_state và giữ prev
    để lần sau thử báo lại.
    """
    iso = now.isoformat()
    if prev is None:  # lần đầu / state mất: chỉ báo nếu DOWN
        if up:
            return None, {"up": True, "since": iso, "last_alert": None, "alerted": True}
        return "down", {"up": False, "since": iso, "last_alert": iso, "alerted": True}
    if prev["up"] and up:
        return None, prev
    if prev["up"] and not up:
        return "down", {"up": False, "since": iso, "last_alert": iso, "alerted": True}
    if not prev["up"] and up:
        if not prev.get("alerted", True):
            # DOWN chưa từng báo được (Telegram lỗi) rồi đã hồi phục: không có gì để "báo lại"
            return None, {"up": True, "since": iso, "last_alert": None, "alerted": True}
        return "up", {"up": True, "since": iso, "last_alert": iso, "alerted": True}
    if not prev.get("alerted", True):
        # DOWN đã phát hiện nhưng báo thất bại: báo lại, giữ nguyên `since` lần phát hiện đầu
        return "down", {**prev, "last_alert": iso, "alerted": True}
    # vẫn DOWN: nhắc mỗi REMINDER_MIN phút
    last = datetime.fromisoformat(prev.get("last_alert") or prev["since"])
    if now - last >= timedelta(minutes=REMINDER_MIN - REMINDER_TOLERANCE_MIN):
        return "reminder", {**prev, "last_alert": iso}
    return None, prev


def build_message(kind, label, detail, now, prev):
    when = fmt_time(now)
    if kind == "down":
        extra = ""
        if prev and not prev.get("up", True) and not prev.get("alerted", True):
            extra = f"Phát hiện từ: {fmt_time(datetime.fromisoformat(prev['since']))}\n"
        return f"🔴 {label} KHÔNG phản hồi\n{extra}Lý do: {detail}\nLúc: {when}"
    if kind == "reminder":
        since = datetime.fromisoformat(prev["since"])
        return (
            f"🔴 {label} vẫn KHÔNG phản hồi (đã {fmt_duration(now - since)})\n"
            f"Lý do: {detail}\nLúc: {when}"
        )
    since = datetime.fromisoformat(prev["since"])
    return (
        f"🟢 {label} đã hoạt động lại\n"
        f"Thời gian ngừng: {fmt_duration(now - since)}\nLúc: {when}"
    )


def load_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        print("không có state trước (lần đầu hoặc cache hết hạn)")
        return {}
    except Exception:  # noqa: BLE001 - OSError, ValueError, RecursionError...
        pass
    print("state hỏng hoặc không đọc được: coi như lần chạy đầu")
    return {}


def _valid_iso(value):
    """Chuỗi ISO 8601 có múi giờ (decide() trừ datetime có múi giờ với nhau)."""
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except (ValueError, TypeError):
        return False


def valid_entry(entry):
    """Trả entry đã chuẩn hóa nếu hợp lệ, ngược lại None (coi như chưa có state)."""
    if not isinstance(entry, dict):
        return None
    if not isinstance(entry.get("up"), bool) or not _valid_iso(entry.get("since")):
        return None
    last_alert = entry.get("last_alert")
    if last_alert is not None and not _valid_iso(last_alert):
        return None
    alerted = entry.get("alerted", True)  # state cũ chưa có trường này = đã báo
    if not isinstance(alerted, bool):
        return None
    return {"up": entry["up"], "since": entry["since"], "last_alert": last_alert,
            "alerted": alerted}


def save_state(path, state):
    """Ghi nguyên tử: ghi file tạm cùng thư mục rồi os.replace."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- Telegram

def send_telegram(token, chat_id, text, timeout=TIMEOUT):
    """Gửi văn bản thuần. True nếu Telegram trả ok=true. Không in/ném chi tiết (URL có token)."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": redact(text)}).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read(MAX_BODY).decode("utf-8"))
        return isinstance(body, dict) and body.get("ok") is True
    except Exception:  # noqa: BLE001 - cố ý không lộ chi tiết (có thể chứa token)
        return False


# ---------------------------------------------------------------- chạy chính

def main(argv=None, env=None, fetch_fn=fetch, send_fn=send_telegram,
         sleep_fn=time.sleep, now_fn=None):
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = env.get("TELEGRAM_CHAT_ID", "").strip()
    missing = [k for k, v in (("TARGETS", env.get("TARGETS", "").strip()),
                              ("TELEGRAM_BOT_TOKEN", token),
                              ("TELEGRAM_CHAT_ID", chat_id)) if not v]
    if missing:
        print(f"LỖI cấu hình: thiếu secret {', '.join(missing)}", file=sys.stderr)
        return 2
    try:
        targets = parse_targets(env["TARGETS"])
    except ConfigError as exc:
        print(f"LỖI cấu hình: {exc}", file=sys.stderr)
        return 2

    state_path = (argv[0] if argv else env.get("STATE_FILE")) or "canh-state/state.json"
    test_mode = env.get("THU_BAO_DONG", "").strip().lower() == "true"
    state = load_state(state_path)
    failed = False

    # bỏ state của target đã bị xóa khỏi TARGETS (chỉ trong bộ nhớ; ghi ra file ở lần lưu kế)
    for key in [k for k in state if not (k.isdigit() and 1 <= int(k) <= len(targets))]:
        del state[key]

    def notify(text):
        ok = send_fn(token, chat_id, redact(text))
        print(f"alert sent: {'yes' if ok else 'no'}")
        return ok

    def persist():
        # Lưu ngay sau MỖI target: job bị kill giữa chừng cũng không báo lặp target đã xong.
        try:
            save_state(state_path, state)
            return True
        except OSError:
            print("LỖI: không ghi được state", file=sys.stderr)
            return False

    for idx, (label, url) in enumerate(targets, start=1):
        try:
            up, detail = check_target(url, fetch_fn, sleep_fn)
        except Exception as exc:  # noqa: BLE001 - không để 1 target làm sập cả vòng
            print(f"lỗi kiểm tra bất ngờ: {type(exc).__name__}")
            up, detail = False, "lỗi kiểm tra"
        print(f"target #{idx}: {'UP' if up else 'DOWN'} ({detail})")
        now = now_fn()
        key = str(idx)
        raw = state.get(key)
        prev = valid_entry(raw)
        if prev is None:
            if raw is not None:
                print(f"state #{idx} hỏng, coi như lần đầu")
            else:
                print(f"state #{idx} chưa có, coi như lần đầu")
        kind, new_state = decide(prev, up, now)
        if kind is None:
            state[key] = new_state
            print("alert: không cần")
        elif notify(build_message(kind, label, detail, now, prev or new_state)):
            state[key] = new_state
        else:
            failed = True
            if kind == "down":
                # Ghi nhận lần phát hiện đầu (giữ `since`) nhưng đánh dấu CHƯA báo được
                # để lượt sau báo lại. Các nhánh khác (nhắc/hồi phục) giữ nguyên prev.
                state[key] = {**new_state, "alerted": False,
                              "last_alert": prev["last_alert"] if prev and not prev["up"] else None}
            elif prev is not None:
                state[key] = prev
        if not persist():
            failed = True

    if test_mode:
        label0, url0 = targets[0]
        up, detail = check_target(derive_test_url(url0), fetch_fn, sleep_fn, retries=0)
        print(f"target #thử: {'UP' if up else 'DOWN'} ({detail})")
        now = now_fn()
        test_label = f"{label0} (THỬ báo động)"
        kind, _ = decide(None, up, now)
        if kind is None:  # hiếm: URL thử lại trả 200 -> vẫn báo để chủ biết
            msg = f"⚠️ {test_label}: URL thử trả về bình thường, không ra lỗi như dự kiến\nLúc: {fmt_time(now)}"
        else:
            msg = build_message(kind, test_label, detail, now, None)
        if not notify(msg):
            failed = True
        if not notify("✅ Kết thúc thử báo động. Các tin 🔴 ở trên chỉ là thử, không phải sự cố thật."):
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
