# SwapSure 1.7.12 - release notes

## Security and correctness

    - the Host allow-list is enforced on every request, not only on mutations
    - the trusted scheme is an operator declaration (--external-scheme https), never a client header
    - session cookies gain Secure under a declared HTTPS scheme and keep their local plain-HTTP shape
    - the public manifest verifier carries the complete release inventory, so deleting a file together
      with its manifest entry still fails; duplicate and unsafe manifest paths are rejected
    - the verifier checks the manifest against an independent required set before hashing anything

## Hosting

    - --bind, --public-host, --external-scheme, --threaded: hosted mode is opt-in, loopback stays default
    - optional shared-demo notice (SWAPSURE_JUDGING_NOTICE=1), advisory only
    - Dockerfile, Caddyfile and start script for a single VM with a persistent disk

## Documentation

    - Google Cloud deployment plan with resource choices and a cost estimate for the judging window
    - submission text, testing instructions and video metadata
    - the hosted-judging plan no longer references a hosting provider that was not chosen, states the
      availability requirement through 20 November 2026 16:45 UTC, and marks the compliance question open

## Verification

The suite runs on Python 3.13 with Pillow and pytest installed; the figures for this release are the
ones recorded on the tag. The package is built with git archive from the tag and compared member by
member against the commit.
