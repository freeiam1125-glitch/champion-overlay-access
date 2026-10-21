"""구매 요청(이슈) 하나를 처리한다. GitHub Actions 에서 돈다 (표준 라이브러리만 쓴다).

이슈 본문의 JSON: {"v": 1, "kind": "riot"|"pc", "id": "...", "item": "champion:<Key>"|"pass7"|"pass30",
                   "reveal": "<사슬의 다음 값>", "key": "<처음 등록할 때의 열쇠>"}
결과는 이슈 댓글 한 줄: "ok <item> balance=<남은 잔액>" 또는 "error <까닭>". 처리 뒤 이슈를 닫는다.
"""
import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.error
import urllib.request

REPO = os.environ["REPO"]
TOKEN = os.environ["GITHUB_TOKEN"]
NUMBER = int(os.environ["ISSUE_NUMBER"])
FILE, BRANCH = "access.json", "main"
PRICES = {"champion": 300, "pass7": 500, "pass30": 1500}
PASS_DAYS = {"pass7": 7, "pass30": 30}
KEY_RE = re.compile(r"^[0-9a-f]{64}$")


def call(method, url, body=None):
    headers = {"Authorization": "Bearer " + TOKEN, "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "overleague-purchase-bot"}
    raw = None if body is None else json.dumps(body).encode("utf-8")
    with urllib.request.urlopen(urllib.request.Request(url, data=raw, headers=headers, method=method), timeout=30) as r:
        return json.loads(r.read().decode("utf-8") or "null")


def mark(salt, kind, value):
    return hashlib.sha256(("%s:%s:%s" % (salt, kind, value)).encode("utf-8")).hexdigest()[:40]


def parse(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    return json.loads(m.group(0)) if m else None


def apply(data, req, now):
    """목록(data)에 구매를 적용한다. (결과 글, 바뀌었는가)"""
    kind, value = req.get("kind"), str(req.get("id", "")).strip()
    if kind not in ("riot", "pc") or not value:
        return "error bad_request", False
    h = mark(data.get("salt", ""), kind, value)
    row = next((r for r in data.get(kind, []) if r.get("h") == h), None)
    if row is None:
        return "error not_registered", False
    reveal, key = str(req.get("reveal", "")), str(req.get("key", ""))
    if not KEY_RE.match(reveal):
        return "error bad_key", False
    expect = row.get("k") or (key if KEY_RE.match(key) else "")
    if not expect:
        return "error no_key", False
    if hashlib.sha256(bytes.fromhex(reveal)).hexdigest() != expect:
        return "error key_mismatch", False
    prices = dict(PRICES, **(data.get("prices") or {}))
    item = str(req.get("item", ""))
    balance = int(row.get("b", 0))
    if item.startswith("champion:"):
        champ = item.split(":", 1)[1]
        if not re.match(r"^[A-Za-z]{2,30}$", champ):
            return "error bad_item", False
        if row.get("c") is None or champ in row.get("c", []):
            row["k"] = reveal
            return "error already_owned", True
        price = int(prices["champion"])
        if balance < price:
            row["k"] = reveal
            return "error no_funds need=%d have=%d" % (price, balance), True
        row["b"] = balance - price
        row["c"] = sorted(set(row.get("c", [])) | {champ})
    elif item in PASS_DAYS:
        price = int(prices[item])
        if balance < price:
            row["k"] = reveal
            return "error no_funds need=%d have=%d" % (price, balance), True
        row["b"] = balance - price
        row["u"] = max(int(row.get("u", 0)), now) + PASS_DAYS[item] * 86400
    else:
        return "error bad_item", False
    row["k"] = reveal
    return "ok %s balance=%d" % (item, row["b"]), True


def main():
    base = "https://api.github.com/repos/%s" % REPO
    issue = call("GET", "%s/issues/%d" % (base, NUMBER))
    result = "error bad_request"
    if issue.get("state") == "open":
        req = None
        try:
            req = parse(issue.get("body", ""))
        except Exception:
            req = None
        if req is None or not str(issue.get("title", "")).startswith("buy"):
            result = "error bad_request"
        else:
            for attempt in range(4):   # 다른 곳에서 방금 고쳤으면 다시 받아 적용한다
                got = call("GET", "%s/contents/%s?ref=%s" % (base, FILE, BRANCH))
                data = json.loads(base64.b64decode(got["content"]).decode("utf-8"))
                result, changed = apply(data, req, int(time.time()))
                if not changed:
                    break
                data["rev"] = int(time.time())
                body = {"message": "purchase #%d" % NUMBER, "branch": BRANCH, "sha": got["sha"],
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
