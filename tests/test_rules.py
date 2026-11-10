import unittest
from backend.main import normalize, classify_doc

class RuleHelperTests(unittest.TestCase):
    def test_normalize_removes_punctuation_and_spaces(self):
        self.assertEqual(normalize('A.B.C. Pvt. Ltd.'), 'ABCPVTLTD')

    def test_document_classification(self):
        self.assertEqual(classify_doc('GST_Certificate.pdf'), 'tax_certificate')
        self.assertEqual(classify_doc('cancelled_cheque.png'), 'bank_proof')
        self.assertEqual(classify_doc('Certificate_of_Incorporation.pdf'), 'company_registration')
        self.assertEqual(classify_doc('misc.pdf'), 'other')

if __name__ == '__main__':
    unittest.main()
