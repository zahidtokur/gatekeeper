import asyncio
import os
import time
import uuid
from unittest import IsolatedAsyncioTestCase, SkipTest, TestCase
from unittest.mock import AsyncMock, Mock, call, patch

import httpx
import redis.asyncio as aioredis
from django.conf import settings
from django.http import HttpResponse
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from core.rate_limiter import FixedWindowRateLimiter
from core.views import GatewayProxy


@override_settings(ALLOWED_HOSTS=["testserver"])
class UpstreamConnectionErrorTests(SimpleTestCase):
    """Connection errors and timeouts are mocked, so these tests run without waiting."""

    def setUp(self):
        self.upstream = Mock(spec=httpx.AsyncClient)
        self.upstream.request = AsyncMock()
        client_patch = patch("core.views.get_client", return_value=self.upstream)
        client_patch.start()
        self.addCleanup(client_patch.stop)
        limiter_patch = patch.object(GatewayProxy, "rate_limiters", {
            "GET": Mock(check=AsyncMock(return_value=(False, 0))),
        })
        limiter_patch.start()
        self.addCleanup(limiter_patch.stop)

    async def test_request_error_returns_502(self):
        for error_type in (httpx.ConnectError, httpx.RemoteProtocolError):
            with self.subTest(error_type=error_type.__name__):
                self.upstream.request.side_effect = error_type("connection failed")

                response = await self.async_client.get("/api/orders/1/")

                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json(), {"detail": "Upstream connection error"})

    async def test_connect_timeout_returns_502(self):
        self.upstream.request.side_effect = httpx.ConnectTimeout("connect timed out")

        response = await self.async_client.get("/api/orders/1/")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {"detail": "Connection timed out."})

    async def test_read_timeout_returns_504(self):
        self.upstream.request.side_effect = httpx.ReadTimeout("read timed out")

        response = await self.async_client.get("/api/orders/1/")

        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json(), {"detail": "Upstream timeout"})

    async def test_healthy_upstream_response_is_forwarded(self):
        self.upstream.request.return_value = httpx.Response(200, json={"id": 1})

        response = await self.async_client.get("/api/orders/1/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": 1})


class RedisTestCase(IsolatedAsyncioTestCase):
    """Runs against the real Redis at REDIS_URL.

    Each test gets its own Redis client, because a redis.asyncio client is tied
    to the event loop it was first used in and every test runs in a new loop.
    Limiter names get a random prefix, so tests never touch each other's keys
    or the live gateway's keys; only those keys are deleted afterwards.
    """

    async def asyncSetUp(self):
        self.redis = aioredis.from_url(settings.REDIS_URL)
        self.prefix = f"test-{uuid.uuid4().hex}"
        redis_patch = patch("core.rate_limiter.get_redis", return_value=self.redis)
        redis_patch.start()
        self.addCleanup(redis_patch.stop)
        clock_patch = patch("core.rate_limiter.time.time", return_value=125)
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)

    async def asyncTearDown(self):
        keys = [key async for key in self.redis.scan_iter(f"ratelimit:{self.prefix}-*")]
        if keys:
            await self.redis.delete(*keys)
        await self.redis.aclose()

    def make_limiter(self, name="get", limit=2, window_size=60):
        return FixedWindowRateLimiter(f"{self.prefix}-{name}", limit, window_size)

    def key(self, name, client_id, window_start):
        return f"ratelimit:{self.prefix}-{name}:{client_id}:{window_start}"


class FixedWindowRateLimiterConfigTests(SimpleTestCase):
    def test_rejects_non_positive_configuration(self):
        for limit, window in ((0, 60), (-1, 60), (1, 0), (1, -60)):
            with self.subTest(limit=limit, window=window):
                with self.assertRaises(ValueError):
                    FixedWindowRateLimiter("get", limit, window)


