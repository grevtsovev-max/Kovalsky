import unittest
from newsroom import dashboard


class GreenCabinetTests(unittest.TestCase):
    def test_active_page_preserves_approved_green_shell(self):
        for marker in ('data-design="materials-path-v2"', '--blue:#087f73',
                       '--bg:#eef2f1', 'class="side"', 'class="nav"',
                       'Материалы', 'Публикации', 'Источники', 'Расходы'):
            with self.subTest(marker=marker):
                self.assertIn(marker, dashboard.PAGE)

    def test_shell_preserves_editorial_script_and_content(self):
        from newsroom.dashboard_shell import green_shell
        source = '<style>old</style><body><main><div id="content">Saved materials</div></main><script>editorialActions()</script></body>'
        result = green_shell(source)
        self.assertIn('<script>editorialActions()</script>', result)
        self.assertIn('<div id="content">Saved materials</div>', result)
        self.assertEqual(green_shell(result), result)
