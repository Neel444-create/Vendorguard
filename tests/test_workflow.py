import tempfile
import time
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from backend import main

class WorkflowAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        main.DB_PATH = Path(cls.tmp.name) / 'test.db'
        main.UPLOAD_DIR = Path(cls.tmp.name) / 'uploads'
        main.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        main.init_db()
        cls.client_context = TestClient(main.app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        cls.tmp.cleanup()

    def wait_for(self, run_id):
        for _ in range(50):
            response = self.client.get(f'/api/submissions/{run_id}')
            self.assertEqual(response.status_code, 200)
            data = response.json()
            if data['status'] not in {'PROCESSING'}:
                return data
            time.sleep(.05)
        self.fail('Background processing did not finish in time')

    def payload(self, **updates):
        data = {
            'company_name':'Aster Engineering Pvt Ltd', 'country':'India',
            'registration_number':'U12345TG2020PTC123456', 'tax_id':'ABCDE1234F',
            'gstin':'36ABCDE1234F1Z5', 'bank_account_holder':'Aster Engineering Pvt Ltd',
            'account_number':'123456789012', 'ifsc':'SBIN0001234',
            'email':'accounts@example.invalid',
            'documents':['company_registration','tax_certificate','bank_proof']
        }
        data.update(updates)
        return data

    def test_approved_pending_and_duplicate_rejected(self):
        r = self.client.post('/api/submissions', json=self.payload())
        self.assertEqual(r.status_code, 200)
        approved = self.wait_for(r.json()['run_id'])
        self.assertEqual(approved['status'], 'APPROVED')
        self.assertEqual(approved['checks_failed'], 0)

        r = self.client.post('/api/submissions', json=self.payload(company_name='BluePeak Systems', tax_id='BCDEF2345G', gstin='36BCDEF2345G1Z5', bank_account_holder='BluePeak Systems', documents=['company_registration','bank_proof']))
        pending = self.wait_for(r.json()['run_id'])
        self.assertEqual(pending['status'], 'PENDING')

        r = self.client.post('/api/submissions', json=self.payload())
        rejected = self.wait_for(r.json()['run_id'])
        self.assertEqual(rejected['status'], 'REJECTED')

if __name__ == '__main__':
    unittest.main()
