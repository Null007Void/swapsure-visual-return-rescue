# SwapSure 1.7.14 - release notes

## Judge sample photos

    The photo step now offers two optional PNG downloads beside the upload control:
      Female sample  /samples/female-sample.png
      Male sample    /samples/male-sample.png

    They are the verified synthetic adult references (sha256 dcde1267... and 7dbfefb7...), bundled
    read-only under assets/samples with a PROVENANCE.json. Downloading creates no case, no consent,
    no preview, no reservation and spends no unit: the handler touches no store and no ledger.
    Uploading one still goes through the ordinary consent, upload and explicit Generate steps.
    PNG only, 512x384 minimum, under 10 MB; a JPEG is refused exactly as before.

## Unchanged

    Consent, refund route, allowance accounting, duplicate protection and deletion wording are
    untouched. 712 tests pass.
