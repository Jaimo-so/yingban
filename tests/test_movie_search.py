from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import Mock, patch

import test_product as fixtures
from integrations import InternetRuntime


class MovieSearchTests(fixtures.ProductFixture):
    def setUp(self):
        super().setUp()
        self.internet = InternetRuntime(self.settings, self.store)
        self.internet.save_config({
            'tmdb_api_key': 'test-key', 'web_search_api_key': 'test-search',
            'web_search_provider': 'bocha',
        })

    @staticmethod
    def record(sid, year=0, date='', poster=''):
        return {
            'id': f'douban-{sid}', 'external_ids': {'douban': str(sid)},
            'title_zh': '奥德赛', 'title_original': 'The Odyssey',
            'aliases': [], 'year': year, 'release_date': date,
            'poster_url': poster, 'summary': '电影简介', 'popularity_rank': int(sid),
        }

    def test_catalog_sorts_dates_before_limit_and_puts_unknown_last(self):
        records = [self.record(1, 1997), self.record(2, 2016, '2016-04-24'),
                   self.record(3, 2026), self.record(4, 2016, '2016-10-12'),
                   self.record(5)]
        self.store.upsert_movies(records)
        self.assertEqual([r['id'] for r in self.catalog.search('奥德赛', 4, newest_first=True)],
                         ['douban-3', 'douban-4', 'douban-2', 'douban-1'])

    def test_tmdb_sorts_before_truncating(self):
        self.internet._tmdb_get = Mock(return_value={'results': [
            {'id': 1, 'title': '奥德赛', 'release_date': '1997-05-18'},
            {'id': 2, 'title': '奥德赛', 'release_date': '2026-07-17'},
        ]})
        self.assertEqual(self.internet.search_movies('奥德赛', 1)[0]['id'], 'tmdb-2')

    def test_douban_new_year_is_enriched_before_limit(self):
        self.internet._tmdb_get = Mock(side_effect=RuntimeError('unavailable'))
        self.internet.web_search = Mock(return_value=[
            {'title': '奥德赛 (豆瓣)', 'url': 'https://movie.douban.com/subject/1/',
             'snippet': '上映日期: 1997-05-18'},
            {'title': '奥德赛 (豆瓣)', 'url': 'https://movie.douban.com/subject/2/',
             'snippet': '暂无上映日期'},
        ])
        self.internet._fetch_douban_suggestions = Mock(return_value=[
            {'id': '2', 'type': 'movie', 'year': '2026',
             'img': 'https://img9.doubanio.com/view/photo/s_ratio_poster/public/p2933569626.jpg'},
        ])
        result = self.internet.search_movies('奥德赛', 1)
        self.assertEqual(result[0]['id'], 'douban-2')
        self.assertEqual(result[0]['year'], 2026)
        self.assertEqual(self.store.movie('douban-2')['release_date'], '2026')
        self.internet._fetch_douban_suggestions.assert_called_once_with('奥德赛')

    def test_existing_poster_does_not_prevent_unknown_year_repair(self):
        record = self.record(2, poster='/api/posters/douban/p2933569626.jpg')
        self.store.upsert_movies([record])
        self.internet._fetch_douban_suggestions = Mock(return_value=[
            {'id': 'wrong', 'type': 'movie', 'year': '2099'},
            {'id': '2', 'type': 'movie', 'year': '2026', 'img': 'https://evil.example/p.jpg'},
        ])
        result = self.internet.hydrate_douban_posters([record])
        self.assertEqual(result[0]['year'], 2026)
        self.assertEqual(result[0]['poster_url'], record['poster_url'])
        self.assertEqual(record['year'], 0)
        self.assertEqual(self.store.movie(record['id'])['year'], 2026)

    def test_concurrent_identical_queries_share_one_request(self):
        entered, release = threading.Event(), threading.Event()
        def fetch(_title):
            entered.set()
            self.assertTrue(release.wait(2))
            return [{'id': '2', 'type': 'movie'}]
        self.internet._fetch_douban_suggestions = Mock(side_effect=fetch)
        with ThreadPoolExecutor(max_workers=4) as pool:
            first = pool.submit(self.internet._douban_suggestions, '奥德赛')
            self.assertTrue(entered.wait(2))
            others = [pool.submit(self.internet._douban_suggestions, '奥德赛') for _ in range(3)]
            release.set()
            self.assertTrue(all(f.result() == first.result() for f in others))
        self.internet._fetch_douban_suggestions.assert_called_once()

    def test_failed_lookup_is_cached_then_retried_after_expiry(self):
        self.internet._fetch_douban_suggestions = Mock(side_effect=[RuntimeError('offline'), []])
        with patch('integrations.time.monotonic', return_value=100):
            self.assertEqual(self.internet._douban_suggestions('奥德赛'), [])
            self.assertEqual(self.internet._douban_suggestions('奥德赛'), [])
        self.internet._fetch_douban_suggestions.assert_called_once()
        with patch('integrations.time.monotonic', return_value=161):
            self.assertEqual(self.internet._douban_suggestions('奥德赛'), [])
        self.assertEqual(self.internet._fetch_douban_suggestions.call_count, 2)

    def test_tmdb_failure_cooldown_recovers_and_configuration_resets_it(self):
        self.internet._tmdb_get = Mock(side_effect=RuntimeError('offline'))
        self.internet.web_search = Mock(return_value=[])
        with patch('integrations.time.monotonic', return_value=100):
            for query in ['奥德赛', '新片']:
                with self.assertRaises(RuntimeError):
                    self.internet.search_movies(query)
        self.internet._tmdb_get.assert_called_once()
        self.assertEqual(self.internet._tmdb_get.call_args.kwargs['timeout_seconds'], 2.0)
        with patch('integrations.time.monotonic', return_value=161):
            with self.assertRaises(RuntimeError):
                self.internet.search_movies('新片')
        self.assertEqual(self.internet._tmdb_get.call_count, 2)
        self.internet.save_config({'tmdb_api_key': 'new-test-key'})
        self.assertEqual(self.internet._tmdb_search_retry_at, 0)

    def test_poster_falls_back_from_html_to_valid_jpeg_without_redirects(self):
        class Response:
            def __init__(self, host, content, content_type):
                self.host, self.content = host, content
                self.headers = {'Content-Type': content_type}
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, limit):
                self.read_limit = limit
                return self.content
            def geturl(self): return f'https://{self.host}/view/photo/s_ratio_poster/public/p2933569626.jpg'
        html = Response('img1.doubanio.com', b'<script>challenge</script>', 'text/html')
        jpeg = Response('img3.doubanio.com', b'\xff\xd8real-image', 'image/jpeg')
        opener = Mock()
        opener.open.side_effect = [html, jpeg]
        with patch('integrations.urllib.request.build_opener', return_value=opener):
            data, kind = self.internet.fetch_douban_poster('p2933569626.jpg')
        self.assertEqual((data, kind), (b'\xff\xd8real-image', 'image/jpeg'))
        self.assertEqual([c.args[0].full_url.split('/')[2] for c in opener.open.call_args_list],
                         ['img1.doubanio.com', 'img3.doubanio.com'])
        self.assertEqual(html.read_limit, 2_000_001)
        self.assertEqual(jpeg.read_limit, 2_000_001)
