import tempfile
import unittest
from clam_gui.scan_output import ScanOutput
from clam_gui.results import summarize,output_was_truncated
from clam_gui.core import Store


class ScanOutputSummaryTests(unittest.TestCase):
    def capture(self,data,step=None):
        with ScanOutput() as log:
            if step:
                for index in range(0,len(data),step):log.write(data[index:index+step])
            else:log.write(data)
            text=log.finish()
        return log,text

    def test_chunk_boundaries_crlf_and_eof_are_counted_without_paths(self):
        data=(b'/synthetic/empty: Empty file\r\n'
              b'C:\\synthetic\\name: with colon: Empty file\n'
              b'/synthetic/link: Symbolic link')
        log,text=self.capture(data,step=7)
        self.assertEqual(log.skip_counts,{'Empty file':2,'Symbolic link':1})
        self.assertIn('空ファイルのため検査省略：2件',text)
        self.assertIn('リンクを追跡しない設定のため検査省略：1件',text)
        self.assertNotIn('/synthetic/',text);self.assertNotIn('C:\\synthetic',text)
        self.assertFalse(log.omitted);self.assertFalse(log.issue)

    def test_large_volume_of_normal_skips_does_not_displace_evidence_or_claim_truncation(self):
        with ScanOutput() as log:
            for _ in range(50):log.write(b'/synthetic/empty-file: Empty file\n'*1000)
            for _ in range(5):log.write(b'/synthetic/link: Symbolic link\n'*1000)
            log.write(b'Scanned files: 1\nInfected files: 0\nTotal errors: 0\n')
            text=log.finish()
        self.assertEqual(log.skip_counts,{'Empty file':50000,'Symbolic link':5000})
        self.assertFalse(output_was_truncated(text));self.assertNotIn('WARNING:',text)
        self.assertTrue(summarize(0,text).assessment.clean)
        self.assertLess(len(text.encode()),1024)
        self.assertLessEqual(len(log.tail),log.TAIL)

    def test_detection_warning_and_error_details_remain_and_round_trip_through_history(self):
        data=(b'/synthetic/first: Test-Signature FOUND\n'+
              b'/synthetic/empty: Empty file\n'*40001+
              b'/synthetic/link: Symbolic link\n'
              b'/synthetic/large: Heuristics.Limits.Exceeded.MaxFileSize FOUND\n'
              b'/synthetic/unreadable: Permission denied ERROR\n'
              b'WARNING: example diagnostic\n'
              b'----------- SCAN SUMMARY -----------\nInfected files: 1\nTotal errors: 1\n')
        log,text=self.capture(data)
        for value in ('/synthetic/first: Test-Signature FOUND',
                      '/synthetic/large: Heuristics.Limits.Exceeded.MaxFileSize FOUND',
                      '/synthetic/unreadable: Permission denied ERROR','WARNING: example diagnostic'):
            self.assertEqual(text.count(value),1)
        self.assertEqual(log.threat_count,1);self.assertEqual(log.retained_threats,1)
        status=summarize(2,text);self.assertTrue(status.assessment.detected)
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory);store.save('now','target',status,text)
            saved=store.recent()[0][3]
        self.assertEqual(saved,text)
        self.assertIn('空ファイルのため検査省略：40001件',saved)

    def test_similar_warning_error_detection_and_unknown_lines_are_not_aggregated(self):
        lines=(b'WARNING: /synthetic/empty: Empty file\n'
               b'/synthetic/broken: Empty file ERROR\n'
               b'/synthetic/name: Empty file: Test-Signature FOUND\n'
               b'unknown relative diagnostic: Symbolic link\n'
               b'/synthetic/new-format: Something else\n')
        log,text=self.capture(lines)
        self.assertEqual(log.skip_counts,{'Empty file':0,'Symbolic link':0})
        self.assertTrue(log.detected);self.assertTrue(log.issue)
        for line in lines.decode().splitlines():self.assertIn(line,text)
        self.assertNotIn('通常の検査省略',text)

    def test_long_line_fragment_is_never_mistaken_for_a_normal_skip(self):
        data=b'ERROR: '+b'x'*200000+b'/synthetic/empty: Empty file\n'
        log,text=self.capture(data,step=32768)
        self.assertEqual(log.skip_counts['Empty file'],0)
        self.assertTrue(log.omitted);self.assertIn('WARNING:',text)
        self.assertIn('Empty file',text)
        self.assertLessEqual(len(log.pending),log.LINE)

    def test_suppression_keeps_evidence_offsets_correct_when_other_output_exceeds_tail(self):
        with ScanOutput() as log:
            log.write(b'/synthetic/first: Test-Signature FOUND\n')
            for _ in range(50):
                log.write(b'/synthetic/empty: Empty file\n'*500)
                log.write(b'ordinary diagnostic\n'*2000)
            log.write(b'Infected files: 1\n')
            text=log.finish()
        self.assertEqual(text.count('/synthetic/first: Test-Signature FOUND'),1)
        self.assertEqual(log.retained_threats,1)
        self.assertIn('空ファイルのため検査省略：25000件',text)
        self.assertIn('Infected files: 1',text)
        self.assertLess(len(text.encode()),2*1024*1024)
