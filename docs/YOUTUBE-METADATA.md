# YouTube metadata - SwapSure demo

## Title (under 100 characters)

SwapSure - Visual Return Rescue (YouCam VTO hackathon demo)

## Description

Online returns are a dead end: you send the garment back and buy nothing. SwapSure gives a return a
second ending. Pick a garment from a curated catalogue, give consent, upload your own photo, and see
the alternative on your own body before you decide - exchange, or the ordinary refund.

This demo shows the full journey, end to end, in LIVE mode against the YouCam Cloth V4 API.

What is real and what is simulated:
- The garment previews are real YouCam provider output.
- The merchant, the order, stock, price, the exchange quote and the refund are simulated. No shop is
  connected and no money moves.
- Previews show appearance, not fit. No size or fit guarantee is implied.

Built with Python 3.13, a standard-library WSGI app, SQLite, and Pillow for decoding provider images.

Code: PENDING
Created by Null007Void | Null Foundry.
Original project code is MIT-licensed; third-party components and assets retain their
applicable terms.

## Chapters (optional)

00:00 Return without a preview
00:20 Choosing a garment
00:45 Consent and photo
01:10 Generating and reading the result
01:50 Trying another garment on the same photo
02:10 Changing the photo, and the refund route
