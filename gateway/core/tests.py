import asyncio
from types import SimpleNamespace

from unittest.mock import AsyncMock, Mock, call, patch

import httpx
from django.http import HttpResponse
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from core.circuit_breaker import CircuitBreaker, CircuitState
from core.rate_limiter import FixedWindowRateLimiter
from core.views import GatewayProxy


@override_settings(ALLOWED_HOSTS=["testserver"])
class UpstreamConnectionErrorTests(SimpleTestCase):
    """Connection errors and timeouts are mocked, so these tests run without waiting."""

    def setUp(self):
        self.upstream = Mock(spec=httpx.AsyncClient)
        self.upstream.request = AsyncMock()
        client_patch = patch("core.views.get_http_client", return_value=self.upstream)
        client_patch.start()
        self.addCleanup(client_patch.stop)
        limiter_patch = patch.object(
            GatewayProxy,
            "rate_limiters",
            {
                "GET": Mock(check=AsyncMock(return_value=(False, 0))),
            },
        )
        limiter_patch.start()
        self.addCleanup(limiter_patch.stop)

    async def test_request_error_returns_502(self):
        for error_type in (httpx.ConnectError, httpx.RemoteProtocolError):
            with self.subTest(error_type=error_type.__name__):
                self.upstream.request.side_effect = error_type("connection failed")

                response = await self.async_client.get("/api/orders/1/")

                self.assertEqual(response.status_code, 502)
                self.assertEqual(
                    response.json(), {"detail": "Upstream connection error"}
                )

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


class FixedWindowRateLimiterConfigTests(SimpleTestCase):
    def test_rejects_non_positive_configuration(self):
        for limit, window in ((0, 60), (-1, 60), (1, 0), (1, -60)):
            with self.subTest(limit=limit, window=window):
                with self.assertRaises(ValueError):
                    FixedWindowRateLimiter("get", limit, window)


class GatewayRateLimitTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.get_limiter = Mock(check=AsyncMock(return_value=(False, 0)))
        self.post_limiter = Mock(check=AsyncMock(return_value=(False, 0)))
        limiter_patch = patch.object(
            GatewayProxy,
            "rate_limiters",
            {
                "GET": self.get_limiter,
                "POST": self.post_limiter,
            },
        )
        limiter_patch.start()
        self.addCleanup(limiter_patch.stop)
        forward_patch = patch.object(
            GatewayProxy,
            "_forward",
            new_callable=AsyncMock,
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
                self.assertEqual(
                    response.data,
                    {
                        "detail": "Rate limit exceeded. Retry after the time in the Retry-After header."
                    },
                )
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

        self.assertEqual(
            self.get_limiter.check.await_args_list,
            [
                call("192.0.2.1"),
                call("192.0.2.2"),
            ],
        )

    async def test_same_client_uses_same_limiter_across_services(self):
        self.get_limiter.check.side_effect = [(False, 0), (True, 12)]

        allowed = await self.request(service="users")
        blocked = await self.request(service="orders")

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(
            self.get_limiter.check.await_args_list,
            [
                call("192.0.2.1"),
                call("192.0.2.1"),
            ],
        )
        self.forward.assert_awaited_once()

    async def test_forwarded_for_header_does_not_override_peer_ip(self):
        await self.request(HTTP_X_FORWARDED_FOR="198.51.100.1")

        self.get_limiter.check.assert_awaited_once_with("192.0.2.1")

    async def test_forwarded_for_header_cannot_replace_missing_peer_ip(self):
        response = await self.request(ip=None, HTTP_X_FORWARDED_FOR="198.51.100.1")

        self.assertEqual(response.status_code, 503)
        self.get_limiter.check.assert_not_awaited()
        self.forward.assert_not_awaited()


class CircuitBreakerTests(SimpleTestCase):
    def setUp(self):
        self.clock = Mock(return_value=100)
        clock_patch = patch(
            "core.circuit_breaker.time", SimpleNamespace(monotonic=self.clock)
        )
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=30)

    def open_circuit(self):
        self.breaker.record_failure()
        self.breaker.record_failure()

    def start_probe(self):
        self.open_circuit()
        self.clock.return_value = 130
        self.assertTrue(self.breaker.allow_request())

    def test_closed_circuit_allows_requests(self):
        for _ in range(3):
            self.assertTrue(self.breaker.allow_request())
        self.assertEqual(self.breaker.state, CircuitState.CLOSED)
        self.assertFalse(self.breaker.probe_in_progress)
        self.assertEqual(self.breaker.failures, 0)

    def test_opens_at_threshold_and_rejects_until_recovery_deadline(self):
        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, CircuitState.CLOSED)
        self.assertTrue(self.breaker.allow_request())

        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, CircuitState.OPEN)
        self.assertEqual(self.breaker.open_until, 130)
        for now in (100, 120, 129.999):
            with self.subTest(now=now):
                self.clock.return_value = now
                self.assertFalse(self.breaker.allow_request())
                self.assertEqual(self.breaker.failures, 2)
                self.assertEqual(self.breaker.open_until, 130)

    def test_exact_recovery_deadline_allows_only_one_probe(self):
        self.start_probe()

        self.assertEqual(self.breaker.state, CircuitState.HALF_OPEN)
        self.assertTrue(self.breaker.probe_in_progress)
        self.assertFalse(self.breaker.allow_request())
        self.clock.return_value = 150
        self.assertFalse(self.breaker.allow_request())

    def test_success_resets_failure_count(self):
        self.breaker.record_failure()
        self.breaker.record_failure()
        self.breaker.record_success()

        self.assertEqual(self.breaker.failures, 0)
        self.assertEqual(self.breaker.state, CircuitState.CLOSED)
        self.assertTrue(self.breaker.allow_request())

    def test_successful_probe_closes_and_resets_breaker(self):
        self.start_probe()
        self.breaker.record_success()

        self.assertEqual(self.breaker.state, CircuitState.CLOSED)
        self.assertEqual(self.breaker.failures, 0)
        self.assertEqual(self.breaker.open_until, 0)
        self.assertFalse(self.breaker.probe_in_progress)
        self.assertTrue(self.breaker.allow_request())
        self.assertTrue(self.breaker.allow_request())


