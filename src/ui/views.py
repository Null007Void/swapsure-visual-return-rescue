"""Server-rendered HTML for the offline shopper workflow.

Rules that hold for every page:

* every dynamic value passes through :func:`esc`; there is no template language
  that could mark data as safe, and the build ships **no** ``<script>`` element;
* the ordinary refund link is in the header of every page, independent of the
  session, the store or any rescue state;
* the mode/limitations panel states MOCK, zero units and "placeholder" on every
  page, including error pages;
* copy stays neutral: no fit, sizing, comfort, attractiveness or beauty claim;
* controls are native form controls with explicit labels, and status changes are
  announced through ``role="status"`` live regions.
"""

from __future__ import annotations

import html
from typing import Any, Iterable, Mapping, Sequence

from commerce.quote import Quote
from domain.catalog import CandidateSelection
from domain.eligibility import EligibilityDecision
from ui.media import MediaView
from ui.service import CaseView

MODE_LABEL = "MOCK PREVIEW - PLACEHOLDER - NOT A YOUCAM RESULT"
DISCLAIMER = "Visual preview only — not a fit or sizing guarantee."
REFUND_LINK = "/refund-host"

FORBIDDEN_COPY = (
    "slimming",
    "flattering",
    "attractive",
    "guaranteed fit",
    "best on your body",
    "perfect for your shape",
)

_STYLE = """:root {
  color-scheme: light;
  --ink:#171614; --muted:#5d564c; --line:#e3dccf; --paper:#faf6ef; --card:#fffdf9;
  --accent:#2f6f6a; --accent-dark:#245652; --warn:#8a3324; --radius:14px;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--paper); color: var(--ink);
  font: 16px/1.55 "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
}
h1, h2, h3, .brand { font-family: "Helvetica Neue", Arial, sans-serif; font-weight: 600; }
h1 { font-size: clamp(1.7rem, 2.6vw, 2.3rem); letter-spacing: -.01em; margin: 30px 0 10px; }
h2 { font-size: 1.12rem; margin: 0 0 10px; }
h3 { font-size: 1rem; margin: 0 0 6px; }
p, li { max-width: 68ch; }
a { color: var(--accent-dark); text-underline-offset: 3px; }
a:hover { color: var(--ink); }
.wrap { max-width: 1120px; margin: 0 auto; padding: 0 26px; }
header.site { border-bottom: 1px solid var(--line); background: rgba(255,253,249,.85); }
header.site .wrap { display: flex; flex-wrap: wrap; align-items: baseline; gap: 10px 26px; padding: 18px 26px; }
.brand { font-size: 1.05rem; letter-spacing: .01em; margin: 0; }
header.site nav ul { display: flex; flex-wrap: wrap; gap: 18px; list-style: none; margin: 0; padding: 0; }
header.site nav a { font: 0.92rem/1 "Helvetica Neue", Arial, sans-serif; text-decoration: none; color: var(--muted); }
header.site nav a:hover, header.site nav a[aria-current="page"] { color: var(--ink); }
main { display: block; padding-bottom: 56px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: var(--radius);
        padding: 26px; margin: 0 0 22px; }
.card > :last-child { margin-bottom: 0; }
.mode { display: inline-block; font: 600 .72rem/1 "Helvetica Neue", Arial, sans-serif;
        letter-spacing: .10em; text-transform: uppercase; color: var(--muted);
        border: 1px solid var(--line); border-radius: 999px; padding: 7px 12px; margin: 22px 0 0; }
.muted { color: var(--muted); }
.small { font-size: .88rem; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(248px, 1fr));
        gap: 22px; list-style: none; margin: 0 0 26px; padding: 0; }
.garment { background: var(--card); border: 1px solid var(--line); border-radius: var(--radius);
           padding: 16px; display: flex; flex-direction: column; gap: 10px; }
.garment img { width: 100%; height: auto; aspect-ratio: 2 / 3; object-fit: cover;
               border-radius: 10px; background: #f2ece1; display: block; }
.garment h3 { margin: 2px 0 0; }
.garment .price { font-family: "Helvetica Neue", Arial, sans-serif; font-size: .95rem; }
.garment form { margin: 6px 0 0; }
.badge { display: inline-block; font: 600 .68rem/1 "Helvetica Neue", Arial, sans-serif;
         letter-spacing: .09em; text-transform: uppercase; color: var(--accent-dark);
         border: 1px solid var(--accent); border-radius: 999px; padding: 5px 9px; background: #f2f7f5; }
dl.facts { display: grid; grid-template-columns: minmax(120px, 190px) 1fr; gap: 8px 18px; margin: 14px 0 0; }
dl.facts dt { color: var(--muted); font: .88rem/1.4 "Helvetica Neue", Arial, sans-serif; }
dl.facts dd { margin: 0; }
table, table.amounts { width: 100%; border-collapse: collapse; margin: 12px 0 0; }
caption { text-align: left; color: var(--muted); font: .85rem/1.4 "Helvetica Neue", Arial, sans-serif; padding-bottom: 8px; }
th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font: 600 .82rem/1.3 "Helvetica Neue", Arial, sans-serif; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }
button, .button { font: 600 .95rem/1 "Helvetica Neue", Arial, sans-serif; border-radius: 10px;
                  border: 1px solid var(--line); background: #fff; color: var(--ink);
                  padding: 12px 18px; cursor: pointer; min-height: 44px; }
button:hover, .button:hover { border-color: var(--accent); }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
button.primary:hover { background: var(--accent-dark); }
button.danger { color: var(--warn); border-color: #d9bfb6; background: #fffaf8; }
button:disabled { opacity: .5; cursor: not-allowed; }
:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; border-radius: 6px; }
input[type=file] { font: .92rem/1.4 "Helvetica Neue", Arial, sans-serif; }
input[type=radio] { accent-color: var(--accent); }
label.inline { display: flex; gap: 10px; align-items: center; font-family: "Helvetica Neue", Arial, sans-serif;
               font-size: .95rem; padding: 8px 0; }
fieldset { border: 1px solid var(--line); border-radius: 12px; padding: 16px 18px; margin: 0 0 16px; }
legend { font: 600 .86rem/1 "Helvetica Neue", Arial, sans-serif; padding: 0 6px; color: var(--muted); text-transform: uppercase; letter-spacing: .07em; }
figure { margin: 0 0 14px; }
figure img { display: block; width: 100%; max-width: 260px; height: auto; border-radius: 10px; border: 1px solid var(--line); }
figcaption { margin-top: 8px; color: var(--muted); font-size: .88rem; }
.error { border: 1px solid #d9bfb6; background: #fff6f2; color: var(--warn); border-radius: 12px; padding: 14px 16px; }
.notice { border: 1px solid var(--line); background: #f6f2e9; border-radius: 12px; padding: 14px 16px; }
ol.steps { display: flex; flex-wrap: wrap; gap: 10px 8px; list-style: none; margin: 20px 0 26px; padding: 0;
           font: 600 .76rem/1 "Helvetica Neue", Arial, sans-serif; letter-spacing: .07em; text-transform: uppercase; }
ol.steps li { color: var(--muted); padding: 8px 12px; border: 1px solid var(--line); border-radius: 999px; background: var(--card); }
ol.steps li[aria-current="step"] { color: #fff; background: var(--accent); border-color: var(--accent); }
footer.site { border-top: 1px solid var(--line); margin-top: 40px; padding: 22px 0 40px; color: var(--muted);
              font: .86rem/1.6 "Helvetica Neue", Arial, sans-serif; }
footer.site ul { list-style: none; display: flex; flex-wrap: wrap; gap: 16px; padding: 0; margin: 8px 0 0; }
@media (max-width: 720px) {
  .wrap { padding: 0 16px; }
  h1 { margin-top: 22px; }
  .card { padding: 18px; border-radius: 12px; }
  .grid { grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 14px; }
  dl.facts { grid-template-columns: 1fr; gap: 2px 0; }
  button, .button { width: 100%; }
  ol.steps { font-size: .7rem; }
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; animation: none !important; } }"""


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def money(minor: int, currency: str = "USD") -> str:
    sign = "-" if minor < 0 else ""
    whole, cents = divmod(abs(int(minor)), 100)
    return f"{sign}{currency} {whole:,}.{cents:02d}"


def _mode_panel(mode: str = "MOCK") -> str:
    if mode == "LIVE":
        detail = (
            "<p><strong>Mode: LIVE.</strong> Real YouCam provider tasks are created for the "
            "alternatives you choose, and provider units are spent per image. Result images are "
            "fetched from the provider for your photo and served from this site. "
            + esc(DISCLAIMER) + "</p>"
        )
    else:
        detail = (
            "<p><strong>Mode: MOCK.</strong> Simulated merchant and simulated exchange. "
            "Zero YouCam units and zero provider calls. Previews are local placeholders, "
            "not YouCam results. " + esc(DISCLAIMER) + "</p>"
        )
    return (
        '<section class="mode" aria-labelledby="mode-heading">'
        '<h2 id="mode-heading">Mode and limitations</h2>'
        + detail +
        "</section>"
    )


