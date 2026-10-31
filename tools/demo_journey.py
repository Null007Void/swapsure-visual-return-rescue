"""Drive the SwapSure offline journeys against a running local demo server, and record evidence.

Loopback only: this talks to 127.0.0.1 and nothing else. It follows the same states a shopper does;
every page captured here is also rendered in a real browser for the visual pass.

    python tools/demo_journey.py --base http://127.0.0.1:8765 --out journey-evidence
"""
import argparse
import http.cookiejar
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

FORM_RE = re.compile(r"<form[^>]*>.*?</form>", re.S)
TAG_RE = re.compile(r"<input[^>]*>", re.S)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Session:
    def __init__(self, base):
        self.base = base.rstrip("/")
        jar = http.cookiejar.CookieJar()
        self.raw = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect)
        self.follow = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def post(self, path, data):
        body = urllib.parse.urlencode(data, doseq=True).encode()
        req = urllib.request.Request(self.base + path, data=body,
                                     headers={"Origin": self.base}, method="POST")
        try:
            r = self.raw.open(req)
            return r.status, r.headers.get("Location"), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Location"), e.read().decode("utf-8", "replace")

    def get(self, path):
        return self.follow.open(self.base + path).read().decode("utf-8", "replace")


def attr(tag, name):
    marker = name + '="'
    start = tag.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = tag.find('"', start)
    return tag[start:end] if end > start else None


def forms(html):
    out = []
    for block in FORM_RE.findall(html):
        hidden, checks = {}, []
        for tag in TAG_RE.findall(block):
            n = attr(tag, "name")
            if not n:
                continue
            if (attr(tag, "type") or "").lower() == "checkbox":
                checks.append(n)
            else:
                v = attr(tag, "value")
                if v is not None:
                    hidden[n] = v
        out.append({"action": attr(block[:300], "action") or "", "hidden": hidden,
                    "checkboxes": checks, "html": block})
    return out


def form_ending(html, suffix):
    for f in forms(html):
        if f["action"].endswith(suffix):
            return f
    return None


def text_of(html):
    body = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    body = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", body).strip()


class Recorder:
    def __init__(self, out):
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.steps, self.refusals = [], []

    def page(self, name, path, html, extra=None):
        (self.out / (name + ".html")).write_text(html, encoding="utf-8")
        body = text_of(html)
        title = re.search(r"<title>(.*?)</title>", html, re.S)
        rec = {"step": name, "path": path, "title": title.group(1).strip() if title else "?",
               "chars": len(html), "has_refund_route": "refund" in html.lower(),
               "mock_labelled": "MOCK" in body, "fit_disclaimer": "Visual preview only" in body,
               "forbidden_claims": [p for p in ("guaranteed fit", "best on your body",
                                                "perfect match", "beauty score")
                                    if p in body.lower()],
               "text": body[:600]}
        if extra:
            rec.update(extra)
        self.steps.append(rec)
        print(f"  {name:26s} {rec['title'][:36]:36s} refund={rec['has_refund_route']} "
              f"mock={rec['mock_labelled']} disc={rec['fit_disclaimer']}")
        return rec

    def refusal(self, name, status, html):
        body = text_of(html)
        rec = {"step": name, "status": status,
               "codes": re.findall(r"\b[A-Z][A-Z_]{3,}\b", body)[:6], "text": body[:300]}
        self.refusals.append(rec)
        print(f"  REFUSAL {name}: status={status} codes={rec['codes'][:3]}")

    def save(self):
        (self.out / "journey.json").write_text(
            json.dumps({"steps": self.steps, "refusals": self.refusals}, indent=1), encoding="utf-8")


def rescue(base, rec):
    s = Session(base)
    print("rescue journey")
    status, loc, _ = s.post("/returns", {"reason": "COLOR"})
    assert status == 303, status
    page = s.get(loc)
    rec.page("01-decision-eligible", loc, page)
    f = form_ending(page, "/opt-in")
    s.post(f["action"], f["hidden"])
    page = s.get(loc)
    rec.page("02-consent", loc, page)
    f = form_ending(page, "/consent")
    data = dict(f["hidden"])
    data.update({"imageConsent": "on", "mockAcknowledged": "on"})
    status, loc, _ = s.post(f["action"], data)
    page = s.get(loc)
    rec.page("03-alternatives", loc, page)
    f = form_ending(page, "/preview")
    status, loc, html = s.post(f["action"], f["hidden"])
    page = s.get(loc) if loc else html
    rec.page("04-previews", loc or "(response)", page)
    choose = [x for x in forms(page) if x["action"].endswith("/choose")]
    if not choose:
        rec.refusal("choose-form-missing", 200, page)
        return
    data = dict(choose[0]["hidden"])
    data.setdefault("sku", attr(choose[0]["html"], "value"))
    status, loc, html = s.post(choose[0]["action"], data)
    page = s.get(loc) if loc else html
    rec.page("05-quote", loc or "(response)", page, {"sku": data.get("sku")})
    approve = form_ending(page, "/approve")
    if not approve:
        rec.refusal("approve-form-missing", 200, page)
        return
    status, loc, html = s.post(approve["action"], approve["hidden"])
    page = s.get(loc) if loc else html
    body = text_of(page)
    rec.page("06-receipt", loc or "(approve response)", page,
             {"receipt": {"simulated_label": "SIMULATED" in body,
                          "mentions_refund": "refund" in body.lower(), "text": body[:400]}})


def refusal(base, rec):
    s = Session(base)
    print("refusal journey (TOO_SMALL)")
    status, loc, _ = s.post("/returns", {"reason": "TOO_SMALL"})
    page = s.get(loc)
    rec.page("07-decision-ineligible", loc, page)
    if form_ending(page, "/opt-in") is not None:
        rec.refusal("opt-in-offered-for-ineligible", 200, page)
    else:
        print("  no preview invitation offered: correct")


def refund(base, rec):
    s = Session(base)
    print("refund journey (WANT_REFUND)")
    status, loc, _ = s.post("/returns", {"reason": "WANT_REFUND"})
    page = s.get(loc)
    rec.page("08-refund-decision", loc, page)
    f = form_ending(page, "/refund")
    status, loc, html = s.post(f["action"], f["hidden"])
    page = s.get(loc) if loc else html
    rec.page("09-refund-host", loc or "(response)", page)


def abuse(base, rec):
    s = Session(base)
    body = urllib.parse.urlencode({"reason": "COLOR"}).encode()
    req = urllib.request.Request(base.rstrip("/") + "/returns", data=body,
                                 headers={"Origin": "http://evil.example"}, method="POST")
    try:
        r = s.raw.open(req)
        status, html = r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, html = e.code, e.read().decode("utf-8", "replace")
    rec.refusal("cross-origin-post", status, html)
    try:
        rec.refusal("unknown-session", 200, s.get("/s/ses_does_not_exist"))
    except urllib.error.HTTPError as e:
        rec.refusal("unknown-session", e.code, e.read().decode("utf-8", "replace"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--out", default="journey-evidence")
    a = ap.parse_args()
    rec = Recorder(a.out)
    rescue(a.base, rec)
    refusal(a.base, rec)
    refund(a.base, rec)
    abuse(a.base, rec)
    rec.save()
    print("evidence:", (Path(a.out).resolve() / "journey.json"))