class GatewayCircuitBreakerTests(SimpleTestCase):
    """Exercise the real breaker through the view; mock time, Redis and HTTP I/O."""

    def setUp(self):
        self.clock = Mock(return_value=100)
        self.users_breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=30)
        self.orders_breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=30)
        self.services = {
            "users": {
                "url": "http://users.test/v1/users/",
                "circuit_breaker": self.users_breaker,
            },
            "orders": {
                "url": "http://orders.test/v1/orders/",
                "circuit_breaker": self.orders_breaker,
            },
        }
        self.upstream = Mock(spec=httpx.AsyncClient)
        self.upstream.request = AsyncMock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        patches = (
            patch("core.circuit_breaker.time", SimpleNamespace(monotonic=self.clock)),
            patch.object(GatewayProxy, "services", self.services),
            patch.object(
                GatewayProxy,
                "rate_limiters",
                {
                    method: Mock(check=AsyncMock(return_value=(False, 0)))
                    for method in ("GET", "POST")
                },
            ),
            patch("core.views.get_http_client", return_value=self.upstream),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.factory = APIRequestFactory()
        self.view = GatewayProxy.as_view()

    async def request(self, service="users", method="get"):
        request = getattr(self.factory, method)(
            f"/api/{service}/1/", REMOTE_ADDR="192.0.2.1"
        )
        return await self.view(request, service_name=service, path="1/")

    async def test_non_5xx_response_resets_failures_for_get_and_post(self):
        for method, status in (("get", 200), ("get", 404), ("post", 201)):
            with self.subTest(method=method, status=status):
                self.users_breaker.record_failure()
                self.upstream.request.return_value = httpx.Response(status)

                response = await self.request(method=method)

                self.assertEqual(response.status_code, status)
                self.assertEqual(self.users_breaker.failures, 0)
                self.assertEqual(self.users_breaker.state, CircuitState.CLOSED)

    async def test_5xx_failures_open_only_affected_service_and_block_upstream_calls(
        self,
    ):
        for status in (500, 503):
            self.upstream.request.return_value = httpx.Response(status)
            response = await self.request()
            self.assertEqual(response.status_code, status)

        self.assertEqual(self.users_breaker.state, CircuitState.OPEN)
        self.assertEqual(self.users_breaker.failures, 2)
        self.upstream.request.reset_mock()
        response = await self.request()
        self.assertEqual(response.status_code, 503)
        self.upstream.request.assert_not_awaited()
        self.assertEqual(self.users_breaker.failures, 2)

        self.upstream.request.return_value = httpx.Response(200)
        response = await self.request(service="orders")
        self.assertEqual(response.status_code, 200)
        self.upstream.request.assert_awaited_once()
        self.assertEqual(self.orders_breaker.state, CircuitState.CLOSED)

    async def test_open_circuit_response_is_gateway_json(self):
        self.users_breaker.record_failure()
        self.users_breaker.record_failure()

        response = await self.request()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data, {"detail": "Service temporarily unavailable."})

    @override_settings(CIRCUIT_BREAKER_ENABLED=False)
    async def test_disabled_breaker_forwards_every_request(self):
        self.upstream.request.return_value = httpx.Response(500)

        for _ in range(5):
            response = await self.request()
            self.assertEqual(response.status_code, 500)

        self.assertEqual(self.upstream.request.await_count, 5)

    async def test_httpx_errors_record_failure_without_recording_success(self):
        cases = (
            (httpx.ConnectTimeout, 502),
            (httpx.ReadTimeout, 504),
            (httpx.ConnectError, 502),
        )
        for error_type, status in cases:
            with self.subTest(error=error_type.__name__):
                self.users_breaker.record_success()
                self.users_breaker.record_failure()
                self.upstream.request.side_effect = error_type("upstream failed")

                response = await self.request()

                self.assertEqual(response.status_code, status)
                self.assertEqual(self.users_breaker.state, CircuitState.OPEN)
                self.assertEqual(self.users_breaker.failures, 2)

    async def test_failed_recovery_probe_reopens_then_successful_probe_closes(self):
        self.users_breaker.record_failure()
        self.users_breaker.record_failure()
        self.clock.return_value = 130
        self.upstream.request.return_value = httpx.Response(503)

        response = await self.request()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.users_breaker.state, CircuitState.OPEN)
        self.assertEqual(self.users_breaker.open_until, 160)
        self.upstream.request.reset_mock()

        self.clock.return_value = 159
        response = await self.request()
        self.assertEqual(response.status_code, 503)
        self.upstream.request.assert_not_awaited()

        self.clock.return_value = 160
        self.upstream.request.return_value = httpx.Response(200)
        response = await self.request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.users_breaker.state, CircuitState.CLOSED)
        self.assertEqual(self.users_breaker.failures, 0)
        self.assertFalse(self.users_breaker.probe_in_progress)

    async def test_cancelled_probe_propagates_cancellation_and_allows_another_probe(
        self,
    ):
        self.users_breaker.record_failure()
        self.users_breaker.record_failure()
        self.clock.return_value = 130
        entered_upstream = asyncio.Event()
        release_upstream = asyncio.Event()

        async def pending_response(*args, **kwargs):
            entered_upstream.set()
            await release_upstream.wait()
            return httpx.Response(200)

        self.upstream.request.side_effect = pending_response
        task = asyncio.create_task(self.request())
        try:
            await asyncio.wait_for(entered_upstream.wait(), timeout=2)
            self.assertTrue(self.users_breaker.probe_in_progress)
            response = await self.request()
            self.assertEqual(response.status_code, 503)
            self.upstream.request.assert_awaited_once()
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(self.users_breaker.state, CircuitState.HALF_OPEN)
        self.assertEqual(self.users_breaker.failures, 2)
        self.assertFalse(self.users_breaker.probe_in_progress)
        self.assertEqual(self.users_breaker.open_until, 130)

        self.upstream.request.side_effect = None
        self.upstream.request.return_value = httpx.Response(200)
        response = await self.request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.users_breaker.state, CircuitState.CLOSED)
        self.assertEqual(self.users_breaker.failures, 0)

    async def test_cancelled_closed_request_does_not_count_as_failure(self):
        self.users_breaker.record_failure()
        self.upstream.request.side_effect = asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await self.request()

        self.assertEqual(self.users_breaker.state, CircuitState.CLOSED)
        self.assertEqual(self.users_breaker.failures, 1)
        self.assertFalse(self.users_breaker.probe_in_progress)
