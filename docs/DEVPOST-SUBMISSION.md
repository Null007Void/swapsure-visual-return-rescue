# Submission materials - SwapSure

Text prepared for the submission form. URL fields are marked pending and must not be filled with a guess.

## Project name

SwapSure - Visual Return Rescue

## Tagline

Turn a return into a rescue: see the alternative on your own photo, then choose the exchange or the refund.

## Description

Online returns are a dead end: the shopper sends the garment back and buys nothing. SwapSure gives the
return a second ending. The shopper opens a return, sees a curated garment catalogue, clicks one
garment, gives explicit image consent, and uploads their own photo. One clearly priced Generate action
later, the shopper sees the alternative on their own body, produced by Perfect Corp YouCam Cloth V4
try-on, and then chooses: exchange for that garment, try another one on the same photo, change the
photo, or take the ordinary refund.

The refund is never removed and never hidden behind the try-on. Consent is versioned and revocable; a
withdrawal discards the pending preview, deletes the local photo and returns the shopper to the refund.
Repeating the same photo and garment is refused rather than charged twice, and every paid operation is
recorded in a local ledger under a per-authorization ceiling.

What is real and what is simulated. The YouCam try-on previews are real provider output. The merchant,
the order, the stock, the price, the exchange quote and the refund are simulated: no shop is connected
and no money moves. Previews show how a garment looks visually; they are not a guarantee of fit, size
or availability.

## Built with

Python 3.13, a standard-library WSGI application, SQLite, Pillow for decoding provider images only,
Perfect Corp YouCam Cloth V4, Caddy in front for TLS and authentication.

## Demo video

    file       SwapSure-synced-narrated-review.mp4
    duration   2:30
    sha256     38696d75f72cb69c7490e0c4495ff8e8aa1a38dd0b6ce110453ca7456610c236
    url        PENDING - not uploaded

## Repository

    url        PENDING - nothing has been published
    licence    MIT, Null007Void | Null Foundry

## Testing instructions (for the judges)

The demo runs in LIVE mode: previews are real YouCam output and cost the organiser units, so please
start with up to three previews and leave room for other reviewers. This is a request, not a limit.

    1. open the URL in the submission; a username and password are supplied separately and privately
       in the submission notes. They are demo credentials, never provider credentials.
    2. choose Women or Men, then click a garment card.
    3. opt in, accept the image-consent step, and upload a PNG of yourself (at least 512x384).
    4. press "Generate preview" once and wait for the result page to refresh.
    5. from a result, use "Try something else on" (keeps your photo and consent), "Change photo"
       (invalidates the old photo consent and results), or continue to the refund.

If the allowance is exhausted the page says so plainly and offers the refund route; it never pretends
to be an offline demo. Judges do not need a YouCam key and are never asked to pay.

## Limitations to state in the submission

    - the exchange, the merchant and the refund are simulated
    - preview images show appearance, not fit; no size guarantee is implied
    - previews are capped by an allowance, which is a restriction on availability; the organisers
      "free of charge and without any restriction" wording has not been confirmed for a capped demo
