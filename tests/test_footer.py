import unittest
from clam_gui.footer import compact_engine_label


class FooterTests(unittest.TestCase):
    def test_package_revisions_are_hidden_in_short_label(self):
        for version, expected in [('1.5.4+dfsg-0ubuntu0.26.04.1', '1.5.4'),
                                  ('2:1.5.4-2', '1.5.4'), ('1.6.0~rc1-1', '1.6.0~rc1')]:
            with self.subTest(version=version):
                for state in ('更新あり → 1.6.0', '更新なし（APT情報）', '更新状態不明'):
                    self.assertEqual(compact_engine_label(f'ClamAV本体（APT） {version}：{state}'),
                                     'ClamAV ' + expected)

    def test_unknown_and_uninstalled_are_not_shown_as_installed_version(self):
        self.assertEqual(compact_engine_label('ClamAV本体：更新状態を確認できません'), 'ClamAV：確認不可')
        self.assertEqual(compact_engine_label('ClamAV本体：APT未導入'), 'ClamAV：APT未導入')