_STEP_LABELS = ("Choose a garment", "Photo and consent", "Preview", "Decision")


def steps_html(step: int | None) -> str:
    """A four-step strip so the shopper always knows where they are."""
    if step not in (1, 2, 3, 4):
        return ""
    parts = []
    for index, label in enumerate(_STEP_LABELS, start=1):
        mark = ' aria-current="step"' if index == step else ""
        parts.append(f"<li{mark}>{index}. {label}</li>")
    return '<ol class="steps" aria-label="Progress through the rescue">' + "".join(parts) + "</ol>"


NL = chr(10)
_JUDGING_NOTICE_ON = False


def configure_judging_notice(enabled: bool) -> None:
    """Process-wide switch for the shared-demo notice. It adds a request, never a limit."""
    global _JUDGING_NOTICE_ON
    _JUDGING_NOTICE_ON = bool(enabled)

JUDGING_NOTICE = (
    "Shared judging demo. Please start with up to three previews so other reviewers can try it too. "
    "This is a request, not a limit: you can keep going, and every other feature stays available. "
    "Each preview costs 2 provider units from a fixed allowance; when it runs out the page says so "
    "plainly and offers the refund route — it never pretends to be an offline demo."
)


def _judging_banner(enabled: bool) -> str:
    if not enabled:
        return ""
    return (
        '<section class="judging" role="note" aria-label="Shared judging demo">' + NL
        + '<p>' + esc(JUDGING_NOTICE) + '</p>' + NL
        + '</section>' + NL
    )


SAMPLE_PHOTOS = (
    ("female", "/samples/female-sample.png", "Female sample"),
    ("male", "/samples/male-sample.png", "Male sample"),
)


def photo_samples_html() -> str:
    """Optional PNG samples a judge may download and then upload through the normal control.

    Downloading is a plain GET: no consent, no session mutation and no provider task is created.
    """
    links = "".join(
        f'<li><a href="{esc(href)}" download="{esc(href.rsplit("/", 1)[-1])}" '
        f'type="image/png">{esc(label)}</a> (PNG, {esc(href.rsplit("/", 1)[-1])})</li>'
        for _key, href, label in SAMPLE_PHOTOS
    )
    return (
        '<div class="samples">'
        '<p><strong>Need a photo to try?</strong> These are synthetic adult sample images, not '
        'customers. Downloading one needs no consent and does nothing else: it creates no case, '
        'no preview and no provider task, and spends no unit. To use it, upload it with the control '
        'above exactly as you would your own photo; the image-consent step applies to every upload, '
        'yours or a sample, before any preview exists. Pressing Generate is what sends the image to '
        'the try-on provider and spends the units. PNG only, at least 512x384 and under 10 MB; a JPEG '
        'is refused.</p>'
        f"<ul>{links}</ul></div>"
    )


def layout(
    *,
    title: str,
    body: str,
    status_html: str = "",
    session_id: str | None = None,
    mode: str = "MOCK",
    refresh_seconds: int | None = None,
    step: int | None = None,
    judging_notice: bool | None = None,
) -> str:
    # A four-step strip, so the shopper always knows where they are in the rescue.
    progress = steps_html(step)
    # The pending page asks the browser to re-request itself: one loop per page, no script,
    # no background thread, and it stops the moment the page is no longer a pending one.
    refresh = (f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">'
               if refresh_seconds else "")
    nav_extra = ""
    if session_id:
        nav_extra = (
            f'<li><a href="/s/{esc(session_id)}">This return</a></li>'
        )
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{esc(title)} — SwapSure</title>\n"
        f"{refresh}"
        f"<style>{_STYLE}</style>\n</head>\n<body>\n"
        '<a class="skip" href="#main">Skip to main content</a>\n'
        '<header class="site" role="banner">\n'
        '<p class="brand">SwapSure — Visual Return Rescue</p>\n'
        '<nav class="site" aria-label="Primary">\n<ul>'
        '<li><a href="/">Start a return</a></li>'
        f"{nav_extra}"
        f'<li><a class="refund" href="{REFUND_LINK}">Continue refund (host returns)</a></li>'
        "</ul>\n</nav>\n</header>\n"
        '<main id="main" tabindex="-1">\n' + progress + '\n\n'
        f"{_mode_panel(mode)}\n"
        f"{_judging_banner(_JUDGING_NOTICE_ON if judging_notice is None else judging_notice)}"
        f"{_collection_strip(mode)}\n"
        f"{status_html}\n"
        f"{body}\n"
        "</main>\n"
        '<footer class="site" role="contentinfo">\n'
        + (
            "<p>LIVE build. Real YouCam provider tasks are created and provider units are spent; no "
            "real merchant, payment or exchange transaction occurs. The merchant return portal is "
            "always reachable at "
            if mode == "LIVE" else
            "<p>Offline build. Simulated exchange only; no real merchant, payment or provider "
            "transaction occurs. The merchant return portal is always reachable at "
        ) +
        f'<a href="{REFUND_LINK}">{REFUND_LINK}</a>.</p>\n'
        '<p><a href="/ledger">Local outcome ledger</a></p>\n'
        "</footer>\n</body>\n</html>\n"
    )


def status_region(message: str, *, live: str = "polite") -> str:
    return f'<div class="status" role="status" aria-live="{esc(live)}">{esc(message)}</div>'


# --------------------------------------------------------------------------- #
# Home / start a return
# --------------------------------------------------------------------------- #
def home_page(
    *,
    order: Mapping[str, Any],
    reason_options: Sequence[tuple[str, str]],
    error: str | None = None,
    upload_handle: str | None = None,
    upload_summary: Mapping[str, Any] | None = None,
    mode: str = "MOCK",
) -> str:
    groups: dict[str, list[tuple[str, str]]] = {"Visual reasons (may be eligible)": [], "Other reasons (ordinary return)": []}
    for code, status in reason_options:
        key = "Visual reasons (may be eligible)" if status == "ELIGIBLE_VISUAL_RESCUE" else "Other reasons (ordinary return)"
        groups[key].append((code, status))

    fieldsets = []
    for legend, options in groups.items():
        items = "".join(
            f'<label class="inline"><input type="checkbox" name="reason" value="{esc(code)}">'
            f"<span><code>{esc(code)}</code></span></label>"
            for code, _status in options
        )
        fieldsets.append(
            f"<fieldset><legend>{esc(legend)}</legend>{items}</fieldset>"
        )
    fieldsets.append(
        '<fieldset><legend>Not listed</legend>'
        '<label class="inline"><input type="checkbox" name="reason" value="OTHER">'
        "<span>My reason is not listed (ordinary return route)</span></label></fieldset>"
    )

    upload_block = ""
    if upload_handle and upload_summary:
        upload_block = (
            '<div class="card"><h2>Using your uploaded scenario</h2>'
            "<dl class=\"facts\">"
            f"<dt>File</dt><dd>{esc(upload_summary.get('filename'))}</dd>"
            f"<dt>Bytes</dt><dd>{esc(upload_summary.get('byteSize'))}</dd>"
            f"<dt>SHA-256</dt><dd><code>{esc(upload_summary.get('sha256'))}</code></dd>"
            "</dl>"
            f'<input type="hidden" name="uploadHandle" value="{esc(upload_handle)}">'
            '<p class="muted">Your file supplies the order and catalog for this session. '
            "It is stored outside any public directory and is not re-served.</p></div>"
        )
    error_block = (
        f'<div class="error" role="alert"><p><strong>{esc(error)}</strong></p></div>'
        if error
        else ""
    )
    body = (
        "<h1>Start a return</h1>"
        f"{error_block}"
        '<div class="card"><h2>Order being returned</h2>'
        '<dl class="facts">'
        f"<dt>Order</dt><dd>{esc(order.get('orderId'))}</dd>"
        f"<dt>Merchant</dt><dd>{esc(order.get('merchant'))}</dd>"
        f"<dt>Item</dt><dd>{esc(order.get('originalSku'))} — {esc(order.get('category'))}, "
        f"size {esc(order.get('size'))}, {esc(order.get('colorFamily'))}</dd>"
        f"<dt>Value</dt><dd>{esc(money(order.get('originalValueMinor', 0), str(order.get('currency', 'USD'))))}</dd>"
        "</dl></div>"
        '<div class="card"><h2>Use your own scenario file (optional)</h2>'
        "<p>You can upload a UTF-8 JSON file with your own order, catalog and reasons. "
        "It replaces the synthetic defaults for your session; the exact bytes are hashed "
        "and stored outside any public directory.</p>"
        '<form method="post" action="/uploads" enctype="multipart/form-data">'
        '<label for="scenario">Scenario JSON (max 64 KiB)</label>'
        '<input id="scenario" name="scenario" type="file" accept="application/json,.json">'
        ' <button type="submit">Validate and preview file</button>'
        "</form></div>"
        f'<form method="post" action="/returns" novalidate>'
        f"{upload_block}"
        "<fieldset><legend>Why are you returning it?</legend>"
        "<p>Choose one or more structured reasons. If nothing matches, use “Not listed” "
        "and we will take you to the ordinary return route.</p>"
        f"{''.join(fieldsets)}"
        '<label class="inline"><input type="checkbox" name="immediateRefund" value="true">'
        "<span>I want a refund now and do not want alternatives.</span></label>"
        "</fieldset>"
        "<fieldset><legend>Notes (optional)</legend>"
        '<label for="notes">Anything you want to add</label>'
        '<textarea id="notes" name="notes" rows="3" cols="50" '
        'aria-describedby="notes-help"></textarea>'
        '<p id="notes-help" class="muted">Notes are shown back to you as text only. '
        "They never decide eligibility and are never sent to an image service.</p>"
        "</fieldset>"
        '<button class="primary" type="submit">Continue</button>'
        "</form>"
        '<p class="muted">Need to stop now? '
        f'<a href="{REFUND_LINK}">Go to the merchant return portal</a> at any time.</p>'
    )
    return layout(title="Start a return", body=body, mode=mode)


