import base64,json
from pathlib import Path
from tempfile import TemporaryDirectory
from playwright.sync_api import expect
from tests.support import SourceTest

class LabTest(SourceTest):
    def test_download_upload_and_unknown_receipt(self):
        p=self.browser();p.goto(self.base+'/report?id=R-A');expect(p.locator('#report')).to_contain_text('signed')
        with p.expect_download() as got:p.locator('.original').click()
        with TemporaryDirectory() as d:
            f=Path(d)/'original.bin';got.value.save_as(f);self.assertTrue(f.read_bytes().startswith(b'LAB\x00'))
            p.goto(self.base+'/archive');p.set_input_files('#file',str(f));p.click('#upload');expect(p.locator('#upload-result')).to_contain_text('sha256')
            self.control('lose_reply');p.click('#submit');expect(p.locator('#result')).to_contain_text('gateway_after_write');p.click('text=归档历史');expect(p.locator('#history')).to_contain_text('ARC-handoff-1-latency')
        self.control('retest');p.goto(self.base+'/report?id=R-B');expect(p.locator('#report')).to_contain_text('R-A:latency')
        self.control('revoke');p.goto(self.base+'/report?id=R-A');expect(p.locator('#report')).to_contain_text('revoked')
        self.assertEqual(self.request('/archive',{'key':'x','report':'R-J','items':['power']})[0],422)
    def test_partial_independent_projects(self):
        for n in ['bandwidth-1.bin','stability-1.bin']:
            raw=self.request('/original?name='+n)[1]
            self.assertEqual(self.request('/upload',{'name':n,'bytes':base64.b64encode(raw).decode()})[0],200)
        self.control('partial')
        receipt=json.loads(self.request('/archive',{'key':'partial-1','report':'R-C','items':['bandwidth','stability']})[1])
        self.assertEqual(list(receipt['accepted']),['bandwidth']);self.assertEqual(receipt['pending'],['stability'])
        self.control('complete')
        receipt=json.loads(self.request('/archive',{'key':'partial-2','report':'R-C','items':['stability']})[1]);self.assertEqual(list(receipt['accepted']),['stability'])
