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