# --------------------------------------------------------------------------- #
# Eligibility decision
# --------------------------------------------------------------------------- #
def decision_page(view: CaseView) -> str:
    decision = view.decision
    assert decision is not None
    status_html = status_region(
        f"Eligibility result: {decision.status}. Refund route available."
    )
    facts = (
        '<div class="card"><h2>Eligibility decision</h2>'
        '<dl class="facts">'
        f"<dt>Status</dt><dd><code>{esc(decision.status)}</code></dd>"
        f"<dt>Eligible for visual rescue</dt><dd>{'yes' if decision.eligible else 'no'}</dd>"
        f"<dt>Matched reasons</dt><dd>{esc(', '.join(decision.matched_reasons) or 'none')}</dd>"
        f"<dt>Explanation code</dt><dd><code>{esc(decision.explanation_code)}</code></dd>"
        f"<dt>Policy version</dt><dd>{esc(decision.policy_version)}</dd>"
        f"<dt>Refund available</dt><dd>{'yes' if decision.refund_available else 'no'}</dd>"
        "</dl>"
        f"<p>{esc(_explanation_text(decision))}</p>"
        "</div>"
    )
    notes = (
        f'<div class="card"><h2>Your note</h2><p>{esc(view.case.notes)}</p>'
        '<p class="muted">Displayed as plain text. It did not affect the decision above.</p></div>'
        if view.case.notes
        else ""
    )
    token = esc(view.case.request_token)
    sid = esc(view.session.session_id)
    if decision.eligible:
        action = (
            '<div class="card"><h2>Optional: see alternatives</h2>'
            "<p>You can look at in-stock alternatives in your size before you decide. "
            "Looking is optional, and choosing an alternative is not an approval.</p>"
            f'<form method="post" action="/s/{sid}/opt-in">'
            f'<input type="hidden" name="request_token" value="{token}">'
            '<button class="primary" type="submit">Show me alternatives</button></form>'
            '<p class="muted">Prefer the refund? '
            f'<a href="{REFUND_LINK}">Continue to the merchant return portal</a>.</p>'
            "</div>"
        )
    else:
        action = (
            '<div class="card"><h2>Ordinary return route</h2>'
            "<p>This reason follows the merchant's ordinary return process. "
            "No visual preview is offered for it.</p>"
            f'<form method="post" action="/s/{sid}/refund">'
            f'<input type="hidden" name="request_token" value="{token}">'
            '<button class="primary" type="submit">Continue refund</button></form>'
            "</div>"
        )
    body = f"<h1>Return decision</h1>{facts}{action}{notes}{_refund_strip(view)}"
    return layout(title="Return decision", body=body, status_html=status_html, session_id=view.session.session_id)


def _explanation_text(decision: EligibilityDecision) -> str:
    table = {
        "REFUND_REQUESTED": "You asked for a refund, so the ordinary return route applies.",
        "UNKNOWN_REQUIRES_REFUND_PATH": (
            "We could not match a recognised structured reason, so the ordinary return route applies."
        ),
        "INELIGIBLE_DEFECT": "A defect is handled by the merchant's ordinary remedy process.",
        "INELIGIBLE_FULFILLMENT_ERROR": "A fulfilment problem is handled by the ordinary return process.",
        "INELIGIBLE_FIT": (
            "Fit and sizing cannot be judged from a visual preview, so no preview is offered."
        ),
        "INELIGIBLE_MATERIAL": "Material feel cannot be judged from a visual preview, so no preview is offered.",
        "ELIGIBLE_VISUAL_RESCUE": (
            "This colour/style/occasion reason may benefit from seeing an alternative in stock. "
            "It is optional and you can still choose the refund."
        ),
    }
    return table.get(decision.status, "The ordinary return route applies.")


def _refund_strip(view: CaseView) -> str:
    if view.session.state in ("REFUND_HANDOFF", "EXCHANGE_COMMITTED"):
        return ""
    return (
        '<div class="card"><h2>Refund</h2>'
        "<p>The ordinary return route does not depend on this page, the previews or the "
        "rescue workflow.</p>"
        f'<p><a href="{REFUND_LINK}">Open the merchant return portal</a></p>'
        f'<form method="post" action="/s/{esc(view.session.session_id)}/refund">'
        f'<input type="hidden" name="request_token" value="{esc(view.case.request_token)}">'
        '<button type="submit">Continue refund now</button></form></div>'
    )


# --------------------------------------------------------------------------- #
# Consent
# --------------------------------------------------------------------------- #
def consent_page(view: CaseView, *, mode: str = "MOCK") -> str:
    sid = esc(view.session.session_id)
    token = esc(view.case.request_token)
    if mode == "LIVE":
        # The grant is obtained BEFORE any upload, so it must describe the real processing and the
        # retention position that is still unconfirmed, not a MOCK limitation notice.
        covered = (
            "<li><strong>Your photo and the chosen garment reference leave this server.</strong> "
            "They are sent to YouCam (Perfect Corp) so it can generate virtual try-on previews for "
            "this return.</li>"
            "<li><strong>Processing is remote.</strong> The provider runs its own models on the "
            "images; this site cannot control or audit what happens inside that service.</li>"
            "<li><strong>Provider retention is not settled.</strong> Perfect Corp's developer "
            "documentation says media and file/task identifiers are retained for up to 30 days "
            "while result links expire after two hours; a Perfect Corp marketing page says uploaded "
            "pictures are deleted within 24 hours. Those statements conflict. No deletion promise "
            "is made for the provider.</li>"
            "<li><strong>Deleting here removes only this server's copy</strong> of your photo and "
            "the preview images it cached. It does not delete anything already sent to the "
            "provider, and local deletion is not provider erasure.</li>"
            "<li>Previews are visual only. They are not proof of fit, sizing or product accuracy.</li>"
            "<li>Consent is optional and can be revoked at any time. Refusing or revoking keeps the "
            "ordinary return and refund route available.</li>"
        )
        acknowledge = ("I understand my photo is processed by YouCam and that the provider's "
                       "retention of it is not settled or guaranteed to me.")
    else:
        covered = (
            "<li>This offline build generates a local placeholder card. It does not process a photo of you.</li>"
            "<li>A future approved live build would send a garment reference and your own image to the "
            "approved YouCam processing service; this build has no live transport and makes no calls.</li>"
            "<li>Placeholders are kept locally for at most 30 minutes, or until you delete them, "
            "whichever comes first.</li>"
            "<li>Deleting local media does not and cannot confirm deletion by a provider; this build has no provider.</li>"
            "<li>Consent is optional. Refusing keeps the ordinary return route available.</li>"
        )
        acknowledge = "I understand every preview here is a MOCK placeholder, not a YouCam result."
    body = (
        "<h1>Consent for visual previews</h1>"
        '<div class="card"><h2>What this step covers</h2>'
        "<ul class=\"plain\">"
        + covered +
        "</ul></div>"
        f'<form method="post" action="/s/{sid}/consent">'
        f'<input type="hidden" name="request_token" value="{token}">'
        "<fieldset><legend>Two separate choices</legend>"
        '<label class="inline"><input type="checkbox" name="imageConsent" value="true">'
        "<span>I consent to image processing for visual previews in this session.</span></label>"
        '<label class="inline"><input type="checkbox" name="mockAcknowledged" value="true">'
        f"<span>{esc(acknowledge)}</span></label>"
        "</fieldset>"
        '<button class="primary" type="submit">Record consent and continue</button>'
        "</form>"
        f'<form method="post" action="/s/{sid}/withdraw">'
        f'<input type="hidden" name="request_token" value="{token}">'
        '<button class="danger" type="submit">Withdraw consent and go to the refund route</button>'
        "</form>"
        f'<p><a href="{REFUND_LINK}">Continue refund without consent</a></p>'
    )
    return layout(
        title="Consent",
        body=body,
        status_html=status_region("Consent has not been recorded yet."),
        session_id=view.session.session_id,
        mode=mode,
    )


