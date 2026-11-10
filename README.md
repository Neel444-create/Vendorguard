# VendorGuard

A runnable vendor-onboarding workflow MVP with a no-build HTML/CSS/JavaScript interface. It accepts structured submissions, PDF/image attachments, and CSV batches; executes real validation rules in a background worker; records every stage; and exposes a dashboard, live run timeline, decision detail, and follow-up text.

> **Important scope boundary:** VendorGuard currently performs deterministic format, completeness, exact-duplicate, and cross-field consistency checks. It does not contact banks, government registries, sanctions lists, or credit bureaus. A syntactically valid tax ID/IFSC/account number is not proof that it exists or belongs to the vendor. The approval result means all *configured local checks* passed. Configure procurement sign-off and external verification before production payment use.

## Quick start (local)

Requirements: Python 3.10+.

```bash
cd vendorguard
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000**. API documentation: **http://127.0.0.1:8000/docs**.

PDF text extraction uses PyMuPDF. Scanned PDFs and image OCR are not included in this MVP; install/configure Tesseract and add an OCR adapter before relying on scanned documents. If PyMuPDF is unavailable, uploaded documents are still stored and tracked, but their text is not extracted.

## Quick start (Docker)

```bash
docker compose up --build
```

Then open http://localhost:8000. Uploaded files and SQLite database persist under `./storage`.

## Deploy a public demo on Render

This repository includes `render.yaml` and a Docker command that listens on Render's assigned `PORT`.

1. Create a GitHub repository and upload the contents of this `vendorguard` folder (so `Dockerfile`, `render.yaml`, `backend/`, and `frontend/` are at the repository root).
2. In Render, choose **New → Blueprint**, connect that GitHub repository, and confirm the `vendorguard` service.
3. Wait for the deploy to finish, then open the generated `https://...onrender.com` URL. Check `/api/health` if you need to confirm the API is responding.
4. Use synthetic demo data only. The included free Render plan has an ephemeral filesystem: SQLite history and uploaded files can be lost when the service spins down, restarts, or redeploys. For persistent records, use a paid service with a persistent disk mounted at `/app/storage`, or migrate SQLite and uploads to managed persistent services.

Do not upload real vendor banking/tax data or use this public demo for payment decisions. The MVP has no authentication or encryption at rest and does not perform external bank or government verification.

## Demo workflow

1. Click **Load demo submissions** on the dashboard, or create a new vendor.
2. Watch the backend-generated processing timeline.
3. Open the result to inspect individual checks, reasons, and next actions.
4. For pending submissions, generate the vendor follow-up or record a procurement review decision.
5. Export the run history using **Export CSV**.

Demo submissions are synthetic. The sample successful vendor uses a synthetic tax identifier and synthetic bank details; never use the sample values for real transactions.

## Supported inputs

### Manual form / JSON

`POST /api/submissions`

```json
{
  "company_name": "Example Manufacturing Pvt Ltd",
  "country": "India",
  "registration_number": "U12345TG2020PTC123456",
  "tax_id": "ABCDE1234F",
  "gstin": "36ABCDE1234F1Z5",
  "bank_account_holder": "Example Manufacturing Pvt Ltd",
  "account_number": "123456789012",
  "ifsc": "SBIN0001234",
  "email": "accounts@example.invalid",
  "documents": ["company_registration", "tax_certificate", "bank_proof"]
}
```

### File upload

`POST /api/submissions/upload` as multipart form data:
- `payload`: JSON string using the same fields as above.
- `files`: up to 10 PDF, PNG, JPG, or JPEG files, max 10 MB each.

Filename patterns are used as hints to classify document types (`incorporation`, `registration`, `gst`, `pan`, `tax`, `bank`, `cheque`, `account`). This is not proof of document authenticity.

### CSV import

`POST /api/import/csv` with multipart field `file`. Header names should match the JSON field names. Use `documents` as a semicolon-separated list or leave it empty. Example: `samples/vendors.csv`.

## API overview

| Endpoint | Description |
|---|---|
| `GET /api/health` | Health check |
| `POST /api/submissions` | Create structured submission |
| `POST /api/submissions/upload` | Submit fields and files |
| `POST /api/import/csv` | Import a batch |
| `GET /api/dashboard` | Counts and recent activity |
| `GET /api/submissions` | Search/filter run history |
| `GET /api/submissions/{run_id}` | Full run, checks, docs, events, audit |
| `GET /api/submissions/{run_id}/events` | Incremental live events (polling endpoint) |
| `GET /api/submissions/{run_id}/follow-up` | Generate follow-up message for pending run |
| `POST /api/submissions/{run_id}/review` | Procurement approve/reject action |
| `GET /api/export.csv` | Export history |
| `POST /api/demo/seed` | Create synthetic demo runs |

## Decision policy

- **APPROVED:** All configured checks pass. This is a local rules-based outcome, not external identity or bank verification.
- **PENDING:** Missing fields/documents, invalid formats, or mismatches require correction or human review.
- **REJECTED:** Exact tax-ID duplicate against an existing approved vendor record, or an explicit procurement rejection.
- **ERROR:** An unexpected processing error occurred; it is never silently treated as approved.

A mismatch is not automatically fraud. The system flags it and asks for review. Only an exact duplicate identifier is an automated rejection in the current policy.

## Validation checks in this build

1. Required-field completeness.
2. India PAN structure (when country is India).
3. India GSTIN structure (optional if not supplied).
4. IFSC structure.
5. Bank account number structure (6–18 digits).
6. Company-name / bank-beneficiary fuzzy similarity.
7. Required document-category presence.
8. Exact tax-ID duplicate against approved vendor records.
9. Basic document text presence/consistency signal when extractable text is available.

Country-specific rules beyond India are placeholders and should be added with reviewed jurisdiction-specific specifications. The current document text check is a weak signal and must not be treated as authenticity verification.

## Data and security notes

- SQLite is selected to make the project runnable without provisioning a database. It can be moved to PostgreSQL by replacing the persistence layer.
- Uploaded files are stored in `storage/uploads`; the database stores metadata and references.
- Account numbers are masked in the detail UI, but the MVP does not yet encrypt sensitive fields at rest. Use synthetic data only until encryption, access control, retention, malware scanning, and secret management are implemented.
- Add authentication/RBAC, CSRF protections where applicable, rate limits, antivirus scanning, encrypted object storage, encrypted database columns, and a formal audit-retention policy before deployment with real vendor data.
- This is a single-process worker model for a demo. For production, move execution to a durable queue (Celery/RQ) and use PostgreSQL plus object storage.

## Project structure

```text
vendorguard/
├── backend/main.py          # API, persistence, validation, decision engine, worker
├── frontend/index.html      # Application shell
├── frontend/styles.css     # UI styling
├── frontend/app.js          # Dashboard, submission, live run, detail views
├── samples/vendors.csv     # CSV import sample
├── tests/test_rules.py      # Basic validation helper tests
├── storage/uploads/         # Uploaded documents (git-ignored)
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
