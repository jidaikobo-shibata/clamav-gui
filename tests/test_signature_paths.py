import unittest
from clam_gui.results import summarize, warning_signature, has_detection


class SignaturePathTests(unittest.TestCase):
    def test_warning_prefix_in_filename_cannot_hide_threat(self):
        for prefix in ('Heuristics.Limits.Exceeded.fake', 'Heuristics.Encrypted.fake',
                       'Heuristics.Broken.fake', 'Heuristics.OLE2.ContainsMacros'):
            for root in ('/tmp/', 'C:\\Users\\'):
                line=root+'name: '+prefix+': Eicar-Signature FOUND'
                self.assertFalse(warning_signature(line))
                self.assertTrue(has_detection('',line))
                status=summarize(1,line+'\nInfected files: 1\n')
                self.assertTrue(status.assessment.detected)
                self.assertNotIn('検査上限',status)

    def test_actual_warning_remains_warning(self):
        line='/tmp/name: strange: Heuristics.Limits.Exceeded.MaxFileSize FOUND'
        self.assertTrue(warning_signature(line))
        self.assertFalse(summarize(1,line).assessment.detected)
        self.assertTrue(summarize(1,line+'\n/tmp/other: Eicar-Signature FOUND').assessment.detected)

    def test_streaming_output_retains_detection_with_warning_like_filename(self):
        from clam_gui.scan_output import ScanOutput
        with ScanOutput() as log:
            log.write(b'/tmp/name: Heuristics.Limits.Exceeded.fake: Eicar-Signature FOUND\n')
            output=log.finish()
            self.assertTrue(log.detected)
            self.assertTrue(summarize(1,output).assessment.detected)

    def test_many_threats_survive_warning_flood_and_history_size_cap(self):
        import tempfile
        from pathlib import Path
        from clam_gui.core import Store
        from clam_gui.scan_output import ScanOutput
        with ScanOutput() as log:
            for _ in range(400):log.write(b'WARNING: '+b'x'*2000+b'\n')
            for index in range(57):log.write(f'/synthetic/{index}: Test-Signature-{index} FOUND\n'.encode())
            for _ in range(80):log.write(b'x'*32768+b'\n')
            text=log.finish()
        self.assertEqual(log.threat_count,57);self.assertEqual(log.retained_threats,57)
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'data')
            store.save('now','test',summarize(1,text),text+'x'*2*1024*1024)
            saved=store.recent()[0][3]
        for index in range(57):self.assertIn(f': Test-Signature-{index} FOUND',saved)
        self.assertLessEqual(len(saved.encode()),2*1024*1024)

    def test_detection_detail_overflow_is_explicit(self):
        from clam_gui.scan_output import ScanOutput
        with ScanOutput() as log:
            log.THREATS=128;log.TAIL=128
            for index in range(57):log.write(f'/synthetic/{index}: Test-Signature-{index} FOUND\n'.encode())
            log.write(b'normal output\n'*100)
            text=log.finish()
        self.assertTrue(log.detected)
        self.assertLess(log.retained_threats,57)
        self.assertIn('脅威の報告57件',text)
        self.assertIn('全件を特定できません',text)