# --------------------------------------------------------------------------- #
# Candidates
# --------------------------------------------------------------------------- #
def candidates_page(view: CaseView, *, mode: str = "MOCK",
                    preselect: str | None = None,
                    chosen: str | None = None,
                    collection: str | None = None) -> str:
    selection = view.selection
    assert selection is not None
    sid = esc(view.session.session_id)
    token = esc(view.case.request_token)
    # A garment this photo already produced is not offered again: it would be refused at plan time.
    spent = set(view.session.generated)
    photo = view.session.photo_version
    offered = tuple(
        candidate for candidate in selection.candidates
        if not (photo and f"{photo}:{candidate.sku}" in spent)
    )
    if selection.has_candidates:
        cards = "".join(_candidate_card(candidate, sid, token) for candidate in offered)
    else:
        cards = (
            '<div class="card"><p>No in-stock alternative meets every constraint right now. '
            "Constraints are never relaxed to fill the list.</p>"
            f'<form method="post" action="/s/{sid}/refund">'
            f'<input type="hidden" name="request_token" value="{token}">'
            '<button class="primary" type="submit">Continue refund</button></form></div>'
        )
    excluded = _exclusions_table(selection)
    truncated = (
        "<p class=\"muted\">Further valid alternatives exist beyond the three-item list: "
        + esc(", ".join(selection.truncated_skus))
        + ".</p>"
        if selection.truncated_skus
        else ""
    )
    prefs = (
        "<div class=\"card\"><h2>Size and preferences</h2>"
        f'<form method="post" action="/s/{sid}/candidates">'
        f'<input type="hidden" name="request_token" value="{token}">'
        f'<label for="size">Your size</label>'
        f'<input id="size" name="size" value="{esc(view.case.size)}" size="6" required>'
        '<label for="preferColor">Preferred colour family (optional)</label>'
        f'<input id="preferColor" name="preferColor" value="{esc(_pref(view, "colorFamily"))}" size="16">'
        '<label for="preferStyle">Preferred style (optional)</label>'
        f'<input id="preferStyle" name="preferStyle" value="{esc(_pref(view, "style"))}" size="16">'
        '<label for="preferOccasion">Preferred occasion (optional)</label>'
        f'<input id="preferOccasion" name="preferOccasion" value="{esc(_pref(view, "occasion"))}" size="16">'
        '<p class="muted">Preferences order the alternatives that already pass every hard '
        "constraint. They never relax a constraint.</p>"
        '<button type="submit">Update alternatives</button></form></div>'
    )
    photo_present = bool(getattr(view, "photo_present", False))
    photo_card = (
        '<div class="card"><h2>Step 1 — your photo</h2>'
        + ("<p><strong>Photo received.</strong> It is stored on this server for this return only.</p>"
           if photo_present else
           "<p><strong>A photo is required.</strong> Previews cannot be generated until one is sent.</p>")
        + f'<form method="post" action="/s/{sid}/photo" enctype="multipart/form-data">'
        + f'<input type="hidden" name="request_token" value="{token}">'
        + '<label for="photo">Photo (PNG, 512x384 minimum, under 10 MB)</label>'
        + '<input id="photo" name="photo" type="file" accept="image/png" required>'
        + '<button type="submit">' + ("Replace photo" if photo_present else "Send photo for try-on")
        + "</button></form>"
        + photo_samples_html()
        + f'<p><a href="/s/{sid}/consent-panel">Photo and consent controls</a></p></div>'
    )
    chosen_row = next((c for c in selection.candidates if c.sku == chosen), None) if chosen else None
    catalogue_link = ("/catalog/" + esc(collection)) if collection else "/catalog"
    if mode == "LIVE":
        cost_line = ("Generating sends your photo and this garment's reference to Perfect Corp and "
                     "spends <strong>2 provider units</strong>.")
        label = "Generate preview — 2 units"
    else:
        cost_line = "Generating creates local placeholder previews. Zero provider units."
        label = "Generate preview (mock, free)"
    start = ""
    if not offered and selection.has_candidates:
        start = (
            '<div class="card"><h2>Every eligible garment has been tried</h2>'
            "<p>This photo has already produced a preview for each alternative this return can "
            "offer. Choose a different photo, pick one of the results above for a quote, or "
            "continue the return.</p>"
            f'<form method="post" action="/s/{sid}/change-photo">'
            f'<input type="hidden" name="request_token" value="{token}">'
            '<button type="submit">Change photo</button></form></div>'
        )
    elif chosen_row is not None:
        start = (
            '<div class="card"><h2>Your selected garment</h2>'
            '<div class="garment" style="max-width:280px">'
            f'<img src="{catalogue_link}/thumb/{esc(chosen_row.sku)}" alt="Selected garment '
            f'{esc(chosen_row.sku)}" width="240" height="360" loading="eager">'
            f'<h3>{esc(chosen_row.sku)}</h3>'
            f'<p class="price">{esc(money(chosen_row.price_minor, chosen_row.currency))} · '
            f"size {esc(selection.size)}</p></div>"
            f"<p>{cost_line}</p>"
            f'<form method="post" action="/s/{sid}/preview">'
            f'<input type="hidden" name="request_token" value="{token}">'
            f'<input type="hidden" name="sku" value="{esc(chosen_row.sku)}">'
            f'<button class="primary" type="submit"{" disabled" if not photo_present else ""}>'
            f"{esc(label)}</button></form>"
            f'<form method="post" action="/s/{sid}/change-garment">'
            f'<input type="hidden" name="request_token" value="{token}">'
            '<button type="submit">Change garment</button></form>'
            + ("" if photo_present else
               '<p class="muted">Send a photo above to enable generation.</p>')
            + "</div>"
        )
    elif chosen is not None:
        start = (
            '<div class="card"><h2>That garment is no longer available</h2>'
            f"<p>The garment you chose (<code>{esc(chosen)}</code>) is out of stock, no longer in "
            "this size, or no longer eligible for this return. Nothing was substituted for it.</p>"
            f'<form method="post" action="/s/{sid}/change-garment">'
            f'<input type="hidden" name="request_token" value="{token}">'
            '<button class="primary" type="submit">Choose another garment</button></form></div>'
        )
    elif offered:
        cards = "".join(
            '<article class="garment">'
            f'<img src="{catalogue_link}/thumb/{esc(c.sku)}" alt="{esc(c.sku)}" width="240" '
            'height="360" loading="eager">'
            f"<h3>{esc(c.sku)}</h3>"
            f'<p class="price">{esc(money(c.price_minor, c.currency))}</p>'
            f'<form method="post" action="/s/{sid}/choose-garment">'
            f'<input type="hidden" name="request_token" value="{token}">'
            f'<input type="hidden" name="sku" value="{esc(c.sku)}">'
            '<button type="submit">Choose this garment</button></form></article>'
            for c in offered
        )
        start = (
            '<div class="card"><h2>Choose a garment</h2>'
            "<p>In stock, in your size, and eligible for this return. Your photo stays with this "
            "session.</p>"
            f'<div class="grid">{cards}</div></div>'
        )
    body = (
        f"{photo_card}"
        "<h1>In-stock alternatives</h1>"
        f'<p class="muted">Stock check: version <code>{esc(selection.inventory_version)}</code>, '
        f"age {esc(round(selection.snapshot_age_seconds, 1))}s, "
        f"{esc(selection.considered_count)} rows considered.</p>"
        f"{prefs}{cards}{truncated}"
        f'<div class="card"><h2>Excluded rows</h2>{excluded}</div>'
        f"{start}{_refund_strip(view)}"
    )
    return layout(step=2, 
        title="Alternatives",
        body=body,
        status_html=status_region(
            f"{len(selection.candidates)} valid alternative(s) listed. Selection is not approval."
        ),
        session_id=view.session.session_id,
        mode=mode,
    )


def _pref(view: CaseView, name: str) -> str:
    values = view.case.preferences.get("preferred", {}).get(name) or []
    return values[0] if values else ""


