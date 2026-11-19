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
