import threading
import time
import unittest
from unittest.mock import patch

from services.openai_backend_api import ChatRequirements, OpenAIBackendAPI
from services.protocol import conversation


def bare_backend() -> OpenAIBackendAPI:
    backend = object.__new__(OpenAIBackendAPI)
    backend._requirements_cache_lock = threading.Lock()
    backend._prefetched_requirements = None
    backend._prefetched_requirements_at = 0.0
    backend._requirements_prefetching = False
    return backend


class RequirementsPrefetchTests(unittest.TestCase):
    def test_prefetched_token_is_consumed_only_once(self):
        backend = bare_backend()
        prefetched = ChatRequirements(token="prefetched")
        backend._prefetched_requirements = prefetched
        backend._prefetched_requirements_at = time.monotonic()
        backend._fetch_chat_requirements = lambda: ChatRequirements(token="fresh")

        self.assertIs(backend._get_chat_requirements(), prefetched)
        self.assertEqual(backend._get_chat_requirements().token, "fresh")

    def test_expired_prefetch_falls_back_to_fresh_token(self):
        backend = bare_backend()
        backend._prefetched_requirements = ChatRequirements(token="expired")
        backend._prefetched_requirements_at = 0.0
        backend._fetch_chat_requirements = lambda: ChatRequirements(token="fresh")

        self.assertEqual(backend._get_chat_requirements().token, "fresh")

    def test_async_prefetch_coalesces_concurrent_starts(self):
        backend = bare_backend()
        fetched = threading.Event()
        calls = []

        def fetch():
            calls.append(1)
            fetched.set()
            return ChatRequirements(token="next")

        backend._fetch_chat_requirements = fetch
        backend._prefetch_chat_requirements_async()
        backend._prefetch_chat_requirements_async()
        self.assertTrue(fetched.wait(1))
        for _ in range(100):
            with backend._requirements_cache_lock:
                if not backend._requirements_prefetching:
                    break
            time.sleep(0.01)

        self.assertEqual(len(calls), 1)
        self.assertEqual(backend._get_chat_requirements().token, "next")


class TextBackendPoolTests(unittest.TestCase):
    def tearDown(self):
        with conversation._text_backend_pool_lock:
            conversation._text_backend_pool.clear()

    def test_same_account_reuses_backend(self):
        class FakeBackend:
            def __init__(self, access_token):
                self.access_token = access_token
                self._closed = False

        with patch.object(conversation, "OpenAIBackendAPI", FakeBackend):
            first = conversation._pooled_text_backend("token-a")
            second = conversation._pooled_text_backend("token-a")

        self.assertIs(first, second)


if __name__ == "__main__":
    unittest.main()
