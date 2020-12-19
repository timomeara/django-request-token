import datetime
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory, TestCase
from django.utils.timezone import now as tz_now

from request_token.exceptions import MaxUseError
from request_token.models import (
    RequestToken,
    RequestTokenErrorLog,
    RequestTokenErrorLogQuerySet,
    RequestTokenLog,
    parse_xff,
)
from request_token.settings import DEFAULT_MAX_USES
from request_token.utils import decode, to_seconds


def get_response(request: HttpRequest) -> HttpResponse:
    return HttpResponse()


class RequestTokenTests(TestCase):

    """RequestToken model property and method tests."""

    def setUp(self):
        # ensure user has unicode chars
        self.user = get_user_model().objects.create_user(
            "zoidberg", first_name=u"ß∂ƒ©˙∆", last_name=u"ƒ∆"
        )

    def test_defaults(self):
        token = RequestToken()
        self.assertIsNone(token.user)
        self.assertEqual(token.scope, "")
        self.assertEqual(token.login_mode, RequestToken.LoginMode.NONE)
        self.assertIsNone(token.expiration_time)
        self.assertIsNone(token.not_before_time)
        self.assertEqual(token.data, {})
        self.assertIsNone(token.issued_at)
        self.assertEqual(token.max_uses, DEFAULT_MAX_USES)
        self.assertEqual(token.used_to_date, 0)

    def test_string_repr(self):
        token = RequestToken(user=self.user)
        self.assertIsNotNone(str(token))
        self.assertIsNotNone(repr(token))

    def test_save(self):
        token = RequestToken().save()
        self.assertIsNotNone(token)
        self.assertIsNone(token.user)
        self.assertEqual(token.scope, "")
        self.assertEqual(token.login_mode, RequestToken.LoginMode.NONE)
        self.assertIsNone(token.expiration_time)
        self.assertIsNone(token.not_before_time)
        self.assertEqual(token.data, {})
        self.assertIsNotNone(token.issued_at)
        self.assertEqual(token.max_uses, DEFAULT_MAX_USES)
        self.assertEqual(token.used_to_date, 0)

        token.issued_at = None
        token = token.save(update_fields=["issued_at"])
        self.assertIsNone(token.issued_at)

        now = tz_now()
        with mock.patch("request_token.models.tz_now", lambda: now):
            token = RequestToken(
                login_mode=RequestToken.LoginMode.NONE, user=self.user, scope="foo"
            )
            self.assertIsNone(token.issued_at)
            self.assertIsNone(token.expiration_time)
            token.save()
            self.assertEqual(token.issued_at, now)
            self.assertIsNone(token.expiration_time)

    def test_claims(self):
        token = RequestToken()
        # raises error with no id set - put into context manager as it's
        # an attr, not a callable
        self.assertEqual(len(token.claims), 3)
        self.assertEqual(token.max, DEFAULT_MAX_USES)
        self.assertEqual(token.sub, "")
        self.assertIsNone(token.jti)
        self.assertIsNone(token.aud)
        self.assertIsNone(token.exp)
        self.assertIsNone(token.nbf)
        self.assertIsNone(token.iat)

        # now let's set some properties
        token.user = self.user
        self.assertEqual(token.aud, self.user.id)
        self.assertEqual(len(token.claims), 4)

        token.login_mode = RequestToken.LoginMode.REQUEST
        self.assertEqual(
            token.claims["mod"], RequestToken.LoginMode.REQUEST[:1].lower()
        )
        self.assertEqual(len(token.claims), 4)

        now = tz_now()
        now_sec = to_seconds(now)

        token.expiration_time = now
        self.assertEqual(token.exp, now_sec)
        self.assertEqual(len(token.claims), 5)

        token.not_before_time = now
        self.assertEqual(token.nbf, now_sec)
        self.assertEqual(len(token.claims), 6)

        # saving updates the id and issued_at timestamp
        with mock.patch("request_token.models.tz_now", lambda: now):
            token.save()
            self.assertEqual(token.iat, now_sec)
            self.assertEqual(token.jti, token.id)
            self.assertEqual(len(token.claims), 8)

    def test_json(self):
        """Test the data field is really JSON."""
        token = RequestToken(data={"foo": True})
        token.save()
        self.assertTrue(token.data["foo"])

    def test_clean_NONE(self):

        # LoginMode.NONE doesn't care about user.
        token = RequestToken(login_mode=RequestToken.LoginMode.NONE)
        token.clean()
        token.user = self.user
        token.clean()

    def test_clean_REQUEST(self):

        # request mode
        token = RequestToken(login_mode=RequestToken.LoginMode.REQUEST)
        token.clean()
        token.user = None
        self.assertRaises(ValidationError, token.clean)

    def test_clean_SESSION__no_user(self):

        token = RequestToken(login_mode=RequestToken.LoginMode.SESSION)
        token.login_mode = RequestToken.LoginMode.SESSION
        token.user = self.user
        token.issued_at = tz_now()
        token.expiration_time = token.issued_at + datetime.timedelta(minutes=1)
        token.max_uses = DEFAULT_MAX_USES
        token.clean()

        def assertValidationFails(field_name):
            with self.assertRaises(ValidationError) as ctx:
                token.clean()
            self.assertTrue(field_name in dict(ctx.exception))

        token.user = None
        assertValidationFails("user")

        # check the reset_session works!
        reset_session()
        token.user = None
        assertValidationFails("user")

        reset_session()
        token.expiration_time = None
        assertValidationFails("expiration_time")

    def test_log(self):
        token = RequestToken().save()
        factory = RequestFactory()
        request = factory.get("/")
        request.user = AnonymousUser()
        request.META = {}
        response = HttpResponse("foo", status=123)

        def assertUsedToDate(expected):
            token.refresh_from_db(fields=["used_to_date"])
            self.assertEqual(token.used_to_date, expected)

        log = token.log(request, response)
        self.assertEqual(RequestTokenLog.objects.get(), log)
        self.assertEqual(log.user, None)
        self.assertEqual(log.token, token)
        self.assertEqual(log.user_agent, "unknown")
        self.assertEqual(log.client_ip, None)
        self.assertEqual(log.status_code, 123)
        assertUsedToDate(1)

        request.META["REMOTE_ADDR"] = "192.168.0.1"
        log = token.log(request, response)
        self.assertEqual(log.client_ip, "192.168.0.1")
        assertUsedToDate(2)

        request.META["HTTP_X_FORWARDED_FOR"] = "192.168.0.2"
        log = token.log(request, response)
        self.assertEqual(log.client_ip, "192.168.0.2")
        assertUsedToDate(3)

        request.META["HTTP_USER_AGENT"] = "test_agent"
        log = token.log(request, response)
        self.assertEqual(log.user_agent, "test_agent")
        token.refresh_from_db(fields=["used_to_date"])
        assertUsedToDate(4)

        with mock.patch.object(
            RequestTokenErrorLogQuerySet, "create_error_log"
        ) as mock_log:
            log = token.log(request, response, MaxUseError("foo"))
            self.assertEqual(mock_log.call_count, 1)
            self.assertEqual(log.user_agent, "test_agent")
            token.refresh_from_db(fields=["used_to_date"])
            assertUsedToDate(5)

    def test_jwt(self):
        token = RequestToken(id=1, scope="foo").save()
        jwt = token.jwt()
        self.assertEqual(decode(jwt), token.claims)

    def test_validate_max_uses(self):
        token = RequestToken(max_uses=1, used_to_date=0)
        token.validate_max_uses()
        token.used_to_date = token.max_uses
        self.assertRaises(MaxUseError, token.validate_max_uses)

    def test_expire(self):
        expiry = tz_now() + datetime.timedelta(days=1)
        token = RequestToken.objects.create_token(
            scope="foo", login_mode=RequestToken.LoginMode.NONE, expiration_time=expiry
        )
        self.assertTrue(token.expiration_time == expiry)
        token.expire()
        self.assertTrue(token.expiration_time < expiry)

    def test_parse_xff(self):
        def assertMeta(meta, expected):
            self.assertEqual(parse_xff(meta), expected)

        assertMeta(None, None)
        assertMeta("", "")
        assertMeta("foo", "foo")
        assertMeta("foo, bar, baz", "foo")
        assertMeta("foo , bar, baz", "foo")
        assertMeta("8.8.8.8, 123.124.125.126", "8.8.8.8")


