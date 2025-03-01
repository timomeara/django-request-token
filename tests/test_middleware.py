import json
from typing import Any
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from jwt import exceptions

from request_token.middleware import RequestTokenMiddleware
from request_token.models import RequestToken
from request_token.settings import JWT_QUERYSTRING_ARG


class MockSession(object):
    """Fake Session model used to support `session_key` property."""

    @property
    def session_key(self):
        return "foobar"


class MiddlewareTests(TestCase):
    """RequestTokenMiddleware tests."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("zoidberg")
        self.factory = RequestFactory()
        self.middleware = RequestTokenMiddleware(get_response=lambda r: HttpResponse())
        self.token = RequestToken.objects.create_token(scope="foo")
        self.default_payload = {JWT_QUERYSTRING_ARG: self.token.jwt()}

    def get_request(self):
        request = self.factory.get(f"/?{JWT_QUERYSTRING_ARG}={self.token.jwt()}")
        request.user = self.user
        request.session = MockSession()
        return request

    def post_request(self):
        request = self.factory.post("/", self.default_payload)
        request.user = self.user
        request.session = MockSession()
        return request

    def post_request_with_JSON(self, payload: Any):
        data = json.dumps(payload)
        request = self.factory.post("/", data, "application/json")
        request.user = self.user
        request.session = MockSession()
        return request

    def test_process_request_assertions(self):
        request = self.factory.get("/")
        self.assertRaises(ImproperlyConfigured, self.middleware, request)

        request.user = AnonymousUser()
        self.assertRaises(ImproperlyConfigured, self.middleware, request)
        request.session = MockSession()

        self.middleware(request)
        self.assertFalse(hasattr(request, "token"))

    def test_process_request_without_token(self):
        request = self.factory.get("/")
        request.user = AnonymousUser()
        request.session = MockSession()
        self.middleware(request)
        self.assertFalse(hasattr(request, "token"))

    def test_process_GET_request_with_valid_token(self):
        request = self.get_request()
        self.middleware(request)
        self.assertEqual(request.token, self.token)

    def test_process_POST_request_with_valid_token(self):
        request = self.post_request()
        self.middleware(request)
        self.assertEqual(request.token, self.token)

    def test_process_POST_request_with_valid_token_with_json(self):
        request = self.post_request_with_JSON(self.default_payload)
        self.middleware(request)
        self.assertEqual(request.token, self.token)

    def test_process_AJAX_request_with_array(self):
        """Test for issue #50."""
        request = self.post_request_with_JSON([1])
        self.middleware(request)
        self.assertFalse(hasattr(request, "token"))

    def test_process_request_not_allowed(self):
        # PUT requests won't decode the token
        request = self.factory.put("/?rt=foo")
        request.user = self.user
        request.session = MockSession()
        response = self.middleware(request)
        self.assertFalse(hasattr(request, "token"))
        self.assertEqual(response.status_code, 200)

    @mock.patch("request_token.middleware.logger")
    def test_process_request_token_error(self, mock_logger):
        # token decode error - request passes through _without_ a token
        request = self.factory.get("/?rt=foo")
        request.user = self.user
        request.session = MockSession()
        self.middleware(request)
        self.assertIsNone(request.token)
        self.assertEqual(mock_logger.exception.call_count, 1)

    @mock.patch("request_token.middleware.logger")
    def test_process_request_token_does_not_exist(self, mock_logger):
        request = self.get_request()
        self.token.delete()
        self.middleware(request)
        self.assertIsNone(request.token)
        self.assertEqual(mock_logger.exception.call_count, 1)

    @mock.patch.object(RequestToken, "log")
    def test_process_exception(self, mock_log):
        request = self.get_request()
        request.token = self.token
        exception = exceptions.InvalidTokenError("bar")
        with self.assertRaises(PermissionDenied):
            response = self.middleware.process_exception(request, exception)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.reason_phrase, str(exception))

        # no request token = no error log
        del request.token
        mock_log.reset_mock()
        with self.assertRaises(PermissionDenied):
            response = self.middleware.process_exception(request, exception)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.reason_phrase, str(exception))

        # round it out with a non-token error
        response = self.middleware.process_exception(request, Exception("foo"))
        self.assertIsNone(response)

    def test_extract_json_token__array(self):
        """Test for issue #51."""
        request = self.post_request_with_JSON(["foo"])
        middleware = RequestTokenMiddleware(lambda r: HttpResponse())
        self.assertIsNone(middleware.extract_ajax_token(request))

    def test_extract_json_token(self):
        request = self.post_request_with_JSON(self.default_payload)
        middleware = RequestTokenMiddleware(lambda r: HttpResponse())
        self.assertEqual(middleware.extract_ajax_token(request), self.token.jwt())