def _candidate_card(candidate: Any, sid: str, token: str) -> str:
    return (
        '<article class="card" aria-labelledby="sku-'
        + esc(candidate.sku)
        + '"><h2 id="sku-'
        + esc(candidate.sku)
        + '">'
        + esc(candidate.sku)
        + "</h2><dl class=\"facts\">"
        f"<dt>Size</dt><dd>{esc(candidate.size)}</dd>"
        f"<dt>Colour family</dt><dd>{esc(candidate.color_family)}</dd>"
        f"<dt>Price</dt><dd>{esc(money(candidate.price_minor, candidate.currency))}</dd>"
        f"<dt>Difference</dt><dd>{esc(money(candidate.abs_delta_minor, candidate.currency))}</dd>"
        f"<dt>Preference matches</dt><dd>{esc(candidate.preference_matches)}</dd>"
        "</dl>"
        f'<form method="post" action="/s/{sid}/choose">'
        f'<input type="hidden" name="request_token" value="{token}">'
        f'<input type="hidden" name="sku" value="{esc(candidate.sku)}">'
        '<button type="submit">Select for a quote (not an approval)</button></form>'
        "</article>"
    )


def _exclusions_table(selection: CandidateSelection) -> str:
    if not selection.excluded:
        return '<p class="muted">No rows were excluded in this pass.</p>'
    rows = "".join(
        "<tr><td>"
        + esc(item.sku or "(no SKU)")
        + "</td><td>"
        + esc(", ".join(item.reasons))
        + "</td><td>"
        + esc(", ".join(item.detail) if item.detail else "")
        + "</td></tr>"
        for item in selection.excluded
    )
    return (
        "<table><caption>Every excluded row and the hard constraint it failed</caption>"
        "<thead><tr><th scope=\"col\">SKU</th><th scope=\"col\">Reasons</th>"
        f"<th scope=\"col\">Detail</th></tr></thead><tbody>{rows}</tbody></table>"
    )


# --------------------------------------------------------------------------- #
# Previews
# --------------------------------------------------------------------------- #
def preview_pending_page(view: CaseView) -> str:
    job = view.job or {}
    progress = job.get("progress") or _job_progress(job)
    live = str(progress.get("mode", "MOCK")) == "LIVE"
    deadline_text = (str(progress.get("deadline") or job.get("planDeadline") or job.get("deadline")
                        or "not recorded")
                     .replace("T", " ").replace("+00:00", " UTC"))
    sid = esc(view.session.session_id)
    body = (
        ("<h1>Generating previews with the provider</h1>" if live
         else "<h1>Generating mock previews</h1>")
        + '<div class="card"><h2>Bounded work in progress</h2>'
        "<dl class=\"facts\">"
        f"<dt>Mode</dt><dd>{esc(progress.get('mode', 'MOCK'))}</dd>"
        f"<dt>Provider units</dt><dd>{esc(progress.get('units', 0))}</dd>"
        f"<dt>Started / candidates</dt><dd>{esc(progress.get('started', 0))} / "
        f"{esc(progress.get('candidates', 0))}</dd>"
        f"<dt>Completed / failed</dt><dd>{esc(progress.get('completed', 0))} / "
        f"{esc(progress.get('failed', 0))}</dd>"
        f"<dt>Concurrency limit</dt><dd>{esc(progress.get('concurrency', 2))}</dd>"
        "</dl>"
        f'<p><a href="/s/{sid}">Check preview status</a></p>'
        f"<p class=\"muted\">The wait deadline for this plan is {esc(deadline_text)}. A preview "
        "that has not answered by then is kept for reconciliation rather than called a failure; "
        "the alternatives and the refund remain available.</p></div>"
        f"{_refund_strip(view)}"
    )
    return layout(step=3, 
        mode="LIVE" if live else "MOCK",
        title="Previews pending",
        body=body,
        status_html=status_region(
            f"Provider previews are still being generated. This page refreshes every "
            f"{POLL_SECONDS} seconds until the provider answers." if live
            else "Mock previews are still being prepared.", live="polite"),
        session_id=view.session.session_id,
        refresh_seconds=POLL_SECONDS,
    )


def _job_progress(job: Mapping[str, Any]) -> dict[str, Any]:
    tasks = job.get("tasks", {})
    return {
        "candidates": len(job.get("candidates", [])),
        "started": len(tasks),
        "completed": sum(1 for task in tasks.values() if task.get("state") == "DONE"),
        "failed": sum(1 for task in tasks.values() if task.get("state") == "FAILED"),
        "concurrency": 2,
        "units": 0,
        "mode": job.get("mode", "MOCK"),
    }


# Bounded self-refresh cadence for the pending page. One request per page per interval; the
# server does the polling, so extra tabs cost extra queries but never extra tasks.
POLL_SECONDS = 5

_NO_IMAGE_REASONS = {
    "BUDGET_EXHAUSTED": "not attempted, because the live allowance for this session is already spent",
    "RESERVATION_EXHAUSTED": "not attempted, because the live allowance is already spent",
    "CONSENT_REQUIRED": "blocked, because consent is missing or revoked",
    "CONSENT_EXPIRED": "blocked, because consent expired",
    "DEADLINE_EXCEEDED": "stopped, because the provider deadline passed",
    "TIMEOUT": "stopped, because the provider did not answer in time",
    "NO_RESULT": "the provider returned no preview",
    "INVALID_RESULT": "the provider returned a result this product cannot decode or display",
    "RECONCILIATION_PENDING": ("the provider has not answered yet, so this is not a failure — "
                              "the task is kept and can be checked again later"),
    "RECOVERY_REQUIRED": ("the provider task exists but its id could not be stored, so it needs "
                          "a manual check — do not retry generation"),
    "NOT_STARTED": "not started, so no provider units were spent on it",
}


def _no_image_reason(code: object) -> str:
    """Say why there is no image, without blaming the provider for our own refusals."""
    return _NO_IMAGE_REASONS.get(str(code or ""), "the provider returned none for it")


