# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from newsroom import ai
from newsroom.core import (
    PublisherArticleParser,
    _primary_link_candidates,
    fetch_publisher_article,
    fetch_google_news,
    _request_with_url,
    process_item,
    require_primary_source_review,
    run_cycle,
    _safe_source_error,
)
from newsroom.db import connect
from newsroom.cli import is_eligible_for_auto_publish


class PrimarySourceExtractionTests(unittest.TestCase):
    def test_nested_related_cards_do_not_leak_links_or_hide_following_article(self):
        parser = PublisherArticleParser()
        parser.feed("<div class='related'><div><a href='https://related.gov/a'>one</a></div>"
                    "<a href='https://related.gov/b'>two</a></div>"
                    "<p><a href='https://www.cbr.ru/decision'>Решение Банка России</a></p>")
        self.assertEqual([link["href"] for link in parser.links], ["https://www.cbr.ru/decision"])

    def test_parser_collects_article_links_but_not_navigation_or_related_cards(self):
        parser = PublisherArticleParser()
        parser.feed("""
            <nav><p><a href="https://nav.gov/skip">official statement</a></p></nav>
            <main><p>Материал с первичной ссылкой
              <a href="https://www.cbr.ru/press/event/?id=1">сообщение Банка России</a>.
            </p><div class="related"><a href="https://related.gov/skip">report</a></div></main>
        """)
        self.assertEqual(len(parser.links), 1)
        self.assertEqual(parser.links[0]["text"], "сообщение Банка России")
        candidates = _primary_link_candidates(parser.links, "https://news.example/article")
        self.assertEqual(candidates, [{"url": "https://www.cbr.ru/press/event/?id=1", "kind": "OFFICIAL"}])

    def test_google_news_keeps_per_article_publisher_failures_for_health(self):
        feed = b"""<rss><channel><item><title>Crypto report</title>
            <link>https://news.google.com/rss/articles/example</link>
            <pubDate>Fri, 25 Sep 2026 12:00:00 GMT</pubDate></item></channel></rss>"""
        with patch("newsroom.core._request", return_value=feed), \
             patch("newsroom.core.decode_google_news_url", return_value="https://ria.ru/example"), \
             patch("newsroom.core.fetch_publisher_article", side_effect=TimeoutError("TLS handshake timed out")):
            items = fetch_google_news("https://news.google.com/rss/search?q=crypto")
        self.assertEqual(items, [])
        self.assertEqual(items.diagnostics, ["NETWORK_TIMEOUT@ria.ru"])

    def test_fetch_reads_official_page_and_returns_its_url_and_text(self):
        article_url = "https://news.example/crypto-story"
        source_url = "https://www.cbr.ru/press/event/?id=1"
        article_body = ("<html><title>Крипторынок</title><h1>Крипторынок</h1>"
                        "<p>Издание сообщает о важном событии на рынке криптовалют. "
                        "Банк России опубликовал <a href='" + source_url + "'>официальное сообщение</a>, "
                        "в котором изложены подробности решения и дата его вступления в силу.</p></html>").encode()
        primary_body = ("<html><title>Сообщение Банка России</title><h1>Решение регулятора</h1>"
                        "<p>Банк России сообщает о новом порядке работы с цифровыми финансовыми активами. "
                        "Документ устанавливает условия доступа участников рынка и срок начала действия.</p></html>").encode()
        responses = [
            (article_body, article_url, "text/html"),
            (primary_body, source_url, "text/html"),
        ]
        with patch("newsroom.core._request_with_url", side_effect=responses):
            article = fetch_publisher_article(article_url, "News example", None)
        self.assertEqual(article["primary_source_url"], source_url)
        self.assertEqual(article["primary_source_type"], "OFFICIAL")
        self.assertIn("условия доступа", article["primary_source_content"])
        self.assertEqual(article["primary_source_title"], "Решение регулятора")
        self.assertEqual(article["primary_source_status"], "READ")

    def test_unreadable_explicit_document_forces_editor_review(self):
        result = require_primary_source_review(
            {"publication_recommendation": "WAIT_FOR_AUTOMATION"}, "UNREADABLE")
        self.assertEqual(result["publication_recommendation"], "WAIT_FOR_AUTOMATION")
        self.assertTrue(result["source_review_required"])

    def test_explicit_do_not_publish_is_not_overridden_by_source_gate(self):
        original = {"publication_recommendation": "DO_NOT_PUBLISH"}
        self.assertIs(require_primary_source_review(original, "UNREADABLE"), original)

    def test_pdf_primary_source_is_read_and_recorded(self):
        url = "https://www.cbr.ru/documents/crypto.pdf"
        extracted = "Решение Банка России устанавливает порядок и сроки применения требований к цифровым финансовым активам. " * 2
        with patch("newsroom.core._request_with_url", return_value=(b"%PDF", url, "application/pdf")), \
             patch("newsroom.core._extract_pdf_text", return_value=extracted):
            article = fetch_publisher_article(url, "Банк России", None)
        self.assertEqual(article["primary_source_status"], "READ")
        self.assertEqual(article["primary_source_type"], "OFFICIAL")
        self.assertEqual(article["primary_source_url"], url)
        self.assertIn("сроки применения", article["primary_source_content"])

    def test_network_timeout_does_not_repeat_the_full_wait(self):
        class Headers:
            def get_content_type(self):
                return "text/html"
        class Response:
            headers = Headers()
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit): return b"body"
            def geturl(self): return "https://news.example/article"
        with patch("newsroom.core.time.sleep"), \
             patch("newsroom.core.urllib.request.urlopen", side_effect=[TimeoutError("timed out"), Response()]) as opener:
            with self.assertRaises(TimeoutError):
                _request_with_url("https://news.example/article")
        self.assertEqual(opener.call_count, 1)

    def test_network_error_is_logged_without_url_or_response_text(self):
        error = _safe_source_error(RuntimeError("HTTP Error 403: secret payload at https://host/path?token=private"))
        self.assertEqual(error, "HTTP_403")
        self.assertNotIn("private", error)
        self.assertEqual(_safe_source_error(TimeoutError("https://host/private timed out")), "NETWORK_TIMEOUT")

    def test_cycle_reconciles_database_sources_to_current_config(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "newsroom.sqlite3")
            db = connect(db_path)
            db.execute("INSERT INTO sources(name,type,url,active,priority,reputation) VALUES(?,?,?,?,?,?)",
                       ("Removed", "rss", "https://removed.example/feed", 1, 1, "unknown"))
            db.execute("INSERT INTO sources(name,type,url,active,priority,reputation) VALUES(?,?,?,?,?,?)",
                       ("Old name", "rss", "https://kept.example/feed", 1, 1, "unknown"))
            db.commit()
            db.close()
            config = {"newsroom": {"database": db_path}, "sources": [
                {"name": "Updated name", "type": "google_news", "url": "https://kept.example/feed",
                 "active": True, "priority": 2, "reputation": "reputable_media"}
            ]}
            with patch("newsroom.core.fetch_google_news", return_value=[]):
                run_cycle(config)
            db = connect(db_path)
            removed = db.execute("SELECT active FROM sources WHERE url='https://removed.example/feed'").fetchone()
            kept = db.execute("SELECT name,type,active,priority,reputation FROM sources WHERE url='https://kept.example/feed'").fetchone()
            self.assertEqual(removed["active"], 0)
            self.assertEqual(tuple(kept), ("Updated name", "google_news", 1, 2, "reputable_media"))
            db.close()

    def test_official_publisher_article_is_its_own_primary_source(self):
        url = "https://www.cbr.ru/press/event/?id=3"
        body = ("<html><title>Банк России</title><h1>Банк России</h1>"
                "<p>Банк России опубликовал подробное решение о порядке работы рынка цифровых активов и сроках его применения.</p></html>").encode()
        with patch("newsroom.core._request_with_url", return_value=(body, url, "text/html")):
            article = fetch_publisher_article(url, "Банк России", None)
        self.assertEqual(article["primary_source_status"], "READ")
        self.assertEqual(article["primary_source_type"], "OFFICIAL")
        self.assertEqual(article["primary_source_url"], url)
        self.assertIn("сроках его применения", article["primary_source_content"])

    def test_unreadable_official_link_is_not_silently_accepted_as_read(self):
        article_url = "https://news.example/crypto-story"
        source_url = "https://www.cbr.ru/press/event/?id=1"
        article_body = ("<html><p>Подробный материал о крипторынке содержит более ста символов. "
                        "См. <a href='" + source_url + "'>официальное сообщение регулятора</a> "
                        "с условиями принятого решения и сроками его исполнения.</p></html>").encode()
        with patch("newsroom.core._request_with_url", side_effect=[
            (article_body, article_url, "text/html"), OSError("document unavailable")]):
            article = fetch_publisher_article(article_url, "News example", None)
        self.assertEqual(article["primary_source_status"], "UNREADABLE")
        self.assertEqual(article["primary_source_url"], source_url)
        self.assertFalse(article.get("primary_source_content"))

    def test_ai_request_contains_read_primary_source_text_and_status(self):
        editor_result = {"action": "NOISE"}
        response_body = json.dumps({"output": [{"content": [{
            "type": "output_text", "text": json.dumps(editor_result)
        }]}]}).encode()

        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self):
                return response_body

        with patch("newsroom.ai.get_api_key", return_value="test-key"), \
             patch("newsroom.ai.urllib.request.urlopen", return_value=Response()) as request:
            ai.analyze(
                {"title": "Новость", "content": "Текст издателя", "url": "https://news.example/a",
                 "primary_source": {"type": "OFFICIAL", "url": "https://www.cbr.ru/doc",
                                    "title": "Решение", "content": "Текст решения."},
                 "primary_source_status": "READ"},
                {"name": "News", "reputation": "unknown", "priority": 1}, [], {"model": "test"})
        sent = json.loads(request.call_args.args[0].data)
        sent_item = json.loads(sent["input"][0]["content"])["item"]
        self.assertEqual(sent_item["primary_source_status"], "READ")
        self.assertEqual(sent_item["primary_source"]["content"], "Текст решения.")
        self.assertIn("primary_source_status равен UNREADABLE", sent["instructions"])

    def test_relevant_rss_item_reads_article_before_ai_and_records_primary_source(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect(str(Path(directory) / "newsroom.sqlite3"))
            db.execute("INSERT INTO sources(name,type,url,active,priority,reputation) VALUES(?,?,?,?,?,?)",
                       ("News example", "rss", "https://news.example/feed.xml", 1, 1, "reputable_media"))
            source = db.execute("SELECT * FROM sources").fetchone()
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            item = {"url": "https://news.example/story", "title": "Новости криптовалют",
                    "description": "Криптобиржа объявила о новом продукте.",
                    "content": "Короткое RSS-описание.", "published_at": now}
            article = {"url": item["url"], "title": item["title"],
                       "description": item["description"],
                       "content": "Полный текст статьи издателя о продукте и условиях доступа.",
                       "primary_source_url": "https://www.cbr.ru/doc/crypto",
                       "primary_source_title": "Документ регулятора",
                       "primary_source_content": "Банк России устанавливает условия работы с цифровыми активами.",
                       "primary_source_type": "OFFICIAL",
                       "primary_source_publisher": "www.cbr.ru",
                       "primary_source_status": "READ"}
            article["primary_source_content"] += f' Дата сообщения: {now[:10]}.'
            ai_result = {
                "action": "NEW_STORY", "story_id": "", "is_relevant": True,
                "topic_category": "OTHER", "is_concrete": False,
                "implementation_stage": "NONE", "geographic_scope": "RUSSIA",
                "russia_cis_impact": "DIRECT",
                "impact_evidence": "Банк России устанавливает условия работы с цифровыми активами.",
                "importance": "MEDIUM", "freshness": "FRESH", "confidence": 0.9,
                "editorial_check": {"source_matches_event": True, "attribution_preserved": True, "stage_preserved": True, "history_required": False, "history_explained": False, "history_note": "", "headline_main_event": True, "lead_event_first": True, "paragraphs_concise_distinct": True, "no_editorial_process_notes": True}, "headline_ru": "🇷🇺 Регулятор опубликовал документ о цифровых активах",
                "summary_ru": "Регулятор опубликовал документ о цифровых активах.",
                "what_is_new": "Опубликован документ.", "event_status": "DECISION",
                "publication_recommendation": "AUTO_PUBLISH", "facts": [],
            }
            ai_result.update(development_date=now[:10], development_date_evidence=article["primary_source_content"])
            with patch("newsroom.core.fetch_publisher_article", return_value=article) as fetch, \
                 patch("newsroom.core.get_api_key", return_value="test-key"), \
                 patch("newsroom.core.analyze_with_ai", return_value=ai_result) as analyze:
                process_item(db, source, item, 0.35, 700, 48, ai_settings={"model": "test"})
            fetch.assert_called_once()
            sent_item = analyze.call_args.args[0]
            self.assertIn("Полный текст статьи", sent_item["content"])
            self.assertEqual(sent_item["primary_source"]["url"], article["primary_source_url"])
            post = db.execute("SELECT text FROM posts").fetchone()[0]
            self.assertIn(article["primary_source_url"], post)
            db.close()

    def test_process_item_persists_primary_text_sends_it_to_ai_and_cites_it(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect(str(Path(directory) / "newsroom.sqlite3"))
            db.execute("INSERT INTO sources(name,type,url,active,priority,reputation) VALUES(?,?,?,?,?,?)",
                       ("News example", "google_news", "https://news.google.com/test", 1, 1, "unknown"))
            source = db.execute("SELECT * FROM sources").fetchone()
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            item = {
                "url": "https://news.example/story",
                "title": "Криптовалюты: опубликовано решение",
                "description": "Банк опубликовал новое решение о цифровых активах.",
                "content": "В материале описано новое решение и его применение на рынке цифровых активов.",
                "published_at": now,
                "primary_source_url": "https://www.cbr.ru/press/event/?id=2",
                "primary_source_title": "Решение Банка России",
                "primary_source_content": "Банк России установил условия доступа к цифровым активам и дату начала действия.",
                "primary_source_status": "READ",
                "primary_source_type": "OFFICIAL",
                "primary_source_publisher": "www.cbr.ru",
            }
            ai_result = {
                "action": "NEW_STORY", "story_id": "", "is_relevant": True,
                "topic_category": "OTHER", "is_concrete": False,
                "implementation_stage": "NONE", "geographic_scope": "RUSSIA",
                "russia_cis_impact": "DIRECT",
                "impact_evidence": "Банк России установил условия доступа к цифровым активам и дату начала действия.",
                "importance": "MEDIUM", "freshness": "FRESH", "confidence": 0.9,
                "editorial_check": {"source_matches_event": True, "attribution_preserved": True, "stage_preserved": True, "history_required": False, "history_explained": False, "history_note": "", "headline_main_event": True, "lead_event_first": True, "paragraphs_concise_distinct": True, "no_editorial_process_notes": True}, "headline_ru": "🇷🇺 Банк России опубликовал решение о цифровых активах",
                "summary_ru": "Банк России опубликовал решение о цифровых активах.",
                "what_is_new": "Опубликовано решение.", "event_status": "DECISION",
                "publication_recommendation": "AUTO_PUBLISH", "facts": [],
            }
            item["primary_source_content"] += f' Дата сообщения: {now[:10]}.'
            ai_result.update(development_date=now[:10], development_date_evidence=item["primary_source_content"])
            with patch("newsroom.core.get_api_key", return_value="test-key"), \
                 patch("newsroom.core.analyze_with_ai", return_value=ai_result) as analyze:
                process_item(db, source, item, 0.35, 700, 48, ai_settings={"model": "test"})
            saved = db.execute("SELECT primary_source_json FROM items").fetchone()[0]
            primary = json.loads(saved)
            self.assertEqual(primary["url"], item["primary_source_url"])
            self.assertIn("дату начала", primary["content"])
            sent_primary = analyze.call_args.args[0]["primary_source"]
            self.assertEqual(sent_primary["type"], "OFFICIAL")
            post = db.execute("SELECT text,fact_check_result FROM posts").fetchone()
            self.assertIn(item["primary_source_url"], post["text"])
            self.assertIn(item["primary_source_url"], post["fact_check_result"])
            provenance = json.loads(post["fact_check_result"])["primary_source"]
            self.assertEqual(provenance["type"], "OFFICIAL")
            self.assertEqual(len(provenance["content_sha256"]), 64)
            db.close()

    def test_unreadable_rss_article_is_triaged_but_held_without_post(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect(str(Path(directory) / "newsroom.sqlite3"))
            db.execute("INSERT INTO sources(name,type,url,active,priority,reputation) VALUES(?,?,?,?,?,?)",
                       ("News example", "rss", "https://news.example/feed.xml", 1, 1, "reputable_media"))
            source = db.execute("SELECT * FROM sources").fetchone()
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            item = {"url": "https://news.example/story", "title": "Новости криптовалют",
                    "description": "Криптобиржа объявила о новом продукте.",
                    "content": "Короткое RSS-описание.", "published_at": now}
            ai_result = {
                "action": "NEW_STORY", "story_id": "", "is_relevant": True,
                "topic_category": "OTHER", "is_concrete": False,
                "implementation_stage": "NONE", "geographic_scope": "RUSSIA",
                "importance": "MEDIUM", "freshness": "FRESH", "confidence": 0.9,
                "editorial_check": {"source_matches_event": True, "attribution_preserved": True, "stage_preserved": True, "history_required": False, "history_explained": False, "history_note": "", "headline_main_event": True, "lead_event_first": True, "paragraphs_concise_distinct": True, "no_editorial_process_notes": True}, "headline_ru": "🇷🇺 Криптобиржа объявила о продукте",
                "summary_ru": "Криптобиржа объявила о новом продукте.",
                "what_is_new": "Объявлен продукт.", "event_status": "ANNOUNCEMENT",
                "publication_recommendation": "AUTO_PUBLISH", "facts": [],
            }
            with patch("newsroom.core.fetch_publisher_article", side_effect=TimeoutError()), \
                 patch("newsroom.core.get_api_key", return_value="test-key"), \
                 patch("newsroom.core.analyze_with_ai", return_value=ai_result) as analyze:
                process_item(db, source, item, 0.35, 700, 48, ai_settings={"model": "test"})
            analyze.assert_called_once()
            self.assertIsNone(analyze.call_args.args[0]["primary_source"])
            held = db.execute("SELECT disposition,primary_source_json FROM items").fetchone()
            self.assertEqual(held["disposition"], "PRIMARY_RETRY")
            self.assertEqual(json.loads(held["primary_source_json"])["status"], "ARTICLE_UNREADABLE")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 0)
            db.close()

    def test_existing_sqlite_database_gets_primary_source_column(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            import sqlite3
            old = sqlite3.connect(path)
            old.execute("""CREATE TABLE items (
                item_id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL,
                url TEXT NOT NULL, canonical_url TEXT NOT NULL, title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '', author TEXT,
                published_at TEXT, updated_at TEXT, discovered_at TEXT NOT NULL, processed_at TEXT,
                content_hash TEXT NOT NULL, title_hash TEXT NOT NULL,
                disposition TEXT NOT NULL DEFAULT 'PENDING', story_id INTEGER
            )""")
            old.execute("""INSERT INTO items(item_id,source_id,url,canonical_url,title,discovered_at,
                         content_hash,title_hash) VALUES (1,1,'u','u','legacy','now','body','title')""")
            old.commit()
            old.close()
            db = connect(str(path))
            columns = {row[1] for row in db.execute("PRAGMA table_info(items)")}
            self.assertIn("primary_source_json", columns)
            self.assertEqual(db.execute("SELECT primary_source_json FROM items WHERE item_id=1").fetchone()[0], "{}")
            db.close()


if __name__ == "__main__":
    unittest.main()
