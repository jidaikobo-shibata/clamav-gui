from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from clam_gui.mailboxes import is_mbox, mailbox_findings, mailbox_guidance, MAILBOX_NOTE
from clam_gui.core import Plan, Store, scan
from clam_gui.options import DEFAULTS

MBOX = (b'From - Mon Nov 16 12:34:56 2026\n'
        b'From: example@example.invalid\nDate: Mon, 16 Nov 2026 12:34:56 +0900\n'
        b'Subject: private subject never logged\n\nmessage body\n')


class MailboxTests(unittest.TestCase):
    def test_content_not_filename_and_warning_filter(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'INBOX'
            path.write_text('ordinary text')
            self.assertFalse(is_mbox(path))
            self.assertEqual(mailbox_findings(f'{path}: Test FOUND', [root]), [])
            path.write_bytes(MBOX)
            self.assertTrue(is_mbox(path))
            self.assertEqual(mailbox_findings(f'{path}: Heuristics.Limits.Exceeded.MaxFileSize FOUND', [root]), [])
            self.assertEqual(len(mailbox_findings(f'{path}: Test FOUND\n{path}: Another FOUND', [root])), 1)
            self.assertEqual(mailbox_findings(f'{path}: Test FOUND', [Path(root)/'elsewhere']), [])

    def test_thunderbird_and_generic_guidance_do_not_log_headers(self):
        with tempfile.TemporaryDirectory() as root:
            profile = Path(root)/'profile'
            mail = profile/'ImapMail'/'server'
            mail.mkdir(parents=True)
            path = mail/'INBOX'
            path.write_bytes(MBOX)
            generic = mailbox_guidance(mailbox_findings(f'{path}: Test FOUND', [root]))
            self.assertNotIn('Thunderbird', generic)
            (profile/'prefs.js').touch()
            findings = mailbox_findings(f'{path}: Test FOUND', [root])
            guidance = mailbox_guidance(findings)
            self.assertIn('Thunderbird', guidance)
            self.assertIn('アドオン', guidance)
            self.assertIn('特定できていません', guidance)
            self.assertNotIn('private subject', guidance)
            self.assertNotIn('example@example.invalid', guidance)

    def test_symlink_is_not_opened(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'mailbox'
            path.write_bytes(MBOX)
            link = Path(root)/'link'
            link.symlink_to(path)
            self.assertFalse(is_mbox(link))

    def test_scan_saves_guidance_and_count_of_files(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'mailbox'
            path.write_bytes(MBOX)
            store = Store(Path(root)/'data')
            def fake_scan(command, **kwargs):
                kwargs['stdout'].write((f'{path}: Test FOUND\nInfected files: 1\n').encode())
                process = unittest.mock.Mock()
                process.wait.return_value = 1
                return process
            with patch('clam_gui.core.shutil.which', return_value='/usr/bin/clamscan'), \
                 patch('clam_gui.core.supported_options', return_value=set(DEFAULTS)), \
                 patch('clam_gui.core.subprocess.Popen', side_effect=fake_scan):
                status, output = scan(Plan('test', [root], []), store, lock_path=Path(root)/'lock')
            self.assertIn(MAILBOX_NOTE, status)
            self.assertIn('保存ファイル：1件', status)
            self.assertIn('保存ファイル自体は削除せず', output)
            self.assertIn(MAILBOX_NOTE, store.recent()[0][3])
