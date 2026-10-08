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
        ]
        for marker in required:
            with self.subTest(marker=marker):
                self.assertIn(marker, page)

        forbidden = [
            'Где сейчас каждый материал',
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


if __name__ == "__main__":
    unittest.main()
