import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.openai_backend_api import ChatRequirements, OpenAIBackendAPI
from services.protocol import conversation
from services.protocol import openai_v1_response


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


class ConversationContinuationTests(unittest.TestCase):
    def tearDown(self):
        openai_v1_response._clear_response_continuations()

    def test_conversation_payload_only_adds_cursor_for_continuation(self):
        backend = bare_backend()
        messages = [{"role": "user", "content": "next question"}]

        first = backend._conversation_payload(messages, "gpt-test", "Asia/Shanghai")
        continued = backend._conversation_payload(
            messages,
            "gpt-test",
            "Asia/Shanghai",
            conversation_id="conv-1",
            parent_message_id="assistant-1",
        )

        self.assertNotIn("conversation_id", first)
        self.assertTrue(first["parent_message_id"])
        self.assertEqual(continued["conversation_id"], "conv-1")
        self.assertEqual(continued["parent_message_id"], "assistant-1")

    def test_parser_exposes_last_visible_assistant_message_id(self):
        payload = json.dumps({
            "conversation_id": "conv-1",
            "message": {
                "id": "assistant-1",
                "author": {"role": "assistant"},
                "recipient": "all",
                "channel": "final",
                "content": {"content_type": "text", "parts": ["answer"]},
            },
        })

        events = list(conversation.iter_conversation_payloads(iter([payload, "[DONE]"])))

        self.assertEqual(events[-1]["conversation_id"], "conv-1")
        self.assertEqual(events[-1]["last_assistant_message_id"], "assistant-1")

    def test_previous_response_reuses_conversation_and_account(self):
        backend = SimpleNamespace(access_token="token-a")
        requests = []

        def fake_stream(active_backend, request):
            requests.append((active_backend, request))
            request.response_conversation_id = "conv-1"
            request.response_parent_message_id = f"assistant-{len(requests)}"
            yield f"answer-{len(requests)}"

        body = {"model": "gpt-test", "input": "question one", "stream": True}
        with patch.object(openai_v1_response, "stream_text_deltas", fake_stream), \
             patch.object(openai_v1_response, "text_backend_for_access_token", return_value=backend), \
             patch.object(openai_v1_response, "count_message_text_tokens", return_value=1), \
             patch.object(openai_v1_response, "count_message_image_tokens", return_value=0), \
             patch.object(openai_v1_response, "count_text_tokens", return_value=1):
            first_events = list(openai_v1_response.stream_text_response(backend, body))
            first_response_id = first_events[0]["response"]["id"]
            second_events = list(openai_v1_response.stream_text_response(None, {
                "model": "gpt-test",
                "input": "question two",
                "stream": True,
                "previous_response_id": first_response_id,
            }))

        self.assertEqual(second_events[-1]["type"], "response.completed")
        self.assertIs(requests[0][0], backend)
        self.assertIs(requests[1][0], backend)
        self.assertEqual(requests[0][1].conversation_id, "")
        self.assertEqual(requests[1][1].conversation_id, "conv-1")
        self.assertEqual(requests[1][1].parent_message_id, "assistant-1")
        self.assertNotIn(first_response_id, openai_v1_response._response_continuations)

    def test_failed_continuation_releases_previous_response_id(self):
        backend = SimpleNamespace(access_token="token-a")
        calls = 0

        def fake_stream(_active_backend, request):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("temporary failure")
            request.response_conversation_id = "conv-1"
            request.response_parent_message_id = f"assistant-{calls}"
            yield "answer"

        body = {"model": "gpt-test", "input": "question one", "stream": True}
        patches = (
            patch.object(openai_v1_response, "stream_text_deltas", fake_stream),
            patch.object(openai_v1_response, "text_backend_for_access_token", return_value=backend),
            patch.object(openai_v1_response, "count_message_text_tokens", return_value=1),
            patch.object(openai_v1_response, "count_message_image_tokens", return_value=0),
            patch.object(openai_v1_response, "count_text_tokens", return_value=1),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            first_events = list(openai_v1_response.stream_text_response(backend, body))
            previous_response_id = first_events[0]["response"]["id"]
            with self.assertRaisesRegex(RuntimeError, "temporary failure"):
                list(openai_v1_response.stream_text_response(None, {
                    **body,
                    "input": "question two",
                    "previous_response_id": previous_response_id,
                }))
            retry_events = list(openai_v1_response.stream_text_response(None, {
                **body,
                "input": "question two retry",
                "previous_response_id": previous_response_id,
            }))

        self.assertEqual(retry_events[-1]["type"], "response.completed")

    def test_independent_first_requests_get_distinct_continuations(self):
        backend = SimpleNamespace(access_token="token-a")
        cursor = 0

        def fake_stream(_active_backend, request):
            nonlocal cursor
            cursor += 1
            request.response_conversation_id = f"conv-{cursor}"
            request.response_parent_message_id = f"assistant-{cursor}"
            yield "answer"

        body = {"model": "gpt-test", "input": "same question", "stream": True}
        with patch.object(openai_v1_response, "stream_text_deltas", fake_stream), \
             patch.object(openai_v1_response, "count_message_text_tokens", return_value=1), \
             patch.object(openai_v1_response, "count_message_image_tokens", return_value=0), \
             patch.object(openai_v1_response, "count_text_tokens", return_value=1):
            first = list(openai_v1_response.stream_text_response(backend, body))[0]["response"]["id"]
            second = list(openai_v1_response.stream_text_response(backend, body))[0]["response"]["id"]

        self.assertNotEqual(first, second)
        self.assertEqual(openai_v1_response._response_continuations[first].conversation_id, "conv-1")
        self.assertEqual(openai_v1_response._response_continuations[second].conversation_id, "conv-2")


if __name__ == "__main__":
    unittest.main()
