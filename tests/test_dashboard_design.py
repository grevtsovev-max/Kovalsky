import unittest

from newsroom import dashboard


class DashboardDesignTests(unittest.TestCase):
    def test_materials_path_v2_is_the_only_pipeline_interface(self):
        page = dashboard.PAGE
        required = [
            'data-design="materials-path-v2"',
            'Путь материалов',
            'Путь материалов v2',
            'id="pipeline-funnel" class="path-strip"',
            "received:'Собрано'",
            "analyzed:'Разобрано ИИ'",
            "drafted:'Пост сохранён'",
            "first_filter:'Отобрано по теме'",
            "primary_read:'Текст получен'",
            "checked:'Текст проверен'",
            "published:'Опубликовано'",
            '<option value="48" selected>48 часов</option>',
            'pipeline-evidence-totals',
            'Каждый следующий этап включает только материалы',
        ]
        for marker in required:
            with self.subTest(marker=marker):
                self.assertIn(marker, page)

        forbidden = [
            'Где сейчас каждый материал',
            'Сбор → отбор по теме →',
            '${n+1}.',
            'ИИ · один запрос',
            'id="pipeline-stages"',
            'Обработка материалов',
            'pipelineDescriptions',
            "first_filter:'Первый фильтр'",
            "primary_read:'Прочитано'",
            "published:'Отправка и квитанция'",
        ]
        for marker in forbidden:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, page)

    def test_script_references_existing_elements_after_retired_views_removed(self):
        import re
        from html.parser import HTMLParser

        class Elements(HTMLParser):
            def __init__(self):
                super().__init__()
                self.ids = set()

            def handle_starttag(self, tag, attrs):
                self.ids.update(value for key, value in attrs if key == 'id')

        page = dashboard.PAGE
        elements = Elements()
        elements.feed(page)
        referenced = set(re.findall(r"document.getElementById\(['\"]([^'\"]+)['\"]\)", page))
        self.assertEqual(referenced - elements.ids, set())
        # The published list must keep its renderer after old editor controls go.
        self.assertIn('function postCard(p)', page)
        self.assertIn('postText(p.text)', page)
        self.assertIn('postMetricsHtml(p)', page)


if __name__ == "__main__":
    unittest.main()
