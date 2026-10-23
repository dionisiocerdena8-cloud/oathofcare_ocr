# Oath of Care — Private OCR API

This deployable OCR service processes **synthetic/test documents during setup**. It is not a document verification or legitimacy service. It does not make pharmacy approval decisions.

## Deployment on Railway

1. Put **only this folder's contents** in a separate private GitHub repository (or a separate folder with Railway's Root Directory set to that folder). Never use a legacy or phase-gated repository without authorization.
2. In [Railway](https://railway.com), **New Project → Deploy from GitHub repo** and select the repository.
3. Railway should build from `Dockerfile`. Set environment variable `OCR_API_TOKEN` to a randomly generated secret at least 32 characters long (use `python -c "import secrets; print(secrets.token_urlsafe(48))"`). Never commit the token.
4. The service listens on Railway's `PORT`. Set Healthcheck Path to `/health`. Enable a generated HTTPS public domain.
5. Verify `GET https://<domain>/health` returns `{"status":"ok"}` and test `/v1/analyze` with a synthetic image or PDF.
6. Keep the token **only in your Java Spring Boot backend's environment**, not in the Pharmacy Web client bundle. The Java backend should validate user session, authorization, rate limits, upload type/size, and forward the file using `Authorization: Bearer <token>`.

## API

- `GET /health` — public service health only.
- `POST /v1/analyze` — authenticated multipart upload field `file` (JPG/PNG/WebP or PDF, up to 10 MB; PDFs max 4 pages).
- `Authorization: Bearer <OCR_API_TOKEN>` required.
- Returns `document_kind`, `page_count`, `extracted_text`, tentative `candidates`, per-page quality warnings, and `authenticity_verified: false`.

Example (synthetic image only):

```bash
curl -X POST 'https://<domain>/v1/analyze' \
  -H 'Authorization: Bearer YOUR_SERVER_SECRET' \
  -F 'file=@synthetic_sample.png'
```

Do not send real license, permit, or ID documents before privacy, retention/deletion, authorization, abuse protection, and logging policies have been reviewed. Production protections still needed: rate limiting/API gateway, logging redaction, anti-malware controls, resource limits/abuse protection, reviewed data-processing disclosures, and an operational deletion policy. Protect the public endpoint using the API secret; do not call it directly from the browser.

**Constraints:** OCR reads pixels and suggests text; blurry-image warnings are heuristics. It cannot establish authenticity, validate permits against government records, or reliably infer missing business details. It does not write files to disk or persist extracted text in a database.
