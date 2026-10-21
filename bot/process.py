"""요청(이슈) 하나를 처리한다. GitHub Actions 에서 돈다 (표준 라이브러리만 쓴다).

이슈 본문의 JSON. 구매(라이엇·PC): {"v":1,"kind":"riot"|"pc","id":"...","item":"champion:<Key>"|"pass7"|"pass30","reveal":"...","key":"..."}
싸서 올린 요청: {"v":1,"enc":{"k":"<RSA 로 싼 임시 열쇠>","b":"<임시 열쇠로 잠근 본문>"}} — 본문의 "t" 가 종류:
  signup {email, v, k, name}  → 인증 코드 메일, 대기 목록(pending)        verify {email, code} → 계정 만들기
  login {email, v, k, name, replace}  → 기기 등록                          logout {email, reveal} → 기기 빼기
  reset {email} → 코드 메일 (pending_reset)                                reset_verify {email, code, v, k, name} → 비밀번호 바꾸고 기기 전부 교체
  buy (kind=email) {kind, id, item, reveal} → 기기 사슬로 구매
결과는 이슈 댓글 한 줄: "ok …" 또는 "error …". 처리 뒤 이슈를 닫는다.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import smtplib
import time
import urllib.error
import urllib.request
from email.message import EmailMessage

REPO = os.environ["REPO"]
TOKEN = os.environ["GITHUB_TOKEN"]
NUMBER = int(os.environ["ISSUE_NUMBER"])
FILE, BRANCH = "access.json", "main"
PRICES = {"champion": 300, "pass7": 500, "pass30": 1500}
PASS_DAYS = {"pass7": 7, "pass30": 30}
DEVICES = 2
HEX64 = re.compile(r"^[0-9a-f]{64}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CODE_MINUTES, LOGIN_MINUTES, PENDING_HOURS = 15, 10, 2


def call(method, url, body=None):
    headers = {"Authorization": "Bearer " + TOKEN, "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "overleague-purchase-bot"}
    raw = None if body is None else json.dumps(body).encode("utf-8")
    with urllib.request.urlopen(urllib.request.Request(url, data=raw, headers=headers, method=method), timeout=30) as r:
        return json.loads(r.read().decode("utf-8") or "null")


def mark(salt, kind, value):
    return hashlib.sha256(("%s:%s:%s" % (salt, kind, value)).encode("utf-8")).hexdigest()[:40]


def _stream(key, nonce, size):
    out, n = b"", 0
    while len(out) < size:
        out += hashlib.sha256(key + nonce + n.to_bytes(4, "big")).digest()
        n += 1
    return out[:size]


def lock(key, text):
    raw = text.encode("utf-8")
    nonce = os.urandom(12)
    body = bytes(a ^ b for a, b in zip(raw, _stream(key, nonce, len(raw))))
    tag = hmac.new(key, nonce + body, hashlib.sha256).digest()[:12]
    return base64.b64encode(nonce + tag + body).decode("ascii")


def unlock(key, packed):
    try:
        raw = base64.b64decode(packed)
        nonce, tag, body = raw[:12], raw[12:24], raw[24:]
        if not hmac.compare_digest(tag, hmac.new(key, nonce + body, hashlib.sha256).digest()[:12]):
            return ""
        return bytes(a ^ b for a, b in zip(body, _stream(key, nonce, len(body)))).decode("utf-8")
    except Exception:
        return ""


def box_key():
    text = os.environ.get("BOX_KEY", "").strip()
    return bytes.fromhex(text) if len(text) == 64 else None


def unseal(enc):
    """프로그램이 공개 열쇠로 싼 본문을 비밀 열쇠로 푼다. 못 풀면 None."""
    try:
        n_hex, d_hex = os.environ["BOT_PRIV"].strip().split(":")
        n, d = int(n_hex, 16), int(d_hex, 16)
        m = pow(int(enc["k"], 16), d, n)
        key = m.to_bytes((n.bit_length() + 7) // 8, "big")[-32:]
        text = unlock(key, enc["b"])
        return json.loads(text) if text else None
    except Exception:
        return None


def parse(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    return json.loads(m.group(0)) if m else None


def tidy_email(text):
    text = str(text or "").strip().lower()
    return text if EMAIL_RE.match(text) and len(text) <= 120 else ""


def send_code(email, code, purpose):
    host, user, password = os.environ.get("MAIL_HOST", ""), os.environ.get("MAIL_USER", ""), os.environ.get("MAIL_PASS", "")
    if not (host and user and password):
        raise RuntimeError("mail_not_configured")
    port = int(os.environ.get("MAIL_PORT", "465") or 465)
    msg = EmailMessage()
    msg["Subject"] = "[Overleague] 인증 코드 %s" % code
    msg["From"] = os.environ.get("MAIL_FROM", "") or user
    msg["To"] = email
    what = "가입" if purpose == "signup" else "비밀번호 재설정"
    msg.set_content("Overleague %s 인증 코드: %s\n\n%d분 안에 프로그램에 입력해 주세요. 본인이 요청한 것이 아니면 이 메일은 무시하세요.\n\n"
                    "Overleague %s verification code: %s (valid for %d minutes)."
                    % (what, code, CODE_MINUTES, "sign-up" if purpose == "signup" else "password reset", code, CODE_MINUTES))
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls()
    with server:
        server.login(user, password)
        server.send_message(msg)


def code_hash(salt, email, code):
    return hashlib.sha256(("code:%s:%s:%s" % (salt, email, code)).encode("utf-8")).hexdigest()


def new_code():
    return "%06d" % (int.from_bytes(os.urandom(4), "big") % 1000000)


def purge(data, now):
    for name in ("pending", "pending_reset"):
        table = data.get(name) or {}
        data[name] = {h: p for h, p in table.items() if now - int(p.get("t", 0)) < PENDING_HOURS * 3600}


def user_row(data, email):
    h = mark(data.get("salt", ""), "email", email)
    return next((r for r in data.get("email", []) if r.get("h") == h), None), h


def buy(data, req, now):
    """목록(data)에 구매를 적용한다. (결과 글, 바뀌었는가)"""
    kind, value = req.get("kind"), str(req.get("id", "")).strip()
    if kind not in ("riot", "pc", "email") or not value:
        return "error bad_request", False
    reveal, key = str(req.get("reveal", "")), str(req.get("key", ""))
    if not HEX64.match(reveal):
        return "error bad_key", False
    if kind == "email":
        row, _h = user_row(data, tidy_email(value))
        if row is None:
            return "error not_registered", False
        dev = next((d for d in row.get("d", []) if hashlib.sha256(bytes.fromhex(reveal)).hexdigest() == d.get("k")), None)
        if dev is None:
            return "error key_mismatch", False
        holder = dev
    else:
        h = mark(data.get("salt", ""), kind, value)
        row = next((r for r in data.get(kind, []) if r.get("h") == h), None)
        if row is None:
            return "error not_registered", False
        expect = row.get("k") or (key if HEX64.match(key) else "")
        if not expect:
            return "error no_key", False
        if hashlib.sha256(bytes.fromhex(reveal)).hexdigest() != expect:
            return "error key_mismatch", False
        holder = row
    prices = dict(PRICES, **(data.get("prices") or {}))
    item = str(req.get("item", ""))
    balance = int(row.get("b", 0))
    holder["k"] = reveal   # 열쇠는 결과와 상관없이 앞으로 나간다 (같은 값을 두 번 못 쓰게)
    if item.startswith("champion:"):
        champ = item.split(":", 1)[1]
        if not re.match(r"^[A-Za-z]{2,30}$", champ):
            return "error bad_item", True
        if row.get("c") is None or champ in row.get("c", []):
            return "error already_owned", True
        price = int(prices["champion"])
        if balance < price:
            return "error no_funds need=%d have=%d" % (price, balance), True
        row["b"] = balance - price
        row["c"] = sorted(set(row.get("c", [])) | {champ})
    elif item in PASS_DAYS:
        price = int(prices[item])
        if balance < price:
            return "error no_funds need=%d have=%d" % (price, balance), True
        row["b"] = balance - price
        row["u"] = max(int(row.get("u", 0)), now) + PASS_DAYS[item] * 86400
    else:
        return "error bad_item", True
    return "ok %s balance=%d" % (item, row["b"]), True


def account(data, req, now):
    """가입·로그인·비밀번호 요청. (결과 글, 바뀌었는가)"""
    box = box_key()
    if box is None:
        return "error box_not_configured", False
    kind = str(req.get("t", ""))
    email = tidy_email(req.get("email"))
    if not email:
        return "error bad_email", False
    if abs(now - int(req.get("ts", 0))) > LOGIN_MINUTES * 60:
        return "error stale", False
    row, h = user_row(data, email)
    name = str(req.get("name", ""))[:40]
    data.setdefault("email", [])
    if kind == "signup":
        if row is not None:
            return "error exists", False
        v, k = str(req.get("v", "")), str(req.get("k", ""))
        if not (HEX64.match(v) and HEX64.match(k)):
            return "error bad_request", False
        pending = data.setdefault("pending", {})
        if h in pending and now - int(pending[h].get("t", 0)) < 60:
            return "error wait", False
        code = new_code()
        try:
            send_code(email, code, "signup")
        except Exception as e:
            return "error mail_failed %s" % type(e).__name__, False
        pending[h] = {"n": lock(box, email), "v": lock(box, v), "k": k, "name": name, "c": code_hash(data.get("salt", ""), email, code), "t": now}
        return "ok code_sent", True
    if kind == "verify":
        pending = data.get("pending") or {}
        p = pending.get(h)
        if p is None or now - int(p.get("t", 0)) > CODE_MINUTES * 60:
            return "error no_pending", False
        if not hmac.compare_digest(p.get("c", ""), code_hash(data.get("salt", ""), email, str(req.get("code", "")).strip())):
            return "error bad_code", False
        if row is None:
            data["email"].append({"h": h, "n": lock(box, json.dumps({"id": email, "note": p.get("name", "")}, ensure_ascii=False)),
                                  "v": p["v"], "d": [{"k": p["k"], "name": p.get("name", ""), "t": now}], "b": 0, "c": []})
        del pending[h]
        return "ok signed_up", True
    if row is None:
        return "error no_account", False
    if kind == "login":
        v, k = str(req.get("v", "")), str(req.get("k", ""))
        if not (HEX64.match(v) and HEX64.match(k)):
            return "error bad_request", False
        if not hmac.compare_digest(unlock(box, row.get("v", "")), v):
            return "error bad_password", False
        devices = row.setdefault("d", [])
        if any(d.get("k") == k for d in devices):
            return "ok logged_in", False
        if len(devices) >= DEVICES:
            if not req.get("replace"):
                return "error device_limit", False
            devices.sort(key=lambda d: int(d.get("t", 0)))
            del devices[0]
        devices.append({"k": k, "name": name, "t": now})
        return "ok logged_in", True
    if kind == "logout":
        reveal = str(req.get("reveal", ""))
        if not HEX64.match(reveal):
            return "error bad_key", False
        before = len(row.get("d", []))
        row["d"] = [d for d in row.get("d", []) if hashlib.sha256(bytes.fromhex(reveal)).hexdigest() != d.get("k")]
        return ("ok logged_out", True) if len(row["d"]) < before else ("error key_mismatch", False)
    if kind == "reset":
        pending = data.setdefault("pending_reset", {})
        if h in pending and now - int(pending[h].get("t", 0)) < 60:
            return "error wait", False
        code = new_code()
        try:
            send_code(email, code, "reset")
        except Exception as e:
            return "error mail_failed %s" % type(e).__name__, False
        pending[h] = {"c": code_hash(data.get("salt", ""), email, code), "t": now}
        return "ok code_sent", True
    if kind == "reset_verify":
        pending = data.get("pending_reset") or {}
        p = pending.get(h)
        if p is None or now - int(p.get("t", 0)) > CODE_MINUTES * 60:
            return "error no_pending", False
        if not hmac.compare_digest(p.get("c", ""), code_hash(data.get("salt", ""), email, str(req.get("code", "")).strip())):
            return "error bad_code", False
        v, k = str(req.get("v", "")), str(req.get("k", ""))
        if not (HEX64.match(v) and HEX64.match(k)):
            return "error bad_request", False
        row["v"] = lock(box, v)
        row["d"] = [{"k": k, "name": name, "t": now}]
        del pending[h]
        return "ok reset", True
    return "error bad_request", False


def handle(data, req, now):
    purge(data, now)
    if "enc" in req:
        req = unseal(req["enc"])
        if req is None:
            return "error cannot_unseal", True
    kind = str(req.get("t", "buy"))
    if kind == "buy" or "item" in req:
        return buy(data, req, now)
    return account(data, req, now)


def main():
    base = "https://api.github.com/repos/%s" % REPO
    issue = call("GET", "%s/issues/%d" % (base, NUMBER))
    result = "error bad_request"
    if issue.get("state") == "open":
        try:
            req = parse(issue.get("body", ""))
        except Exception:
            req = None
        if req is None:
            result = "error bad_request"
        else:
            for attempt in range(4):   # 다른 곳에서 방금 고쳤으면 다시 받아 적용한다
                got = call("GET", "%s/contents/%s?ref=%s" % (base, FILE, BRANCH))
                data = json.loads(base64.b64decode(got["content"]).decode("utf-8"))
                result, changed = handle(data, req, int(time.time()))
                if not changed:
                    break
                data["rev"] = int(time.time())
                body = {"message": "request #%d" % NUMBER, "branch": BRANCH, "sha": got["sha"],
                        "content": base64.b64encode(json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")).decode("ascii")}
                try:
                    call("PUT", "%s/contents/%s" % (base, FILE), body)
                    break
                except urllib.error.HTTPError as e:
                    if e.code not in (409, 422) or attempt == 3:
                        result = "error save_failed http=%d" % e.code
                        break
                    time.sleep(2)
    call("POST", "%s/issues/%d/comments" % (base, NUMBER), {"body": result})
    call("PATCH", "%s/issues/%d" % (base, NUMBER), {"state": "closed"})
    print(result)


if __name__ == "__main__":
    main()
