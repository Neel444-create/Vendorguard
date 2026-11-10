from __future__ import annotations
import csv, io, json, os, re, sqlite3, threading, time, uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv('VENDORGUARD_DB', ROOT / 'storage' / 'vendorguard.db'))
UPLOAD_DIR = Path(os.getenv('VENDORGUARD_UPLOADS', ROOT / 'storage' / 'uploads'))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
POOL = ThreadPoolExecutor(max_workers=4)
LOCK = threading.Lock()

app = FastAPI(title='VendorGuard API', version='1.0.0', description='Explainable vendor onboarding workflow')
app.mount('/static', StaticFiles(directory=ROOT / 'frontend'), name='static')


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS vendors (
          id TEXT PRIMARY KEY, company_name TEXT NOT NULL, country TEXT NOT NULL,
          tax_id TEXT, registration_number TEXT, email TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS submissions (
          id TEXT PRIMARY KEY, vendor_id TEXT, company_name TEXT NOT NULL, payload TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'PROCESSING', risk_level TEXT, summary TEXT,
          checks_passed INTEGER DEFAULT 0, checks_failed INTEGER DEFAULT 0,
          created_at TEXT NOT NULL, completed_at TEXT,
          FOREIGN KEY(vendor_id) REFERENCES vendors(id)
        );
        CREATE TABLE IF NOT EXISTS documents (
          id TEXT PRIMARY KEY, submission_id TEXT NOT NULL, filename TEXT NOT NULL,
          stored_name TEXT NOT NULL, doc_type TEXT NOT NULL, extracted_text TEXT,
          created_at TEXT NOT NULL, FOREIGN KEY(submission_id) REFERENCES submissions(id)
        );
        CREATE TABLE IF NOT EXISTS validation_results (
          id TEXT PRIMARY KEY, submission_id TEXT NOT NULL, check_name TEXT NOT NULL,
          status TEXT NOT NULL, severity TEXT NOT NULL, reason TEXT NOT NULL,
          action_required TEXT, evidence TEXT, created_at TEXT NOT NULL,
          FOREIGN KEY(submission_id) REFERENCES submissions(id)
        );
        CREATE TABLE IF NOT EXISTS stage_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id TEXT NOT NULL,
          stage TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL,
          created_at TEXT NOT NULL, FOREIGN KEY(submission_id) REFERENCES submissions(id)
        );
        CREATE TABLE IF NOT EXISTS audit_logs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id TEXT NOT NULL,
          action TEXT NOT NULL, actor TEXT NOT NULL, details TEXT NOT NULL,
          created_at TEXT NOT NULL, FOREIGN KEY(submission_id) REFERENCES submissions(id)
        );
        CREATE INDEX IF NOT EXISTS idx_submissions_created ON submissions(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_validation_submission ON validation_results(submission_id);
        ''')


@app.on_event('startup')
def startup() -> None:
    init_db()


class VendorSubmission(BaseModel):
    company_name: str = Field(min_length=2, max_length=200)
    country: str = 'India'
    registration_number: str = ''
    tax_id: str = ''
    gstin: str = ''
    bank_account_holder: str = ''
    account_number: str = ''
    ifsc: str = ''
    email: str = ''
    documents: list[str] = Field(default_factory=list)
    notes: str = ''


def event(sub_id: str, stage: str, status: str, message: str) -> None:
    with db() as c:
        c.execute('INSERT INTO stage_events(submission_id,stage,status,message,created_at) VALUES(?,?,?,?,?)',
                  (sub_id, stage, status, message, now()))


def audit(sub_id: str, action: str, details: dict[str, Any], actor: str = 'system') -> None:
    with db() as c:
        c.execute('INSERT INTO audit_logs(submission_id,action,actor,details,created_at) VALUES(?,?,?,?,?)',
                  (sub_id, action, actor, json.dumps(details), now()))


def normalize(s: str) -> str:
    return re.sub(r'[^A-Z0-9]', '', (s or '').upper())


def add_check(results: list[dict], name: str, passed: bool, reason: str,
              severity: str = 'low', action: str = '') -> None:
    results.append({'check_name': name, 'status': 'PASS' if passed else 'FAIL',
                    'severity': severity if not passed else 'none', 'reason': reason,
                    'action_required': action if not passed else ''})


def run_validation(sub_id: str, payload: dict, extracted_docs: list[dict]) -> None:
    try:
        event(sub_id, 'extraction', 'running', 'Normalizing submitted fields and document metadata.')
        time.sleep(0.15)
        docs = payload.get('documents') or []
        docs = [str(x).lower() for x in docs]
        docs += [d['doc_type'] for d in extracted_docs]
        text_by_type = {d['doc_type']: (d.get('extracted_text') or '') for d in extracted_docs}
        event(sub_id, 'extraction', 'completed', f"Submission parsed; {len(extracted_docs)} uploaded file(s) attached.")

        results: list[dict] = []
        event(sub_id, 'validation', 'running', 'Checking mandatory company, tax, banking, and document fields.')
        required = ['company_name', 'country', 'registration_number', 'tax_id', 'bank_account_holder', 'account_number', 'ifsc']
        missing = [k for k in required if not str(payload.get(k, '')).strip()]
        add_check(results, 'Completeness', not missing,
                  'All mandatory fields are present.' if not missing else 'Missing required fields: ' + ', '.join(missing),
                  'medium', 'Complete the missing fields: ' + ', '.join(missing))

        country = str(payload.get('country', 'India')).strip().lower()
        tax = normalize(str(payload.get('tax_id', '')))
        gst = normalize(str(payload.get('gstin', '')))
        if country == 'india':
            tax_ok = bool(re.fullmatch(r'[A-Z]{5}[0-9]{4}[A-Z]', tax)) if tax else False
            add_check(results, 'PAN format', tax_ok,
                      'PAN matches the expected 10-character structure.' if tax_ok else 'PAN is missing or does not match the expected Indian PAN structure.',
                      'medium', 'Provide a valid PAN in the format ABCDE1234F.')
            gst_ok = (not gst) or bool(re.fullmatch(r'\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]', gst))
            add_check(results, 'GSTIN format', gst_ok,
                      'GSTIN format is valid or GSTIN was not supplied.' if gst_ok else 'GSTIN does not match the expected 15-character structure.',
                      'medium', 'Correct the GSTIN or provide a valid GST registration document.')
        else:
            add_check(results, 'Tax identifier format', bool(tax),
                      'A tax identifier was supplied; country-specific registry verification is not configured.' if tax else 'No tax identifier supplied.',
                      'medium', 'Provide the tax identifier required in the selected country.')

        ifsc = normalize(str(payload.get('ifsc', '')))
        ifsc_ok = bool(re.fullmatch(r'[A-Z]{4}0[A-Z0-9]{6}', ifsc))
        add_check(results, 'IFSC format', ifsc_ok,
                  'IFSC matches the expected 11-character structure.' if ifsc_ok else 'IFSC is missing or has an invalid structure.',
                  'medium', 'Provide an IFSC in the format ABCD0123456.')
        account = re.sub(r'\s+', '', str(payload.get('account_number', '')))
        account_ok = bool(re.fullmatch(r'[0-9]{6,18}', account))
        add_check(results, 'Bank account format', account_ok,
                  'Account number contains 6–18 digits.' if account_ok else 'Account number must contain 6–18 digits.',
                  'medium', 'Correct the bank account number.')

        legal = normalize(str(payload.get('company_name', '')))
        beneficiary = normalize(str(payload.get('bank_account_holder', '')))
        ratio = SequenceMatcher(None, legal, beneficiary).ratio() if legal and beneficiary else 0
        name_ok = ratio >= 0.88
        add_check(results, 'Bank beneficiary name match', name_ok,
                  f'Company and beneficiary names have a {ratio:.0%} normalized similarity.' if legal and beneficiary else 'Company name or bank beneficiary name is missing.',
                  'high', 'Provide bank proof showing the legal entity name, or request procurement review of an approved legal-name variation.')

        required_docs = {'company_registration', 'tax_certificate', 'bank_proof'}
        supplied_docs = set()
        for d in docs:
            key = re.sub(r'[^a-z_]', '', d.replace(' ', '_'))
            if any(k in key for k in ('company_registration', 'incorporation', 'registration_certificate')): supplied_docs.add('company_registration')
            if any(k in key for k in ('tax_certificate', 'gst_certificate', 'gstin_certificate', 'pan_card')): supplied_docs.add('tax_certificate')
            if any(k in key for k in ('bank_proof', 'cancelled_cheque', 'bank_statement')): supplied_docs.add('bank_proof')
        for d in extracted_docs:
            supplied_docs.add(d['doc_type'])
        missing_docs = sorted(required_docs - supplied_docs)
        add_check(results, 'Required documents', not missing_docs,
                  'All three required document categories are attached.' if not missing_docs else 'Missing document categories: ' + ', '.join(missing_docs),
                  'medium', 'Upload: ' + ', '.join(missing_docs))

        # Compare identifiers against prior vendors; only exact tax-ID duplication is a hard rejection.
        duplicate = False
        if tax:
            with db() as c:
                duplicate = c.execute('SELECT 1 FROM vendors WHERE upper(replace(replace(tax_id,\' \',\'\'),\'-\',\'\'))=? LIMIT 1', (tax,)).fetchone() is not None
        add_check(results, 'Duplicate registration check', not duplicate,
                  'No exact matching tax identifier exists in the vendor register.' if not duplicate else 'This tax identifier is already associated with an existing vendor record.',
                  'critical', 'Contact procurement to resolve the existing vendor record.')

        # Evidence is useful but does not imply official authenticity verification.
        extracted_text = ' '.join(d.get('extracted_text', '') for d in extracted_docs).lower()
        doc_name_match = True
        if extracted_text and legal and len(extracted_text) > 20:
            # Only flag an obvious contradiction where the legal name is not represented at all.
            doc_name_match = legal in normalize(extracted_text) or SequenceMatcher(None, legal, normalize(extracted_text)).ratio() > 0.55
        add_check(results, 'Document text consistency', doc_name_match,
                  'No obvious company-name contradiction was detected in extractable document text.' if doc_name_match else 'Uploaded document text may not contain the submitted legal entity name.',
                  'high', 'Review the source document and confirm it belongs to this legal entity.')

        event(sub_id, 'validation', 'completed', f'{sum(r["status"] == "PASS" for r in results)} checks passed; {sum(r["status"] == "FAIL" for r in results)} require attention.')
        event(sub_id, 'decision', 'running', 'Applying deterministic status rules.')
        failed = [r for r in results if r['status'] == 'FAIL']
        hard_reject = any(r['check_name'] == 'Duplicate registration check' and r['status'] == 'FAIL' for r in failed)
        if hard_reject:
            status, risk, summary = 'REJECTED', 'HIGH', 'An exact tax identifier duplicate exists in the vendor register. Procurement investigation is required.'
        elif failed:
            status = 'PENDING'
            risk = 'HIGH' if any(r['severity'] == 'high' for r in failed) else 'MEDIUM'
            summary = f'{len(failed)} validation issue(s) require vendor correction or procurement review.'
        else:
            status, risk, summary = 'APPROVED', 'LOW', 'All configured onboarding checks passed. This is a rules-based system decision, not independent bank or government registry authentication.'

        with db() as c:
            row = c.execute('SELECT * FROM submissions WHERE id=?', (sub_id,)).fetchone()
            vendor_id = row['vendor_id']
            if status == 'APPROVED' and not vendor_id:
                vendor_id = 'VEN-' + uuid.uuid4().hex[:8].upper()
                c.execute('INSERT INTO vendors(id,company_name,country,tax_id,registration_number,email,created_at) VALUES(?,?,?,?,?,?,?)',
                          (vendor_id, payload.get('company_name'), payload.get('country','India'), payload.get('tax_id'), payload.get('registration_number'), payload.get('email'), now()))
            for r in results:
                c.execute('INSERT INTO validation_results(id,submission_id,check_name,status,severity,reason,action_required,evidence,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                          (uuid.uuid4().hex, sub_id, r['check_name'], r['status'], r['severity'], r['reason'], r['action_required'], json.dumps({'source':'submitted fields and uploaded document text'}), now()))
            c.execute('UPDATE submissions SET vendor_id=?,status=?,risk_level=?,summary=?,checks_passed=?,checks_failed=?,completed_at=? WHERE id=?',
                      (vendor_id, status, risk, summary, len(results)-len(failed), len(failed), now(), sub_id))
        event(sub_id, 'decision', 'completed', f'Final status: {status}. {summary}')
        event(sub_id, 'complete', 'completed', 'Processing run completed and results saved to the audit history.')
        audit(sub_id, 'decision_generated', {'status':status,'risk_level':risk,'checks':results})
    except Exception as exc:
        event(sub_id, 'system', 'failed', f'Processing failed safely: {type(exc).__name__}: {str(exc)[:240]}')
        with db() as c:
            c.execute("UPDATE submissions SET status='ERROR',summary=?,completed_at=? WHERE id=?", (f'Processing error: {type(exc).__name__}', now(), sub_id))
        audit(sub_id, 'processing_error', {'error':type(exc).__name__})


def create_submission(payload: dict, files_meta: list[dict] | None = None) -> str:
    sub_id = 'RUN-' + uuid.uuid4().hex[:10].upper()
    with db() as c:
        c.execute('INSERT INTO submissions(id,company_name,payload,status,created_at) VALUES(?,?,?,?,?)',
                  (sub_id, payload.get('company_name','Unnamed vendor'), json.dumps(payload), 'PROCESSING', now()))
        for d in (files_meta or []):
            c.execute('INSERT INTO documents(id,submission_id,filename,stored_name,doc_type,extracted_text,created_at) VALUES(?,?,?,?,?,?,?)',
                      (uuid.uuid4().hex, sub_id, d['filename'], d['stored_name'], d['doc_type'], d.get('extracted_text',''), now()))
    event(sub_id, 'submission', 'completed', 'Vendor submission received and assigned a run ID.')
    audit(sub_id, 'submission_created', {'company_name':payload.get('company_name'),'document_count':len(files_meta or [])})
    POOL.submit(run_validation, sub_id, payload, files_meta or [])
    return sub_id


def classify_doc(filename: str) -> str:
    s = filename.lower().replace('-', '_').replace(' ', '_')
    if any(x in s for x in ['incorporation','registration','company_certificate','cin']): return 'company_registration'
    if any(x in s for x in ['gst','pan','tax']): return 'tax_certificate'
    if any(x in s for x in ['bank','cheque','account']): return 'bank_proof'
    return 'other'


def extract_pdf_text(path: Path) -> str:
    try:
        import fitz  # PyMuPDF, optional
        doc = fitz.open(path)
        return '\n'.join(page.get_text() for page in doc)[:30000]
    except ImportError:
        return ''
    except Exception:
        return ''


@app.get('/', response_class=HTMLResponse)
def home():
    return FileResponse(ROOT / 'frontend' / 'index.html')


@app.get('/api/health')
def health():
    return {'ok': True, 'service': 'VendorGuard', 'database': str(DB_PATH.name), 'time': now()}


@app.post('/api/submissions')
def submit_vendor(payload: VendorSubmission):
    data = payload.model_dump()
    sub_id = create_submission(data)
    return {'run_id':sub_id,'status':'PROCESSING','message':'Submission accepted; processing has started.'}


@app.post('/api/submissions/upload')
async def submit_upload(payload: str = Form(...), files: list[UploadFile] = File(default=[])):
    try: data = VendorSubmission.model_validate_json(payload).model_dump()
    except Exception as exc: raise HTTPException(422, f'Invalid vendor payload: {exc}')
    if len(files) > 10: raise HTTPException(400, 'Maximum 10 files per submission.')
    metas=[]
    for f in files:
        filename = Path(f.filename or 'upload').name
        ext = Path(filename).suffix.lower()
        if ext not in {'.pdf','.png','.jpg','.jpeg'}: raise HTTPException(400, f'Unsupported file type: {ext}')
        content = await f.read()
        if len(content) > 10 * 1024 * 1024: raise HTTPException(413, f'{filename} exceeds 10 MB.')
        stored = uuid.uuid4().hex + ext
        path = UPLOAD_DIR / stored
        path.write_bytes(content)
        text = extract_pdf_text(path) if ext == '.pdf' else ''
        metas.append({'filename':filename,'stored_name':stored,'doc_type':classify_doc(filename),'extracted_text':text})
    data['documents'] = list(data.get('documents', [])) + [m['doc_type'] for m in metas]
    sub_id=create_submission(data,metas)
    return {'run_id':sub_id,'status':'PROCESSING','uploaded_files':len(metas)}


@app.post('/api/import/csv')
async def import_csv(file: UploadFile = File(...)):
    if not (file.filename or '').lower().endswith('.csv'): raise HTTPException(400,'Upload a .csv file.')
    raw=(await file.read()).decode('utf-8-sig')
    reader=csv.DictReader(io.StringIO(raw))
    runs=[]
    for row in reader:
        payload={k: (v or '').strip() for k,v in row.items() if k}
        if not payload.get('company_name'): continue
        for key in ['country','registration_number','tax_id','gstin','bank_account_holder','account_number','ifsc','email']:
            payload.setdefault(key,'')
        docs_value = payload.get('documents', '')
        payload['documents'] = [x.strip() for x in docs_value.split(';') if x.strip()] if isinstance(docs_value, str) else docs_value
        runs.append(create_submission(payload))
    return {'created':len(runs),'run_ids':runs}


@app.get('/api/dashboard')
def dashboard():
    with db() as c:
        counts={r['status']:r['n'] for r in c.execute('SELECT status,COUNT(*) n FROM submissions GROUP BY status')}
        recent=[dict(r) for r in c.execute('SELECT id,company_name,status,risk_level,checks_passed,checks_failed,created_at,completed_at FROM submissions ORDER BY created_at DESC LIMIT 50')]
        total=sum(counts.values())
    return {'total':total,'approved':counts.get('APPROVED',0),'pending':counts.get('PENDING',0),'rejected':counts.get('REJECTED',0),'processing':counts.get('PROCESSING',0),'errors':counts.get('ERROR',0),'recent':recent}


@app.get('/api/submissions')
def list_submissions(status: str | None = None, q: str | None = None):
    sql='SELECT id,company_name,status,risk_level,checks_passed,checks_failed,created_at,completed_at FROM submissions WHERE 1=1'
    args=[]
    if status: sql+=' AND status=?'; args.append(status.upper())
    if q: sql+=' AND company_name LIKE ?'; args.append('%'+q+'%')
    sql+=' ORDER BY created_at DESC LIMIT 200'
    with db() as c: return [dict(r) for r in c.execute(sql,args)]


@app.get('/api/submissions/{sub_id}')
def get_submission(sub_id: str):
    with db() as c:
        s=c.execute('SELECT * FROM submissions WHERE id=?',(sub_id,)).fetchone()
        if not s: raise HTTPException(404,'Submission not found.')
        d=dict(s); d['payload']=json.loads(d['payload'])
        d['checks']=[dict(r) for r in c.execute('SELECT * FROM validation_results WHERE submission_id=? ORDER BY created_at',(sub_id,))]
        d['documents']=[dict(r) for r in c.execute('SELECT id,filename,doc_type,created_at FROM documents WHERE submission_id=?',(sub_id,))]
        d['events']=[dict(r) for r in c.execute('SELECT stage,status,message,created_at FROM stage_events WHERE submission_id=? ORDER BY id',(sub_id,))]
        d['audit']=[dict(r) for r in c.execute('SELECT action,actor,details,created_at FROM audit_logs WHERE submission_id=? ORDER BY id',(sub_id,))]
        return d


@app.get('/api/submissions/{sub_id}/events')
def get_events(sub_id: str, after: int = 0):
    with db() as c:
        if not c.execute('SELECT 1 FROM submissions WHERE id=?',(sub_id,)).fetchone(): raise HTTPException(404,'Submission not found.')
        rows=[dict(r) for r in c.execute('SELECT id,stage,status,message,created_at FROM stage_events WHERE submission_id=? AND id>? ORDER BY id',(sub_id,after))]
    return rows


@app.post('/api/submissions/{sub_id}/review')
def review_submission(sub_id: str, body: dict):
    decision=str(body.get('decision','')).upper()
    if decision not in {'APPROVE','REJECT'}: raise HTTPException(422,'decision must be APPROVE or REJECT')
    with db() as c:
        row=c.execute('SELECT * FROM submissions WHERE id=?',(sub_id,)).fetchone()
        if not row: raise HTTPException(404,'Submission not found.')
        if row['status'] not in {'PENDING','APPROVED','REJECTED'}: raise HTTPException(409,'Submission is not ready for review.')
        payload=json.loads(row['payload'])
        if decision=='APPROVE':
            vendor_id=row['vendor_id'] or 'VEN-'+uuid.uuid4().hex[:8].upper()
            if not row['vendor_id']:
                c.execute('INSERT INTO vendors(id,company_name,country,tax_id,registration_number,email,created_at) VALUES(?,?,?,?,?,?,?)',(vendor_id,row['company_name'],payload.get('country','India'),payload.get('tax_id'),payload.get('registration_number'),payload.get('email'),now()))
            c.execute("UPDATE submissions SET vendor_id=?,status='APPROVED',summary='Approved by procurement reviewer.',completed_at=? WHERE id=?",(vendor_id,now(),sub_id))
        else:
            c.execute("UPDATE submissions SET status='REJECTED',summary='Rejected by procurement reviewer.',completed_at=? WHERE id=?",(now(),sub_id))
    audit(sub_id,'human_review',{'decision':decision,'note':body.get('note','')},str(body.get('reviewer','procurement')))
    event(sub_id,'human_review','completed',f'{decision} decision recorded by procurement reviewer.')
    return {'run_id':sub_id,'status':'APPROVED' if decision=='APPROVE' else 'REJECTED'}


@app.get('/api/submissions/{sub_id}/follow-up')
def followup(sub_id: str):
    d=get_submission(sub_id)
    if d['status']!='PENDING': raise HTTPException(409,'Follow-up is available for pending submissions only.')
    actions=[c['action_required'] for c in d['checks'] if c['status']=='FAIL' and c['action_required']]
    vendor=d['company_name']
    body=f"Subject: Additional information required for vendor onboarding\n\nDear {vendor},\n\nWe reviewed your vendor onboarding submission. Please address the following items before we can proceed:\n\n"+'\n'.join(f'{i+1}. {a}' for i,a in enumerate(actions))+"\n\nPlease reply with the corrected information and supporting documents.\n\nRegards,\nProcurement Team"
    return {'text':body,'items':actions}


@app.get('/api/documents/{doc_id}')
def get_document(doc_id: str):
    with db() as c: row=c.execute('SELECT * FROM documents WHERE id=?',(doc_id,)).fetchone()
    if not row: raise HTTPException(404,'Document not found.')
    path=UPLOAD_DIR / row['stored_name']
    if not path.exists(): raise HTTPException(404,'Stored document unavailable.')
    return FileResponse(path,filename=row['filename'])


@app.get('/api/export.csv')
def export_csv():
    with db() as c: rows=c.execute('SELECT id,company_name,status,risk_level,checks_passed,checks_failed,created_at,completed_at,summary FROM submissions ORDER BY created_at DESC').fetchall()
    output=io.StringIO(); writer=csv.writer(output)
    writer.writerow(['run_id','company_name','status','risk_level','checks_passed','checks_failed','created_at','completed_at','summary'])
    for r in rows: writer.writerow(list(r))
    from fastapi.responses import Response
    return Response(output.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename=vendorguard-history.csv'})


@app.post('/api/demo/seed')
def seed_demo():
    samples=[
      {'company_name':'Aster Engineering Pvt Ltd','country':'India','registration_number':'U12345TG2020PTC123456','tax_id':'ABCDE1234F','gstin':'36ABCDE1234F1Z5','bank_account_holder':'Aster Engineering Pvt Ltd','account_number':'123456789012','ifsc':'SBIN0001234','email':'accounts@aster.example','documents':['company_registration','tax_certificate','bank_proof']},
      {'company_name':'BluePeak Systems','country':'India','registration_number':'U12345TG2021PTC123456','tax_id':'BCDEF2345G','gstin':'36BCDEF2345G1Z5','bank_account_holder':'BluePeak Systems','account_number':'123456789012','ifsc':'SBIN0001234','email':'finance@bluepeak.example','documents':['company_registration','bank_proof']},
      {'company_name':'Cedar Works Pvt Ltd','country':'India','registration_number':'U12345TG2022PTC123456','tax_id':'CDEFG3456H','gstin':'36CDEFG3456H1Z5','bank_account_holder':'Cedar Works Limited','account_number':'123456789012','ifsc':'SBIN0001234','email':'ops@cedar.example','documents':['company_registration','tax_certificate','bank_proof']}
    ]
    ids=[create_submission(x) for x in samples]
    return {'created':len(ids),'run_ids':ids}


if __name__ == '__main__':
    import uvicorn
    uvicorn.run('backend.main:app',host='0.0.0.0',port=8000,reload=True)