class FixedWindowRateLimiterTests(RedisTestCase):
    async def test_allows_exactly_limit_requests_then_returns_remaining_window(self):
        limiter = self.make_limiter(limit=2)

        self.assertEqual(await limiter.check("client-a"), (False, 0))
        self.assertEqual(await limiter.check("client-a"), (False, 0))
        self.assertEqual(await limiter.check("client-a"), (True, 55))
        self.clock.return_value = 179.9
        self.assertEqual(await limiter.check("client-a"), (True, 1))

    async def test_allowance_resets_at_fixed_window_boundary(self):
        limiter = self.make_limiter(limit=1)
        self.clock.return_value = 179
        self.assertEqual(await limiter.check("client-a"), (False, 0))
        self.assertEqual(await limiter.check("client-a"), (True, 1))

        self.clock.return_value = 180
        self.assertEqual(await limiter.check("client-a"), (False, 0))
        self.assertEqual(await limiter.check("client-a"), (True, 60))

    async def test_clients_have_independent_allowances(self):
        limiter = self.make_limiter(limit=1)

        self.assertEqual(await limiter.check("client-a"), (False, 0))
        self.assertEqual(await limiter.check("client-a"), (True, 55))
        self.assertEqual(await limiter.check("client-b"), (False, 0))
        self.assertEqual(await limiter.check("client-b"), (True, 55))

    async def test_limiters_with_different_names_do_not_share_counters(self):
        get_limiter = self.make_limiter("get", limit=1)
        post_limiter = self.make_limiter("post", limit=1)

        self.assertEqual(await get_limiter.check("client-a"), (False, 0))
        self.assertEqual(await get_limiter.check("client-a"), (True, 55))
        self.assertEqual(await post_limiter.check("client-a"), (False, 0))

    async def test_new_instance_with_same_name_continues_the_count(self):
        """State lives in Redis, not in the object: this is what survives a gateway restart."""
        before_restart = self.make_limiter("get", limit=1)
        self.assertEqual(await before_restart.check("client-a"), (False, 0))

        after_restart = self.make_limiter("get", limit=1)
        self.assertEqual(await after_restart.check("client-a"), (True, 55))

    async def test_counter_is_stored_under_window_key_with_expiration(self):
        limiter = self.make_limiter("get", limit=5, window_size=60)

        await limiter.check("client-a")
        await limiter.check("client-a")

        key = self.key("get", "client-a", 120)
        self.assertEqual(await self.redis.get(key), b"2")
        ttl = await self.redis.ttl(key)
        self.assertGreater(ttl, 0)
        self.assertLessEqual(ttl, 60)

    async def test_concurrent_requests_cannot_exceed_limit(self):
        limiter = self.make_limiter(limit=10)

        results = await asyncio.gather(*(limiter.check("client-a") for _ in range(50)))

        self.assertEqual(results.count((False, 0)), 10)
        self.assertEqual(results.count((True, 55)), 40)


class GatewayRateLimitTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.get_limiter = Mock(check=AsyncMock(return_value=(False, 0)))
        self.post_limiter = Mock(check=AsyncMock(return_value=(False, 0)))
        limiter_patch = patch.object(GatewayProxy, "rate_limiters", {
            "GET": self.get_limiter,
            "POST": self.post_limiter,
        })
        limiter_patch.start()
        self.addCleanup(limiter_patch.stop)
        forward_patch = patch.object(
            GatewayProxy, "_forward", new_callable=AsyncMock,
            return_value=HttpResponse(status=200),
        )
        self.forward = forward_patch.start()
        self.addCleanup(forward_patch.stop)
        self.view = GatewayProxy.as_view()

    async def request(self, method="get", service="users", ip="192.0.2.1", **extra):
        request = getattr(self.factory, method)(
            f"/api/{service}/1/", REMOTE_ADDR=ip, **extra
        )
        return await self.view(request, service_name=service, path="1/")

    async def test_allowed_requests_use_only_the_limiter_for_their_method(self):
        for method, selected, other in (
            ("get", self.get_limiter, self.post_limiter),
            ("post", self.post_limiter, self.get_limiter),
        ):
            with self.subTest(method=method):
                self.get_limiter.reset_mock()
                self.post_limiter.reset_mock()
                self.forward.reset_mock()

                response = await self.request(method)

                self.assertEqual(response.status_code, 200)
                selected.check.assert_awaited_once_with("192.0.2.1")
                other.check.assert_not_awaited()
                self.forward.assert_awaited_once()

    async def test_limited_requests_return_429_and_retry_after_without_forwarding(self):
        for method, limiter in (("get", self.get_limiter), ("post", self.post_limiter)):
            with self.subTest(method=method):
                limiter.check.return_value = (True, 17)

                response = await self.request(method)

                self.assertEqual(response.status_code, 429)
                self.assertEqual(response.headers["Retry-After"], "17")
                self.assertEqual(response.data, {
                    "detail": "Rate limit exceeded. Retry after the time in the Retry-After header."
                })
                self.forward.assert_not_awaited()

    async def test_missing_ip_returns_503_before_checking_limits_or_forwarding(self):
        for ip in (None, ""):
            with self.subTest(ip=ip):
                response = await self.request(ip=ip)

                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.data, {"detail": "Unable to identify client"})
                self.get_limiter.check.assert_not_awaited()
                self.post_limiter.check.assert_not_awaited()
                self.forward.assert_not_awaited()

    async def test_different_client_ips_are_passed_as_distinct_limiter_keys(self):
        await self.request(ip="192.0.2.1")
        await self.request(ip="192.0.2.2")

        self.assertEqual(self.get_limiter.check.await_args_list, [
            call("192.0.2.1"), call("192.0.2.2"),
        ])

    async def test_same_client_uses_same_limiter_across_services(self):
        self.get_limiter.check.side_effect = [(False, 0), (True, 12)]

        allowed = await self.request(service="users")
        blocked = await self.request(service="orders")

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(self.get_limiter.check.await_args_list, [
            call("192.0.2.1"), call("192.0.2.1"),
        ])
        self.forward.assert_awaited_once()

    async def test_forwarded_for_header_does_not_override_peer_ip(self):
        await self.request(HTTP_X_FORWARDED_FOR="198.51.100.1")

        self.get_limiter.check.assert_awaited_once_with("192.0.2.1")

    async def test_forwarded_for_header_cannot_replace_missing_peer_ip(self):
        response = await self.request(ip=None, HTTP_X_FORWARDED_FOR="198.51.100.1")

        self.assertEqual(response.status_code, 503)
        self.get_limiter.check.assert_not_awaited()
        self.forward.assert_not_awaited()