def decision_ready_page(view: CaseView, *, now=None) -> str:
    session = view.session
    result = session.preview_result
    selection = view.selection
    assert selection is not None
    sid = esc(view.session.session_id)
    token = esc(view.case.request_token)
    status = result.status if result is not None else "UNKNOWN"
    # A result that has passed its validity is history, not a picture this server will serve.
    expires_at = getattr(result, "expires_at", None)
    expired = bool(
        result is not None
        and result.mode == "LIVE"
        and now is not None
        and expires_at is not None
        and now >= expires_at
    )
    expired_at = expires_at.isoformat().replace("+00:00", "Z") if expired else ""
    live_urls = {}
    if result is not None and result.mode == "LIVE":
        live_urls = {cid: url for cid, url in zip(result.candidate_ids, result.media_handles) if url}
    cards = []
    for candidate in selection.candidates:
        media = view.media_for(candidate.sku)
        live_url = live_urls.get(candidate.sku)
        if expired and live_url:
            figure = (
                '<p class="muted"><strong>Preview expired.</strong> The provider image for this '
                "alternative is no longer available from this server. The generation itself "
                "succeeded and its units were spent; nothing is regenerated or refunded "
                "automatically.</p>"
            )
        elif live_url:
            figure = (
                f'<figure><img src="/s/{sid}/preview/{esc(candidate.sku)}" width="240" '
                f'height="320" alt="YouCam virtual try-on for SKU {esc(candidate.sku)}. '
                f'Visual preview only.">'
                f'<figcaption><span class="badge">YOUCAM RESULT</span>'
                f"<p>Returned by the provider for your photo and shown from this origin. "
                f"{esc(DISCLAIMER)}</p></figcaption></figure>"
            )
        elif result is not None and result.mode == "LIVE":
            # A live result never falls back to a locally drawn card: no provider image means no
            # image, and the page says so.
            reason = _no_image_reason(getattr(result, "failure_code", None))
            figure = f'<p class="muted">No image for this alternative — {esc(reason)}.</p>'
        else:
            figure = (
                f'<figure><img src="/media/{esc(media.handle)}" width="240" height="320" '
                f'alt="Mock placeholder card for SKU {esc(candidate.sku)}. Not a YouCam result.">'
                f"<figcaption><span class=\"badge\">{esc(MODE_LABEL)}</span>"
                f"<p>{esc(DISCLAIMER)}</p></figcaption></figure>"
                if media is not None
                else '<p class="muted">Preview unavailable for this alternative — no image is shown.</p>'
            )
        cards.append(
            '<article class="card" aria-labelledby="cand-'
            + esc(candidate.sku)
            + '"><h2 id="cand-'
            + esc(candidate.sku)
            + '">'
            + esc(candidate.sku)
            + "</h2>"
            + figure
            + "<dl class=\"facts\">"
            f"<dt>Price</dt><dd>{esc(money(candidate.price_minor, candidate.currency))}</dd>"
            f"<dt>Difference</dt><dd>{esc(money(candidate.abs_delta_minor, candidate.currency))}</dd>"
            "</dl>"
            f'<form method="post" action="/s/{sid}/choose">'
            f'<input type="hidden" name="request_token" value="{token}">'
            f'<input type="hidden" name="sku" value="{esc(candidate.sku)}">'
            '<button type="submit">Select for a quote (not an approval)</button></form>'
            "</article>"
        )
    generated_ok = len(result.succeeded_candidate_ids) if result else 0
    available = 0 if expired else generated_ok
    delivery_line = (
        f"Generated previews: {generated_ok} (all expired at {esc(expired_at)}; "
        f"{available} currently available)"
        if expired
        else f"Delivered previews: {available}"
    )
    if expired:
        mode_detail = (
            "The provider did succeed for this photo, and the units were spent at the time. The "
            "result has since passed its validity, so this page neither shows nor claims a picture "
            "any more. Choosing an alternative again would need a new, separately priced "
            f"generation. {esc(DISCLAIMER)}"
        )
    elif result is not None and result.mode == "LIVE":
        mode_detail = (
            f"Each image above is a result the provider returned for your photo, fetched through "
            f"this site rather than loaded from the provider. Failed alternatives show no image. "
            f"{esc(DISCLAIMER)}"
        )
    else:
        mode_detail = (
            "Every image above is a locally generated placeholder; none is a YouCam result. "
            f"{esc(DISCLAIMER)}"
        )
    mode_note = (
        f'<div class="card"><h2>Preview mode</h2><p><strong>Mode: '
        f'{esc(result.mode if result else "MOCK")}.</strong> Status: {esc(status)}. '
        f"{esc(delivery_line)}. {mode_detail}</p></div>"
    )
    body = (
        "<h1>Your decision</h1>"
        f"{mode_note}"
        '<div class="grid">'
        f"{''.join(cards)}"
        "</div>"
        '<div class="card"><h2>Delete preview images</h2>'
        "<p>You can delete the preview images stored here now. The alternatives and the refund "
        "remain available, and the commerce record is not changed.</p>"
        f'<form method="post" action="/s/{sid}/delete-media">'
        f'<input type="hidden" name="request_token" value="{token}">'
        '<button type="submit">Delete local preview images</button></form>'
        '<p class="muted">Consent withdrawal is separate: '
        f'<a href="/s/{sid}/consent-panel">review consent and revocation</a>.</p></div>'
        '<div class="card"><h2>Try something else on</h2>'
        "<p>The photo and its consent stay valid, so nothing is uploaded again. Every new garment "
        "is a separate preview with its own cost confirmation, and already tried garments are not "
        "offered again for this photo.</p>"
        f'<form method="post" action="/s/{sid}/again">'
        f'<input type="hidden" name="request_token" value="{token}">'
        '<button class="primary" type="submit">Try something else on</button></form>'
        "<p>Changing to a different person starts a separate photo workflow: the current photo, "
        "its consent and any result produced from it are discarded first.</p>"
        f'<form method="post" action="/s/{sid}/change-photo">'
        f'<input type="hidden" name="request_token" value="{token}">'
        '<button type="submit">Change photo</button></form></div>'
        f"{_refund_strip(view)}"
    )
    return layout(step=4, 
        mode=(result.mode if result is not None else "MOCK"),
        title="Your decision",
        body=body,
        status_html=status_region(
            f"Previews ready in {result.mode if result else 'MOCK'} mode. Choosing is not approval."
        ),
        session_id=view.session.session_id,
    )


# --------------------------------------------------------------------------- #
# Quote, approval, receipt
# --------------------------------------------------------------------------- #
def quote_page(view: CaseView, quote: Quote, *, error: str | None = None) -> str:
    sid = esc(view.session.session_id)
    token = esc(view.case.request_token)
    nonce = esc(view.case.approval_nonce)
    error_block = (
        f'<div class="error" role="alert"><p><strong>{esc(error)}</strong></p></div>' if error else ""
    )
    body = (
        "<h1>Confirm the exchange</h1>"
        f"{error_block}"
        '<div class="card"><h2>Exact terms</h2>'
        "<table class=\"amounts\"><caption>Quote "
        + esc(quote.quote_id)
        + " version "
        + esc(quote.quote_version)
        + "</caption><tbody>"
        "<tr><th scope=\"row\">Original item value</th><td class=\"num\">"
        + esc(money(quote.original_value_minor, quote.currency))
        + "</td></tr>"
        "<tr><th scope=\"row\">Replacement item value</th><td class=\"num\">"
        + esc(money(quote.replacement_value_minor, quote.currency))
        + "</td></tr>"
        "<tr><th scope=\"row\">Partial refund to you</th><td class=\"num\">"
        + esc(money(quote.partial_refund_minor, quote.currency))
        + "</td></tr>"
        "<tr><th scope=\"row\">Additional charge</th><td class=\"num\">"
        + esc(money(quote.additional_charge_minor, quote.currency))
        + "</td></tr>"
        "<tr><th scope=\"row\">Tax / shipping adjustment</th><td class=\"num\">"
        + esc(money(quote.tax_shipping_adjustment_minor, quote.currency))
        + "</td></tr>"
        "</tbody></table>"
        "<dl class=\"facts\">"
        f"<dt>Replacement SKU</dt><dd>{esc(quote.candidate_variant_id)}</dd>"
        f"<dt>Stock version</dt><dd><code>{esc(quote.inventory_version)}</code></dd>"
        f"<dt>Quote expires</dt><dd>{esc(quote.expires_at.isoformat())}</dd>"
        f"<dt>Seconds remaining</dt><dd>{esc(max(round(view.quote_remaining or 0, 1), 0))}</dd>"
        "</dl>"
        "<p>Selecting an alternative was not approval. The exchange happens only when you "
        "press the approval button below, and only for these exact amounts.</p>"
        "</div>"
        f'<form method="post" action="/s/{sid}/approve">'
        f'<input type="hidden" name="request_token" value="{token}">'
        f'<input type="hidden" name="approval_nonce" value="{nonce}">'
        f'<button class="primary" type="submit">Approve exchange: refund '
        f"{esc(money(quote.partial_refund_minor, quote.currency))} and exchange for "
        f"{esc(quote.candidate_variant_id)}</button>"
        "</form>"
        f'<form method="post" action="/s/{sid}/requote">'
        f'<input type="hidden" name="request_token" value="{token}">'
        '<button type="submit">Get a fresh quote</button></form>'
        f"{_refund_strip(view)}"
    )
    return layout(
        title="Confirm exchange",
        body=body,
        status_html=status_region("Quote ready. Nothing is committed until you approve it."),
        session_id=view.session.session_id,
    )


def receipt_page(view: CaseView, receipt: Mapping[str, Any]) -> str:
    body = (
        "<h1>Rescue Receipt</h1>"
        f'<div class="status" role="status"><p><strong>{esc(receipt.get("modeLabel"))}</strong> — '
        "simulated exchange; no real merchant transaction occurred.</p></div>"
        '<div class="card"><h2>Recorded outcome</h2><dl class="facts">'
        f"<dt>Commit</dt><dd><code>{esc(receipt.get('commitId'))}</code></dd>"
        f"<dt>Original SKU</dt><dd>{esc(receipt.get('originalSku'))}</dd>"
        f"<dt>Replacement SKU</dt><dd>{esc(receipt.get('replacementSku'))}</dd>"
        f"<dt>Original value</dt><dd>{esc(money(receipt.get('originalValueMinor', 0), receipt.get('currency', 'USD')))}</dd>"
        f"<dt>Replacement value</dt><dd>{esc(money(receipt.get('replacementValueMinor', 0), receipt.get('currency', 'USD')))}</dd>"
        f"<dt>Retained original GMV</dt><dd>{esc(money(receipt.get('retainedGmvMinor', 0), receipt.get('currency', 'USD')))}</dd>"
        f"<dt>Partial refund</dt><dd>{esc(money(receipt.get('partialRefundMinor', 0), receipt.get('currency', 'USD')))}</dd>"
        f"<dt>Additional charge</dt><dd>{esc(money(receipt.get('additionalChargeMinor', 0), receipt.get('currency', 'USD')))}</dd>"
        f"<dt>Eligibility</dt><dd><code>{esc(receipt.get('eligibilityStatus'))}</code> / "
        f"<code>{esc(receipt.get('eligibilityRationale'))}</code></dd>"
        f"<dt>Policy version</dt><dd>{esc(receipt.get('policyVersion'))}</dd>"
        f"<dt>Stock check</dt><dd>{esc(receipt.get('stockCheckedAt'))} at "
        f"<code>{esc(receipt.get('stockCheckInventoryVersion'))}</code></dd>"
        f"<dt>Candidates / previews</dt><dd>{esc(receipt.get('candidatesEligible'))} / "
        f"{esc(receipt.get('previewsDelivered'))} ({esc(receipt.get('previewMode'))})</dd>"
        f"<dt>Approval</dt><dd>{esc(receipt.get('approval', {}).get('approvedAt'))} via "
        f"<code>{esc(receipt.get('approval', {}).get('origin'))}</code></dd>"
        "</dl>"
        f'<p><a href="{esc(receipt.get("returnSupportRef"))}">Ordinary return / support route</a></p>'
        "</div>"
        '<div class="card"><h2>Images</h2>'
        "<p>The receipt stores no image bytes, handles or provider URLs. Local placeholders "
        "can still be deleted, separately from this financial record.</p>"
        f'<form method="post" action="/s/{esc(view.session.session_id)}/delete-media">'
        f'<input type="hidden" name="request_token" value="{esc(view.case.request_token)}">'
        '<button type="submit">Delete local preview images</button></form></div>'
        f'<p><a href="{REFUND_LINK}">Open the merchant return portal</a> for anything further.</p>'
    )
    return layout(title="Rescue Receipt", body=body, session_id=view.session.session_id)


