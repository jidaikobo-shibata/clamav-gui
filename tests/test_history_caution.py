import tempfile
import unittest
from clam_gui.core import Store,Plan,HistoryEntry
from clam_gui.results import summarize,has_detection,ScanStatus,ResultKind,detection_uncertain


TRUNCATED=('出力が大きいため末尾のみ保存しています。\n'
           '/synthetic/large: Heuristics.Limits.Exceeded.MaxFileSize FOUND\n'
           'Infected files: 57\nTotal errors: 303\n')


class HistoryCautionTests(unittest.TestCase):
    def test_incomplete_detection_information_is_not_claimed_as_confirmed_detection(self):
        for output in (TRUNCATED,'出力が大きいため末尾のみ保存しています。\nInfected files: 57\n'):
            status=summarize(2,output)
            self.assertTrue(status.startswith('要確認・内訳不明'))
            self.assertFalse(status.assessment.detected)
            self.assertEqual(status.assessment.kind,ResultKind.INCOMPLETE)
            self.assertFalse(has_detection('',output))

    def test_full_warning_logs_and_zero_detection_logs_are_not_uncertain(self):
        full='/synthetic/file: Heuristics.Encrypted.OLE2 FOUND\nInfected files: 1\n'
        self.assertFalse(detection_uncertain(full))
        self.assertFalse(detection_uncertain(TRUNCATED.replace('57','0')))
        self.assertFalse(summarize(1,full).assessment.detected)
        entry=HistoryEntry(1,'now','now',0,'','検査不完了',full,'incomplete')
        self.assertEqual(entry.attention_label(),'')

    def test_confirmed_detection_remains_distinct_from_uncertainty(self):
        output=TRUNCATED+'/synthetic/infected: Test-Signature FOUND\n'
        self.assertTrue(summarize(2,output).assessment.detected)
        entry=HistoryEntry(1,'now','now',1,'','検出あり',output,'incomplete')
        self.assertEqual(entry.attention_label(),'検出あり')
        self.assertFalse(entry.is_uncertain())
        self.assertEqual(entry.attention_notice(),'')

    def test_old_zero_flag_is_cautioned_in_headers_and_body_without_rewriting_record(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory);plan=Plan('test',[directory],[]);key=store.save_profile(plan)
            output=TRUNCATED.split('\n')[0]+'\n'+'normal log\n'*50000+'\n'+TRUNCATED.split('\n',1)[1]
            store.save('2026-11-20T02:00:00+09:00',plan.describe(),ScanStatus('検査不完了',ResultKind.INCOMPLETE),output,key)
            with store.connect() as db:before=db.execute('SELECT * FROM scans').fetchall()
            header=store.history_headers(key,plan)[0]
            self.assertEqual(header.output,'');self.assertEqual(header.detected,0)
            self.assertEqual(header.attention_label(),'要確認・内訳不明')
            body=store.history_entry(header.id,key,plan)
            self.assertTrue(body.is_uncertain());self.assertFalse(body.is_detected())
            self.assertIn('「検出なし」とは判断できません',body.attention_notice())
            self.assertEqual(body.output,output)
            with store.connect() as db:self.assertEqual(db.execute('SELECT * FROM scans').fetchall(),before)

    def test_old_null_metadata_also_uses_uncertainty_without_marking_detection_confirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory);plan=Plan('test',[directory],[])
            with store.connect() as db:
                db.execute('INSERT INTO scans(started,target,status,output) VALUES (?,?,?,?)',
                           ('2026-11-20',plan.describe(),'検査不完了',TRUNCATED+'End Date: 2026:11:20 05:52:20\n'))
            header=store.history_headers('not-saved',plan)[0]
            self.assertEqual(header.attention_label(),'要確認・内訳不明')
            self.assertFalse(header.is_detected())
            self.assertIn('05:52:20',header.date_label())

    def test_current_truncation_marker_is_understood_and_normal_filename_is_not(self):
        self.assertTrue(detection_uncertain('WARNING: 出力の一部を省略しました。\nInfected files: 57\n'))
        self.assertTrue(detection_uncertain('出力が大きいため先頭と末尾のみ保存しています。\nInfected files: 57\n'))
        self.assertFalse(detection_uncertain('/tmp/出力が大きいため末尾のみ保存しています。: example FOUND\nInfected files: 57\n'))

    def test_summary_before_long_settings_or_file_list_is_still_cautioned(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory);plan=Plan('test',[directory],[]);key=store.save_profile(plan)
            output=('出力が大きいため末尾のみ保存しています。\n'+'old logs\n'*5000+
                    '\n----------- SCAN SUMMARY -----------\nInfected files: 57\n'+
                    '--- 監視による検査ファイル ---\n'+'long filename\n'*5000)
            store.save('now',plan.describe(),ScanStatus('検査不完了',ResultKind.INCOMPLETE),output,key)
            entry=store.history_headers(key,plan)[0]
            self.assertEqual(entry.attention_label(),'要確認・内訳不明')
            self.assertTrue(store.history_entry(entry.id,key,plan).is_uncertain())