class GatewayRateLimitIntegrationTests(RedisTestCase):
    """Real limiter, real Redis and view together; only the clock and the upstream are fake."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        limiter_patch = patch.object(GatewayProxy, "rate_limiters", {
            "GET": self.make_limiter("get", limit=2, window_size=60),
        })
        limiter_patch.start()
        self.addCleanup(limiter_patch.stop)
        self.upstream = Mock(spec=httpx.AsyncClient)
        self.upstream.request = AsyncMock(return_value=httpx.Response(200))
        client_patch = patch("core.views.get_client", return_value=self.upstream)
        client_patch.start()
        self.addCleanup(client_patch.stop)
        self.factory = APIRequestFactory()
        self.view = GatewayProxy.as_view()

    async def request(self, ip="192.0.2.1"):
        request = self.factory.get("/api/users/1/", REMOTE_ADDR=ip)
        return await self.view(request, service_name="users", path="1/")

    async def test_over_limit_request_is_rejected_without_reaching_upstream(self):
        self.assertEqual((await self.request()).status_code, 200)
        self.assertEqual((await self.request()).status_code, 200)

        response = await self.request()

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "55")
        self.assertEqual(self.upstream.request.await_count, 2)

    async def test_other_client_keeps_its_own_allowance(self):
        await self.request(ip="192.0.2.1")
        await self.request(ip="192.0.2.1")

        response = await self.request(ip="192.0.2.2")

        self.assertEqual(response.status_code, 200)

    async def test_allowance_returns_after_window_expires(self):
        await self.request()
        await self.request()
        self.assertEqual((await self.request()).status_code, 429)

        self.clock.return_value = 180

        self.assertEqual((await self.request()).status_code, 200)


class MultiGatewaySharedQuotaTests(TestCase):
    """Opt-in HTTP check against two already-running gateways sharing Redis."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        second_url = os.environ.get("GATEWAY_SECOND_URL")
        if not second_url:
            raise SkipTest("Set GATEWAY_SECOND_URL to an already-running second gateway")
        cls.urls = [
            os.environ.get("GATEWAY_URL", "http://localhost:8000").rstrip("/"),
            second_url.rstrip("/"),
        ]
        if cls.urls[0] == cls.urls[1]:
            raise ValueError("The two gateway URLs must be different")
        cls.client = httpx.Client(timeout=10, trust_env=False)
        cls.addClassCleanup(cls.client.close)
        for url in cls.urls:
            try:
                cls.client.get(url + "/health/").raise_for_status()
            except httpx.HTTPError as exc:
                raise SkipTest(f"Gateway is not ready at {url}: {exc}")

    @staticmethod
    def wait_for_fresh_window(window_size):
        time.sleep(window_size - time.time() % window_size + 0.1)

    def test_sequential_get_and_post_quotas_are_shared_across_instances(self):
        get_limiter = GatewayProxy.rate_limiters["GET"]
        post_limiter = GatewayProxy.rate_limiters["POST"]
        self.assertEqual(get_limiter.window_size, post_limiter.window_size)
        window_size = get_limiter.window_size
        self.wait_for_fresh_window(window_size)
        window_start = int(time.time()) // window_size * window_size
        try:
            for method, limiter in (("GET", get_limiter), ("POST", post_limiter)):
                with self.subTest(method=method):
                    # Alternate instances; GET also alternates upstream services.
                    for index in range(limiter.limit):
                        url = self.urls[index % 2]
                        path = "/api/users/1/" if index % 2 == 0 else "/api/orders/42/"
                        kwargs = {}
                        if method == "POST":
                            path = "/api/orders/"
                            kwargs = {"json": {"user_id": 1}}
                        response = self.client.request(method, url + path, **kwargs)
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(response.json()["method"], method)

                    # Each instance handled only part of the allowance, yet both
                    # must reject once the combined quota has been consumed.
                    for url in self.urls:
                        path = "/api/users/1/" if method == "GET" else "/api/orders/"
                        response = self.client.request(method, url + path)
                        self.assertEqual(response.status_code, 429, response.text)
                        retry_after = int(response.headers["Retry-After"])
                        self.assertGreaterEqual(retry_after, 1)
                        self.assertLessEqual(retry_after, window_size)
                        self.assertIn("Rate limit exceeded", response.json()["detail"])

            self.assertEqual(
                int(time.time()) // window_size * window_size, window_start,
                "The request batch crossed a window boundary; rerun without competing traffic",
            )
        finally:
            # Leave the live client's allowance available to subsequent E2E tests.
            # No Redis keys are deleted and no gateway/container is managed here.
            if int(time.time()) // window_size * window_size == window_start:
                self.wait_for_fresh_window(window_size)