# --------------------------------------------------------------------------- #
# Refund routes
# --------------------------------------------------------------------------- #
def refund_page(view: CaseView, outcome: Mapping[str, Any] | None = None) -> str:
    present = [item for item in view.media if item.local_status == "PRESENT"]
    deleted = [item for item in view.media if item.local_status == "DELETED"]
    pending = [item for item in view.media if item.local_status == "PENDING_RETRY"]
    body = (
        "<h1>Refund route</h1>"
        '<div class="card"><h2>Rescue is closed</h2>'
        "<p>The rescue workflow is no longer processing this return. The merchant's own "
        "return portal owns the refund from here.</p>"
        f'<p><a class="refund" href="{REFUND_LINK}">Open the merchant return portal</a></p>'
        "</div>"
        '<div class="card"><h2>Preview images</h2>'
        "<p>Any preview work was cancelled and its local placeholders were deleted.</p>"
        f"<p>Local deletion: <strong>{len(deleted)}</strong> deleted, "
        f"{len(pending)} pending retry, {len(present)} still present. "
        "Provider deletion: not applicable — this build has no provider.</p></div>"
    )
    return layout(title="Refund route", body=body, session_id=view.session.session_id)


def inflight_page(view: CaseView) -> str:
    sid = esc(view.session.session_id)
    body = (
        "<h1>Exchange result pending</h1>"
        '<div class="status" role="status"><p>This exchange has an in-doubt outcome. '
        "Reconciliation uses the existing transaction id only; no second transaction is "
        "started and no duplicate charge is possible.</p></div>"
        f'<form method="post" action="/s/{sid}/reconcile">'
        f'<input type="hidden" name="request_token" value="{esc(view.case.request_token)}">'
        '<button class="primary" type="submit">Check the existing transaction</button></form>'
        f'<p><a href="{REFUND_LINK}">Open the merchant return portal</a> for support in parallel.</p>'
    )
    return layout(title="Reconciling", body=body, session_id=view.session.session_id)


def upload_page(
    *,
    record: Mapping[str, Any],
    order: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    reason_codes: Sequence[str],
) -> str:
    row_items = "".join(
        "<tr><td>" + esc(row.get("sku")) + "</td><td>" + esc(row.get("category")) + "</td>"
        "<td>" + esc(row.get("size")) + "</td><td>" + esc(row.get("colorFamily")) + "</td>"
        "<td class=\"num\">" + esc(money(int(row.get("priceMinor", 0)), str(row.get("currency", "USD"))))
        + "</td><td class=\"num\">" + esc(row.get("stock")) + "</td></tr>"
        for row in rows
    )
    body = (
        "<h1>Scenario file validated</h1>"
        '<div class="status" role="status"><p>The file is valid. Review the exact captured '
        "bytes below, then start the return with it.</p></div>"
        '<div class="card"><h2>Captured file</h2><dl class="facts">'
        f"<dt>File</dt><dd>{esc(record.get('filename'))}</dd>"
        f"<dt>Bytes</dt><dd>{esc(record.get('byte_size'))}</dd>"
        f"<dt>SHA-256</dt><dd><code>{esc(record.get('sha256'))}</code></dd>"
        "</dl><p class=\"muted\">Stored outside any public directory under an opaque handle; "
        "the file itself is never re-served.</p></div>"
        '<div class="card"><h2>Order</h2><dl class="facts">'
        f"<dt>Order</dt><dd>{esc(order.get('orderId'))}</dd>"
        f"<dt>Merchant</dt><dd>{esc(order.get('merchant'))}</dd>"
        f"<dt>Item</dt><dd>{esc(order.get('originalSku'))} — size {esc(order.get('size'))}</dd>"
        f"<dt>Value</dt><dd>{esc(money(int(order.get('originalValueMinor', 0)), str(order.get('currency', 'USD'))))}</dd>"
        "</dl></div>"
        "<table><caption>Catalog rows supplied by the file</caption>"
        "<thead><tr><th scope=\"col\">SKU</th><th scope=\"col\">Category</th>"
        "<th scope=\"col\">Size</th><th scope=\"col\">Colour</th>"
        "<th scope=\"col\">Price</th><th scope=\"col\">Stock</th></tr></thead>"
        f"<tbody>{row_items}</tbody></table>"
        "<fieldset><legend>Reasons supplied by the file</legend><p>"
        + esc(", ".join(reason_codes) if reason_codes else "none — the ordinary return route applies")
        + "</p></fieldset>"
        '<form method="post" action="/returns">'
        f'<input type="hidden" name="uploadHandle" value="{esc(record.get("handle"))}">'
        '<button class="primary" type="submit">Start this return with the uploaded scenario</button>'
        "</form>"
        '<p><a href="/">Back to the standard form</a></p>'
    )
    return layout(title="Scenario file", body=body)


def refund_host_page(*, reference: str | None = None, note: str | None = None) -> str:
    """The independent host refund screen.  Reads no session and no store."""

    body = (
        "<h1>Merchant return portal</h1>"
        '<div class="status" role="status"><p>This is the merchant host return flow, served '
        "independently of the SwapSure rescue application.</p></div>"
        '<div class="card"><h2>Your return</h2><dl class="facts">'
        f"<dt>Reference</dt><dd>{esc(reference or 'not linked to a rescue session')}</dd>"
        "<dt>Status</dt><dd>PENDING_HOST — the merchant owns the refund lifecycle</dd>"
        "<dt>Next step</dt><dd>Follow the merchant instructions below; this page does not "
        "promise a completed refund.</dd>"
        "</dl>"
        "<p>If this page is available but the rescue application is not, your return still "
        "works. That independence is deliberate.</p>"
        "<h2>Contact</h2><p>Support: returns@example.invalid (synthetic fixture; no message "
        "is sent by this build).</p>"
        f"{('<p>' + esc(note) + '</p>') if note else ''}"
        "</div>"
        '<p><a href="/">Back to the return start</a></p>'
    )
    return layout(title="Merchant return portal", body=body)


def consent_panel_page(view: CaseView, *, mode: str = "MOCK") -> str:
    history = view.consent
    sid = esc(view.session.session_id)
    rows = "".join(
        f"<tr><td><code>{esc(item.get('action'))}</code></td>"
        f"<td>{esc(item.get('consent_version') or '')}</td>"
        f"<td>{esc(item.get('recorded_at'))}</td></tr>"
        for item in view.consent_history
    )
    body = (
        "<h1>Consent and image controls</h1>"
        f'<div class="card"><h2>Latest consent action</h2>'
        f"<p><code>{esc((history or {}).get('action') or 'NONE')}</code></p>"
        "<table><caption>Consent ledger for this session</caption>"
        "<thead><tr><th scope=\"col\">Action</th><th scope=\"col\">Version</th>"
        f"<th scope=\"col\">Recorded</th></tr></thead><tbody>{rows}</tbody></table></div>"
        '<div class="card"><h2>Your photo</h2>'
        "<p>Send one photo of yourself for the try-on. It is stored on this server, used only for "
        "the previews you asked for, and deleted here when you revoke consent.</p>"
        f'<form method="post" action="/s/{sid}/photo" enctype="multipart/form-data">'
        f'<input type="hidden" name="request_token" value="{esc(view.case.request_token)}">'
        '<label for="photo">Photo (PNG, 512x384 minimum, under 10 MB)</label>'
        '<input id="photo" name="photo" type="file" accept="image/png" required>'
        '<button type="submit">Send photo for try-on</button></form>'
        + photo_samples_html()
        + '<p class="muted">Deleting your photo here removes this server\'s copy only. It does not '
        "delete anything the try-on provider already received: the provider's developer "
        "documentation states media and file/task identifiers are retained for 30 days while "
        "result links expire after two hours, and a separate provider page says uploaded pictures "
        "are deleted within 24 hours. Those statements conflict, so no deletion promise is made "
        "beyond this server. Ask the provider for account-specific retention terms before sending "
        "a real photo.</p></div>"
        '<div class="card"><h2>Revoke image consent</h2>'
        "<p>Revocation stops all further preview dispatch, discards any in-flight result, "
        "deletes the local placeholders and moves this return to the ordinary refund route. "
        "The financial record is not rewritten.</p>"
        f'<form method="post" action="/s/{sid}/revoke">'
        f'<input type="hidden" name="request_token" value="{esc(view.case.request_token)}">'
        '<button class="danger" type="submit">Revoke image consent and continue refund</button>'
        "</form></div>"
        f'<div class="card"><h2>Delete local previews only</h2>'
        f'<form method="post" action="/s/{sid}/delete-media">'
        f'<input type="hidden" name="request_token" value="{esc(view.case.request_token)}">'
        '<button type="submit">Delete local preview images</button></form></div>'
        f'<p><a href="/s/{sid}">Back to this return</a></p>'
    )
    return layout(mode=mode, title="Consent controls", body=body,
                  session_id=view.session.session_id)


