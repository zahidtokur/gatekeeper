from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, Mock, call, patch

import httpx
from django.http import HttpResponse
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from core.rate_limiter import FixedWindowRateLimiter
from core.views import GatewayProxy


@override_settings(ALLOWED_HOSTS=["testserver"])
class UpstreamConnectionErrorTests(SimpleTestCase):
    """A connection error can't be produced by a running service, so it is mocked."""

    def setUp(self):
        self.upstream = Mock(spec=httpx.AsyncClient)
        self.upstream.request = AsyncMock()
        client_patch = patch("core.views.get_client", return_value=self.upstream)
        client_patch.start()
        self.addCleanup(client_patch.stop)
        limiter_patch = patch.object(GatewayProxy, "rate_limiters", {
            "GET": Mock(check=Mock(return_value=(False, 0))),
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


class FixedWindowRateLimiterTests(SimpleTestCase):
    def setUp(self):
        clock_patch = patch("core.rate_limiter.time.time", return_value=125)
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)

    def test_rejects_non_positive_configuration(self):
        for limit, window in ((0, 60), (-1, 60), (1, 0), (1, -60)):
            with self.subTest(limit=limit, window=window):
                with self.assertRaises(ValueError):
                    FixedWindowRateLimiter(limit, window)

    def test_allows_exactly_limit_requests_then_returns_remaining_window(self):
        limiter = FixedWindowRateLimiter(limit=2, window_size=60)

        self.assertEqual(limiter.check("client-a"), (False, 0))
        self.assertEqual(limiter.check("client-a"), (False, 0))
        self.assertEqual(limiter.check("client-a"), (True, 55))
        self.clock.return_value = 179.9
        self.assertEqual(limiter.check("client-a"), (True, 1))

    def test_allowance_resets_at_fixed_window_boundary(self):
        limiter = FixedWindowRateLimiter(limit=1, window_size=60)
        self.clock.return_value = 179
        self.assertEqual(limiter.check("client-a"), (False, 0))
        self.assertEqual(limiter.check("client-a"), (True, 1))

        self.clock.return_value = 180
        self.assertEqual(limiter.check("client-a"), (False, 0))
        self.assertEqual(limiter.check("client-a"), (True, 60))

    def test_clients_have_independent_allowances(self):
        limiter = FixedWindowRateLimiter(limit=1, window_size=60)

        self.assertEqual(limiter.check("client-a"), (False, 0))
        self.assertEqual(limiter.check("client-a"), (True, 55))
        self.assertEqual(limiter.check("client-b"), (False, 0))
        self.assertEqual(limiter.check("client-b"), (True, 55))

    def test_separate_limiter_instances_do_not_share_counters(self):
        first = FixedWindowRateLimiter(limit=1, window_size=60)
        second = FixedWindowRateLimiter(limit=1, window_size=60)

        self.assertEqual(first.check("client-a"), (False, 0))
        self.assertEqual(first.check("client-a"), (True, 55))
        self.assertEqual(second.check("client-a"), (False, 0))

    def test_concurrent_requests_cannot_exceed_limit(self):
        limiter = FixedWindowRateLimiter(limit=10, window_size=60)

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: limiter.check("client-a"), range(50)))

        self.assertEqual(results.count((False, 0)), 10)
        self.assertEqual(results.count((True, 55)), 40)


class GatewayRateLimitTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.get_limiter = Mock()
        self.get_limiter.check.return_value = (False, 0)
        self.post_limiter = Mock()
        self.post_limiter.check.return_value = (False, 0)
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
                selected.check.assert_called_once_with("192.0.2.1")
                other.check.assert_not_called()
                self.forward.assert_awaited_once()

    async def test_limited_requests_return_429_and_retry_after_without_forwarding(self):
        for method, limiter in (("get", self.get_limiter), ("post", self.post_limiter)):
            with self.subTest(method=method):
                limiter.check.return_value = (True, 17)

                response = await self.request(method)

                self.assertEqual(response.status_code, 429)
                self.assertEqual(response.headers["Retry-After"], "17")
                self.forward.assert_not_awaited()

    async def test_missing_ip_returns_503_before_checking_limits_or_forwarding(self):
        for ip in (None, ""):
            with self.subTest(ip=ip):
                response = await self.request(ip=ip)

                self.assertEqual(response.status_code, 503)
                self.get_limiter.check.assert_not_called()
                self.post_limiter.check.assert_not_called()
                self.forward.assert_not_awaited()

    async def test_different_client_ips_are_passed_as_distinct_limiter_keys(self):
        await self.request(ip="192.0.2.1")
        await self.request(ip="192.0.2.2")

        self.assertEqual(self.get_limiter.check.call_args_list, [
            call("192.0.2.1"), call("192.0.2.2"),
        ])

    async def test_same_client_uses_same_limiter_across_services(self):
        self.get_limiter.check.side_effect = [(False, 0), (True, 12)]

        allowed = await self.request(service="users")
        blocked = await self.request(service="orders")

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(self.get_limiter.check.call_args_list, [
            call("192.0.2.1"), call("192.0.2.1"),
        ])
        self.forward.assert_awaited_once()

    async def test_forwarded_for_header_does_not_override_peer_ip(self):
        await self.request(HTTP_X_FORWARDED_FOR="198.51.100.1")

        self.get_limiter.check.assert_called_once_with("192.0.2.1")

    async def test_forwarded_for_header_cannot_replace_missing_peer_ip(self):
        response = await self.request(ip=None, HTTP_X_FORWARDED_FOR="198.51.100.1")

        self.assertEqual(response.status_code, 503)
        self.get_limiter.check.assert_not_called()
        self.forward.assert_not_awaited()
