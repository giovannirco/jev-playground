import http.client
import json
import threading
import unittest

from jev_playground.arcade.server import Handler, Server


class RequestValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = Server(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def request(self, path: str, body: bytes, length: str | None = None) -> tuple[int, dict]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.request("POST", path, body, {"Content-Length": length or str(len(body))})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        except (OSError, http.client.HTTPException) as error:
            self.fail(f"Request did not receive an HTTP response: {error}")
        finally:
            connection.close()

    def test_rejects_non_object_json(self) -> None:
        for path in ("/api/jev", "/api/btc/trade"):
            for body in (b"[]", b"null", b'"text"', b"42"):
                with self.subTest(path=path, body=body):
                    status, payload = self.request(path, body)
                    self.assertEqual(status, 400)
                    self.assertIn("error", payload)

    def test_rejects_invalid_content_length(self) -> None:
        for path in ("/api/jev", "/api/btc/trade"):
            for length in ("invalid", "-1"):
                with self.subTest(path=path, length=length):
                    status, _ = self.request(path, b"{}", length)
                    self.assertEqual(status, 400)

    def test_rejects_invalid_json_encoding(self) -> None:
        for path in ("/api/jev", "/api/btc/trade"):
            with self.subTest(path=path):
                status, _ = self.request(path, b"\xff")
                self.assertEqual(status, 400)

    def test_rejects_oversized_trading_request_before_reading(self) -> None:
        status, _ = self.request("/api/btc/trade", b"", "8193")
        self.assertEqual(status, 413)

    def test_rejects_content_length_beyond_integer_conversion_limit(self) -> None:
        status, _ = self.request("/api/jev", b"", "9" * 5000)
        self.assertEqual(status, 413)

    def test_valid_object_reaches_site_validation(self) -> None:
        status, payload = self.request("/api/jev", b'{"site":"unknown-fixture-site"}')
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Unknown site.")

    def test_catalog_and_all_static_sites_are_available(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.request("GET", "/api/sites")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            catalog = json.loads(response.read())
            self.assertEqual(len(catalog["sites"]), 20)
            for site in catalog["sites"]:
                connection.request("GET", "/" + site["slug"] + "/")
                response = connection.getresponse()
                self.assertEqual(response.status, 200, site["slug"])
                self.assertIn(b"<html", response.read().lower())
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