class RequestTokenQuerySetTests(TestCase):

    """RequestTokenQuerySet class tests."""

    def test_create_token(self):
        self.assertRaises(TypeError, RequestToken.objects.create_token)
        RequestToken.objects.create_token(scope="foo")
        self.assertEqual(RequestToken.objects.get().scope, "foo")


class RequestTokenErrorLogQuerySetTests(TestCase):
    def test_create_error_log(self):
        user = get_user_model().objects.create_user(
            "zoidberg", first_name=u"∂ƒ©˙∆", last_name=u"†¥¨^"
        )
        token = RequestToken.objects.create_token(
            scope="foo", user=user, login_mode=RequestToken.LoginMode.REQUEST
        )
        log = RequestTokenLog(token=token, user=user).save()
        elog = RequestTokenErrorLog.objects.create_error_log(log, MaxUseError("foo"))
        self.assertEqual(elog.token, token)
        self.assertEqual(elog.log, log)
        self.assertEqual(elog.error_type, "MaxUseError")
        self.assertEqual(elog.error_message, "foo")
        self.assertEqual(str(elog), "foo")


class RequestTokenLogTests(TestCase):

    """RequestTokenLog model property and method tests."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "zoidberg", first_name=u"∂ƒ©˙∆", last_name=u"†¥¨^"
        )
        self.token = RequestToken.objects.create_token(
            scope="foo", user=self.user, login_mode=RequestToken.LoginMode.REQUEST
        )

    def test_defaults(self):
        log = RequestTokenLog(token=self.token, user=self.user)
        self.assertEqual(log.user, self.user)
        self.assertEqual(log.token, self.token)
        self.assertEqual(log.user_agent, "")
        self.assertEqual(log.client_ip, None)
        self.assertIsNone(log.timestamp)

        token = RequestToken(user=self.user)
        self.assertIsNotNone(str(token))
        self.assertIsNotNone(repr(token))

    def test_string_repr(self):
        log = RequestTokenLog(token=self.token, user=self.user)
        self.assertIsNotNone(str(log))
        self.assertIsNotNone(repr(log))

        log.user = None
        self.assertIsNotNone(str(log))
        self.assertIsNotNone(repr(log))

    def test_save(self):
        log = RequestTokenLog(token=self.token, user=self.user).save()
        self.assertIsNotNone(log.timestamp)

        log.timestamp = None
        self.assertRaises(IntegrityError, log.save, update_fields=["timestamp"])

    def test_ipv6(self):
        """Test that IP v4 and v6 are handled."""
        log = RequestTokenLog(token=self.token, user=self.user).save()
        self.assertIsNone(log.client_ip)

        def assertIP(ip):
            log.client_ip = ip
            log.save()
            self.assertEqual(log.client_ip, ip)

        assertIP("192.168.0.1")
        # taken from http://ipv6.com/articles/general/IPv6-Addressing.htm
        assertIP("2001:cdba:0000:0000:0000:0000:3257:9652")
        assertIP("2001:cdba:0:0:0:0:3257:9652")
        assertIP("2001:cdba::3257:9652")
