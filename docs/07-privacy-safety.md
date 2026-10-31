# Privacy and safety policy
This is the product's engineering policy. Vendor legal terms and regional obligations require review before live processing; source-conversation claims about camera-module restrictions and retention are not a completed legal review.

## Consent and data flow
Rescue opt-in and image-processing consent are separate actions. Before image selection, disclose purpose, YouCam as processor, image inputs/outputs, intended retention, deletion limits and optionality. Use unchecked controls. Refusal preserves ordinary returns.

Allowed future flow: shopper → application media boundary → owner-approved YouCam processing → application → same shopper. No unrelated AI services, face-analysis APIs, advertising endpoints, session replay or analytics may receive images, image URLs, signed URLs or embeddings. Metadata-only LLM use is deferred and unnecessary for the MVP.

The offline demo uses synthetic metadata and placeholders; no real shopper photo is included. A future demo may use synthetic or expressly consented adult imagery with documented processing and publication rights. Do not assume a public product photo is licensed.

## Storage and deletion
Proposed local media TTL is 30 minutes maximum from creation, shortened by user deletion or session end. Store media outside public/static directories; use scoped opaque handles, access checks and no-store responses. Strip unnecessary metadata such as EXIF before approved upload. Validate decoded type, dimensions and size; reject multiple-person or unsuitable inputs without retaining them.

Delete source, generated previews, thumbnails, temporary uploads and local cache copies. Revoke handles immediately; cleanup retries must persist locally and resume after restart. Revocation prevents further dispatch and late-result display. Show local deletion success separately from any provider deletion status. Provider retention remains a gate item.

Use pseudonymous session IDs in the ledger. Retain only necessary transaction metadata under an owner-selected retention period before live deployment. Exclude image data and full URLs from telemetry, crash reports and backups. Log consent version, action and timestamp without unnecessary identity data.

## User experience requirements
Show “Visual preview only — not a fit or sizing guarantee.” Never promise attractiveness, fit, comfort, material feel or quality. Use “Alternative color” rather than “More flattering.” Keep refund equally understandable and keyboard accessible, including during loading and errors.

Unrelated services must not receive media through hidden SDK behavior. Validate network egress in tests; application documentation alone is insufficient. A privacy leak, lost refund route or fabricated human approval blocks release.