def ledger_page(*, rows: Sequence[Mapping[str, Any]], totals: Mapping[str, Any]) -> str:
    body_rows = "".join(
        "<tr><td>"
        + esc(row.get("transaction_id"))
        + "</td><td>"
        + esc(row.get("session_id"))
        + "</td><td>"
        + esc(row.get("outcome"))
        + "</td><td class=\"num\">"
        + esc(money(int(row.get("retained_gmv_minor", 0))))
        + "</td></tr>"
        for row in rows
    )
    conversion = (totals.get("metrics") or {}).get("rescueConversion")
    conversion_text = (
        "N/A (no denominator)" if conversion in (None, "N/A") else str(conversion)
    )
    body = (
        "<h1>Local outcome ledger</h1>"
        '<div class="card"><h2>Totals</h2><dl class="facts">'
        f"<dt>Rows</dt><dd>{esc(totals.get('rows', len(rows)))}</dd>"
        f"<dt>Retained original GMV</dt><dd>{esc(money(int(totals.get('retainedGmvMinor', 0))))}</dd>"
        f"<dt>Rescue conversion</dt><dd>{esc(conversion_text)}</dd>"
        "</dl>"
        "<p class=\"muted\">Simulated exchange outcomes only. A refund or failed attempt "
        "retains zero GMV. Deduplicated by committed transaction id.</p></div>"
        "<table><caption>Outcome rows</caption><thead><tr><th scope=\"col\">Transaction</th>"
        "<th scope=\"col\">Session</th><th scope=\"col\">Outcome</th>"
        f"<th scope=\"col\">Retained GMV</th></tr></thead><tbody>{body_rows}</tbody></table>"
    )
    return layout(title="Outcome ledger", body=body)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
def error_page(
    *,
    code: str,
    message: str,
    status: int,
    details: Iterable[str] = (),
    session_id: str | None = None,
    mode: str = "MOCK",
) -> str:
    detail_items = "".join(f"<li>{esc(item)}</li>" for item in details)
    back = (
        f'<p><a href="/s/{esc(session_id)}">Back to this return</a></p>' if session_id else ""
    )
    body = (
        "<h1>Request refused</h1>"
        '<div class="error" role="alert">'
        f"<p><strong>{esc(code)}</strong></p><p>{esc(message)}</p>"
        f"<p class=\"muted\">HTTP status {esc(status)}</p></div>"
        + (f"<ul class=\"plain\">{detail_items}</ul>" if detail_items else "")
        + back
        + '<div class="card"><h2>You can still leave</h2>'
        "<p>No refusal blocks the ordinary return route.</p>"
        f'<p><a class="refund" href="{REFUND_LINK}">Open the merchant return portal</a></p>'
        f'<p><a href="/">Start again</a></p></div>'
    )
    return layout(title="Request refused", body=body, session_id=session_id, mode=mode)


def _collection_strip(mode: str) -> str:
    """The two bundled collections, reachable from every page — browsing creates no work."""
    links = "".join(
        f'<li><a class="choice" href="/catalog/{key}">{esc(label)}</a></li>'
        for key, label in (("women", "Women's collection"), ("men", "Men's collection")))
    return (f'<section class="card" aria-label="Sample collections">'
            f"<h2>Start from a sample collection</h2>"
            f"<p>Two sample collections of synthetic garment references. Browsing them creates no "
            f"provider task and costs nothing; you choose a garment, then bring your own photo.</p>"
            f'<ul class="choices">{links}</ul>'
            f'<p><a href="/catalog">Compare both collections</a></p></section>')


def _garment_card(collection: str, item, *, featured: bool) -> str:
    stock = f"{item.stock} in stock" if item.stock > 0 else "out of stock"
    badge = "" if item.stock > 0 else ' <span class="muted">(not selectable)</span>'
    disabled = "" if item.stock > 0 else " disabled"
    featured_note = "Featured" if featured else item.garment_type.title()
    return (
        f'<li class="garment">'
        f'<img src="/catalog/{esc(collection)}/thumb/{esc(item.sku)}" width="180" height="270" '
        # Eager, not lazy: a hidden or below-the-fold tab never fetches a lazy card image, and
        # a catalogue whose pictures do not appear is not a visual catalogue.
        f'alt="{esc(item.name)}" loading="eager" decoding="async">'
        f'<h3>{esc(item.name)}</h3>'
        f'<p class="muted">{esc(featured_note)} &middot; size {esc(item.size)} &middot; {esc(stock)}</p>'
        f'<p><strong>USD {item.price_minor / 100:.2f}</strong>{badge}</p>'
        f'<p class="muted">Provider category: {esc(item.provider_category)} '
        f'(descriptive mapping, not live-verified)</p>'
        f'<form method="post" action="/catalog/{esc(collection)}/select">'
        f'<input type="hidden" name="sku" value="{esc(item.sku)}">'
        f'<button type="submit"{disabled}>Choose this garment</button></form>'
        f"</li>")


def catalog_index_page(*, mode: str = "MOCK") -> str:
    body = (
        "<h1>Sample collections</h1>"
        '<div class="card"><h2>Women&#39;s collection</h2>'
        "<p>Ten synthetic garment references. Pick one to try on with your own photo.</p>"
        '<p><a class="choice" href="/catalog/women">Open the women&#39;s collection</a></p></div>'
        '<div class="card"><h2>Men&#39;s collection</h2>'
        "<p>Ten synthetic garment references, modern v2 for the first five.</p>"
        '<p><a class="choice" href="/catalog/men">Open the men&#39;s collection</a></p></div>'
        '<div class="card"><h2>Advanced</h2>'
        "<p>Import your own scenario file. This is not needed for the sample walkthrough and is "
        "kept for testing.</p>"
        '<p><a href="/">Open the scenario import</a></p></div>'
    )
    return layout(title="Sample collections", body=body, mode=mode)


def catalog_collection_page(collection: str, *, mode: str = "MOCK") -> str:
    from domain.samples import COLLECTION_LABELS, FAMILY_LABELS, featured as featured_items

    ordered = featured_items(collection)
    top_five = ordered[:5]
    rest = ordered[5:]
    cards = "".join(_garment_card(collection, item, featured=True) for item in top_five)
    more = "".join(_garment_card(collection, item, featured=False) for item in rest)
    families = ", ".join(sorted({FAMILY_LABELS[i.family] for i in ordered}))
    body = (
        f"<h1>{esc(COLLECTION_LABELS[collection])}</h1>"
        f'<div class="card"><h2>Featured</h2>'
        f"<p>These five are shown first for visual clarity and variety. Every reference is a single "
        f"garment; a preview shows that one garment, not a whole outfit.</p>"
        f'<ul class="garments">{cards}</ul></div>'
        f'<div class="card"><h2>Also available</h2><ul class="garments">{more}</ul></div>'
        f'<div class="card"><h2>How this works</h2>'
        f"<p>Families in this collection: {esc(families)}. Choosing a garment starts a sample "
        f"return for a comparable item; eligible alternatives and the refund route stay available.</p>"
        f"<p>Selecting a garment is not consent, not generation and not exchange approval. Nothing "
        f"is sent to a provider until you consent, upload your photo, and confirm the cost.</p>"
        f'<p><a href="/catalog">Back to collections</a></p></div>'
    )
    return layout(step=1, title=COLLECTION_LABELS[collection], body=body, mode=mode)
