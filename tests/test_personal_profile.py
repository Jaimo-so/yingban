from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from app import build_context
from fastapi_app import create_fastapi_app
from settings import Settings
from storage import Store


class PersonalProfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'profile.db'
        self.context = build_context(Settings(
            database_path=self.path, movie_seed_path=Path(__file__).resolve().parents[1] / 'data/movies.json',
            invite_pepper='fixture-pepper', session_secret='fixture-secret', cookie_secure=False,
            local_open_access=False, model_api_key='', sqlite_journal_mode='WAL', sqlite_vfs='', database_mount='',
        ))
        self.context.internet = None
        self.store = self.context.store
        self.client = TestClient(create_fastapi_app(self.context))
        self.account = self.store.register_with_password('profile_user', 'fixture-password', 30)[0]
        self.other = self.store.register_with_password('other_user', 'fixture-password', 30)[0]
        self.movies = self.store.all_movies()[:5]

    def tearDown(self):
        self.client.close()
        self.directory.cleanup()

    def login(self):
        self.assertEqual(self.client.post('/api/auth/login', json={'username': 'profile_user', 'password': 'fixture-password'}).status_code, 200)

    def test_authentication_and_empty_defaults(self):
        self.assertEqual(self.client.get('/api/profile').status_code, 401)
        self.assertEqual(self.client.post('/api/profile', json={}).status_code, 401)
        self.login()
        response = self.client.get('/api/profile')
        self.assertIn('no-store', response.headers['cache-control'])
        data = response.json()
        self.assertEqual(data['profile']['id'], self.account)
        self.assertEqual(data['profile']['avatar_key'], 'ticket')
        self.assertEqual(data['stats'], {'watched': 0, 'watchlist': 0, 'reflections': 0})
        self.assertEqual(data['recent_records'], [])
        self.assertNotIn('password', str(data))

    def test_save_reload_account_isolation_and_not_agent_memory(self):
        self.login()
        payload = {'nickname': '  散场以后  ', 'bio': '喜欢把电影里的话留一会儿。', 'avatar_key': 'moon', 'account_id': self.other}
        saved = self.client.post('/api/profile', json=payload).json()['profile']
        self.assertEqual(saved['nickname'], '散场以后')
        self.assertEqual(saved['username'], 'profile_user')
        reopened = Store(self.path, 'fixture-pepper', 'fixture-secret')
        reopened.initialize()
        self.assertEqual(reopened.personal_profile(self.account), saved)
        self.assertEqual(reopened.personal_profile(self.other)['nickname'], '')
        self.assertNotIn('nickname', reopened.account_profile(self.account))
        self.assertNotIn('bio', reopened.account_profile(self.account))
        self.assertEqual(self.client.get('/api/me').json()['account']['avatar_key'], 'moon')
        self.assertEqual(self.client.get('/api/profile?account_id='+self.other).json()['profile']['id'], self.account)

    def test_validation_is_atomic_and_clearing_is_supported(self):
        self.login()
        valid = {'nickname': '原名', 'bio': '原简介', 'avatar_key': 'screen'}
        self.client.post('/api/profile', json=valid)
        for field, invalid in [('nickname', '字'*25), ('nickname', []), ('bio', '字'*161), ('bio', None), ('avatar_key', 'https://example.com/a.png'), ('avatar_key', [])]:
            response = self.client.post('/api/profile', json={**valid, field: invalid})
            self.assertEqual(response.status_code, 400, (field, invalid))
            self.assertEqual(self.store.personal_profile(self.account)['nickname'], '原名')
        response = self.client.post('/api/profile', json={'nickname': '', 'bio': '', 'avatar_key': 'ticket'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['profile']['nickname'], '')

    def test_old_schema_migrates_without_losing_taste_or_identity(self):
        movie = self.movies[0]['id']
        self.store.add_onboarding_movie(self.account, movie, 'positive')
        original = self.store.complete_onboarding(self.account)
        with self.store.connect() as connection:
            for column in ('nickname', 'bio', 'avatar_key'):
                connection.execute(f'ALTER TABLE account_profiles DROP COLUMN {column}')
        self.store.initialize()
        self.store.initialize()
        self.assertEqual(self.store.account_profile(self.account), original)
        self.assertEqual(self.store.personal_profile(self.account)['avatar_key'], 'ticket')
        self.assertEqual(self.store.personal_profile(self.account)['username'], 'profile_user')

    def test_only_latest_confirmed_versions_count_and_list(self):
        self.login()
        first, second, third = [m['id'] for m in self.movies[:3]]
        for movie in (first, second):
            self.store.set_movie_state(self.account, movie, 'watched', 'manual')
        self.store.set_movie_state(self.account, third, 'watchlist', 'manual')
        self.store.create_reflection_version(self.account, first, '旧版', 'user', 'confirmed')
        self.store.create_reflection_version(self.account, first, '我确认的新版', 'user', 'confirmed')
        self.store.create_reflection_version(self.account, first, '未确认的私密草稿', 'ai', 'draft')
        self.store.create_reflection_version(self.account, second, '另一份草稿', 'ai', 'draft')
        self.store.set_movie_state(self.other, third, 'watched', 'manual')
        self.store.create_reflection_version(self.other, third, '其他账户的记录', 'user', 'confirmed')
        data = self.client.get('/api/profile').json()
        self.assertEqual(data['stats'], {'watched': 2, 'watchlist': 1, 'reflections': 1})
        self.assertEqual(data['recent_records'][0]['excerpt'], '我确认的新版')
        self.assertNotIn('私密草稿', str(data))
        history = self.client.get('/api/history?state=reflections&limit=1').json()
        self.assertEqual(history['total'], 1)
        self.assertEqual(history['items'][0]['note'], '我确认的新版')
        self.store.delete_reflections(self.account, first)
        self.assertEqual(self.client.get('/api/profile').json()['stats']['reflections'], 0)

    def test_recent_limit_and_pagination(self):
        self.login()
        for index, movie in enumerate(self.movies):
            self.store.set_movie_state(self.account, movie['id'], 'watched', 'manual')
            self.store.create_reflection_version(self.account, movie['id'], f'记录{index}', 'user', 'confirmed')
        home = self.client.get('/api/profile').json()
        self.assertEqual(home['stats']['reflections'], 5)
        self.assertEqual(len(home['recent_records']), 3)
        first = self.client.get('/api/history?state=reflections&limit=3').json()
        second = self.client.get('/api/history?state=reflections&cursor=3&limit=3').json()
        self.assertEqual(first['next_cursor'], 3)
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(len({m['id'] for m in first['items']+second['items']}), 5)

    def test_corrected_taste_disappears_from_summary_and_home(self):
        movie = self.movies[0]['id']
        self.store.add_onboarding_movie(self.account, movie, 'positive')
        profile = self.store.complete_onboarding(self.account)
        self.login()
        for dimension in profile['taste_dimensions']:
            self.assertEqual(self.client.post('/api/taste-profile/corrections', json={'dimension_id': dimension['id']}).status_code, 200)
        taste = self.client.get('/api/profile').json()['taste']
        self.assertTrue(all(d['hidden'] for d in taste['taste_dimensions']))
        self.assertEqual(taste['taste_summary'], '暂时没有保留的口味结论。我们可以继续从具体电影聊起。')
