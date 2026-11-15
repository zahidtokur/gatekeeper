from django.test import SimpleTestCase, override_settings


@override_settings(ALLOWED_HOSTS=["testserver"])
class UserRetrieveTests(SimpleTestCase):
    url = "/v1/users/1/"

    async def test_get_echoes_request_details(self):
        response = await self.async_client.get(
            self.url, {"name": "Ada", "page": 2, "active": "true"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "service": "user-service",
            "method": "GET",
            "path": self.url,
            "query_params": {
                "name": ["Ada"], 
                "page": ["2"], 
                "active": ["true"]
            },
        })

    async def test_disallowed_methods(self):
        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method):
                response = await getattr(self.async_client, method)(
                    self.url, data="{}", content_type="application/json"
                )

                self.assertEqual(response.status_code, 405)
                self.assertIn("detail", response.json())
                self.assertNotIn(method.upper(), response.headers["Allow"])
