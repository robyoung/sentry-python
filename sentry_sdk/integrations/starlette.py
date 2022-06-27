from __future__ import absolute_import

from sentry_sdk._compat import iteritems
from sentry_sdk._types import MYPY
from sentry_sdk.hub import Hub, _should_send_default_pii
from sentry_sdk.integrations import DidNotEnable, Integration
from sentry_sdk.integrations._wsgi_common import (
    _is_json_content_type,
    request_body_within_bounds,
)
from sentry_sdk.integrations.asgi import SentryAsgiMiddleware
from sentry_sdk.utils import AnnotatedValue, event_from_exception

if MYPY:
    from typing import Any, Awaitable, Callable, Dict, Optional, Union


try:
    from starlette.applications import Starlette
    from starlette.datastructures import UploadFile
    from starlette.middleware import Middleware
    from starlette.middleware.authentication import AuthenticationMiddleware
    from starlette.middleware.exceptions import ExceptionMiddleware
    from starlette.requests import Request
    from starlette.routing import Match
except ImportError:
    raise DidNotEnable("Starlette is not installed")

TRANSACTION_STYLE_VALUES = ("endpoint", "url")


class StarletteIntegration(Integration):
    identifier = "starlette"

    transaction_style = None

    def __init__(self, transaction_style="endpoint"):
        # type: (str) -> None
        if transaction_style not in TRANSACTION_STYLE_VALUES:
            raise ValueError(
                "Invalid value for transaction_style: %s (must be in %s)"
                % (transaction_style, TRANSACTION_STYLE_VALUES)
            )
        self.transaction_style = transaction_style

    @staticmethod
    def setup_once():
        # type: () -> None
        patch_middlewares()
        patch_asgi_app()


def _enable_span_for_middleware(middleware_class):
    # type: (Any) -> type
    old_call = middleware_class.__call__

    async def _create_span_call(*args, **kwargs):
        # type: (Any, Any) -> None
        hub = Hub.current
        integration = hub.get_integration(StarletteIntegration)
        if integration is not None:
            middleware_name = args[0].__class__.__name__
            with hub.start_span(
                op="starlette.middleware", description=middleware_name
            ) as middleware_span:
                middleware_span.set_tag("starlette.middleware_name", middleware_name)

                await old_call(*args, **kwargs)

        else:
            await old_call(*args, **kwargs)

    middleware_class.__call__ = _create_span_call

    return middleware_class


def _capture_exception(exception, handled=False):
    # type: (BaseException, **Any) -> None
    hub = Hub.current
    if hub.get_integration(StarletteIntegration) is None:
        return

    event, hint = event_from_exception(
        exception,
        client_options=hub.client.options if hub.client else None,
        mechanism={"type": StarletteIntegration.identifier, "handled": handled},
    )

    hub.capture_event(event, hint=hint)


def patch_exception_middleware(middleware_class):
    # type: (Any) -> None
    """
    Capture all exceptions in Starlette app and
    also extract user information.
    """
    old_http_exception = middleware_class.http_exception

    def _sentry_patched_http_exception(self, request, exc):
        # type: (Any, Any, Any) -> None
        _capture_exception(exc, handled=True)
        return old_http_exception(self, request, exc)

    middleware_class.http_exception = _sentry_patched_http_exception


def _add_user_to_sentry_scope(scope):
    # type: (Dict[str, Any]) -> None
    """
    Extracts user information from the ASGI scope and
    adds it to Sentry's scope.
    """
    if "user" not in scope:
        return

    hub = Hub.current
    if hub.get_integration(StarletteIntegration) is None:
        return

    with hub.configure_scope() as sentry_scope:
        user_info = {}  # type: Dict[str, Any]
        starlette_user = scope["user"]

        username = getattr(starlette_user, "username", None)
        if username:
            user_info.setdefault("username", starlette_user.username)

        user_id = getattr(starlette_user, "id", None)
        if user_id:
            user_info.setdefault("id", starlette_user.id)

        email = getattr(starlette_user, "email", None)
        if email:
            user_info.setdefault("email", starlette_user.email)

        sentry_scope.user = user_info


def patch_authentication_middleware(middleware_class):
    # type: (Any) -> None
    """
    Add user information to Sentry scope.
    """
    old_call = middleware_class.__call__

    async def _sentry_authenticationmiddleware_call(self, scope, receive, send):
        # type: (Dict[str, Any], Dict[str, Any], Callable[[], Awaitable[Dict[str, Any]]], Callable[[Dict[str, Any]], Awaitable[None]]) -> None
        await old_call(self, scope, receive, send)
        _add_user_to_sentry_scope(scope)

    middleware_class.__call__ = _sentry_authenticationmiddleware_call


def patch_middlewares():
    # type: () -> None
    """
    Patches Starlettes `Middleware` class to record
    spans for every middleware invoked.
    """
    old_middleware_init = Middleware.__init__

    def _sentry_middleware_init(self, cls, **options):
        # type: (Any, Any, Any) -> None
        span_enabled_cls = _enable_span_for_middleware(cls)
        old_middleware_init(self, span_enabled_cls, **options)

        if cls == AuthenticationMiddleware:
            patch_authentication_middleware(cls)

        if cls == ExceptionMiddleware:
            patch_exception_middleware(cls)

    Middleware.__init__ = _sentry_middleware_init

    old_build_middleware_stack = Starlette.build_middleware_stack

    def _sentry_build_middleware_stack(self):
        # type: (Callable[..., Any]) -> Callable[..., Any]
        """
        Adds `SentryStarletteMiddleware` to the
        middleware stack of the Starlette application.
        """
        app = old_build_middleware_stack(self)
        app = SentryStarletteMiddleware(app=app)
        return app

    Starlette.build_middleware_stack = _sentry_build_middleware_stack


def patch_asgi_app():
    # type: () -> None
    """
    Instrument Starlette ASGI app using the SentryAsgiMiddleware.
    """
    old_app = Starlette.__call__

    async def _sentry_patched_asgi_app(self, scope, receive, send):
        # type: (Dict[str, Any], Dict[str, Any], Callable[[], Awaitable[Dict[str, Any]]], Callable[[Dict[str, Any]], Awaitable[None]]) -> None
        if Hub.current.get_integration(StarletteIntegration) is None:
            return await old_app(self, scope, receive, send)

        middleware = SentryAsgiMiddleware(
            lambda *a, **kw: old_app(self, *a, **kw),
            mechanism_type=StarletteIntegration.identifier,
        )
        middleware.__call__ = middleware._run_asgi3
        return await middleware(scope, receive, send)

    Starlette.__call__ = _sentry_patched_asgi_app


class StarletteRequestExtractor:
    """
    Extracts useful information from the Starlette request
    (like form data or cookies) and adds it to the Sentry event.
    """

    request = None  # type: Request

    def __init__(self, request):
        # type: (StarletteRequestExtractor, Request) -> None
        self.request = request

    async def extract_request_info(self):
        # type: (StarletteRequestExtractor) -> Optional[Dict[str, Any]]
        client = Hub.current.client
        if client is None:
            return None

        data = None  # type: Union[Dict[str, Any], AnnotatedValue, None]

        content_length = await self.content_length()
        request_info = {}  # type: Dict[str, Any]

        if _should_send_default_pii():
            request_info["cookies"] = self.cookies()

        if not request_body_within_bounds(client, content_length):
            data = AnnotatedValue(
                "",
                {"rem": [["!config", "x", 0, content_length]], "len": content_length},
            )
        else:
            parsed_body = await self.parsed_body()
            if parsed_body is not None:
                data = parsed_body
            elif await self.raw_data():
                data = AnnotatedValue(
                    "",
                    {"rem": [["!raw", "x", 0, content_length]], "len": content_length},
                )
            else:
                data = None

        if data is not None:
            request_info["data"] = data

        return request_info

    async def content_length(self):
        # type: (StarletteRequestExtractor) -> int
        raw_data = await self.raw_data()
        if raw_data is None:
            return 0
        return len(raw_data)

    def cookies(self):
        # type: (StarletteRequestExtractor) -> Dict[str, Any]
        return self.request.cookies

    async def raw_data(self):
        # type: (StarletteRequestExtractor) -> Any
        return await self.request.body()

    async def form(self):
        # type: (StarletteRequestExtractor) -> Any
        """
        curl -X POST http://localhost:8000/upload/somethign -H "Content-Type: application/x-www-form-urlencoded" -d "username=kevin&password=welcome123"
        curl -X POST http://localhost:8000/upload/somethign  -F username=Julian -F password=hello123
        """
        return await self.request.form()

    def is_json(self):
        # type: (StarletteRequestExtractor) -> bool
        return _is_json_content_type(self.request.headers.get("content-type"))

    async def json(self):
        # type: (StarletteRequestExtractor) -> Optional[Dict[str, Any]]
        """
        curl -X POST localhost:8000/upload/something -H 'Content-Type: application/json' -d '{"login":"my_login","password":"my_password"}'
        """
        if not self.is_json():
            return None

        return await self.request.json()

    async def parsed_body(self):
        # type: (StarletteRequestExtractor) -> Any
        """
        curl -X POST http://localhost:8000/upload/somethign  -F username=Julian -F password=hello123 -F photo=@photo.jpg
        """
        form = await self.form()
        if form:
            data = {}
            for key, val in iteritems(form):
                if isinstance(val, UploadFile):
                    size = len(await val.read())
                    data[key] = AnnotatedValue(
                        "", {"len": size, "rem": [["!raw", "x", 0, size]]}
                    )
                else:
                    data[key] = val

            return data

        return await self.json()


class SentryStarletteMiddleware(SentryAsgiMiddleware):
    def __init__(self, app, dispatch=None):
        # type: (SentryStarletteMiddleware, Any) -> None
        self.app = app

    async def __call__(self, scope, receive, send):
        # type: (SentryStarletteMiddleware, Dict[str, Any], Callable[[], Awaitable[Dict[str, Any]]], Callable[[Dict[str, Any]], Awaitable[None]]) -> Any
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        hub = Hub.current
        integration = hub.get_integration(StarletteIntegration)
        if integration is None:
            return

        with hub.configure_scope() as sentry_scope:
            request = Request(scope, receive=receive, send=send)

            extractor = StarletteRequestExtractor(request)
            info = await extractor.extract_request_info()

            def _make_request_event_processor(req, integration):
                # type: (Any, Any) -> Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]
                def inner(event, hint):
                    # type: (Dict[str, Any], Dict[str, Any]) -> Dict[str, Any]

                    # Extract information from request
                    request_info = event.get("request", {})
                    if info:
                        if "cookies" in info:
                            request_info["cookies"] = info["cookies"]
                        if "data" in info:
                            request_info["data"] = info["data"]
                    event["request"] = request_info

                    # Set transaction name
                    if "router" in req.scope:
                        router = req.scope["router"]
                        for route in router.routes:
                            match = route.matches(req.scope)
                            if match[0] == Match.FULL:
                                if integration.transaction_style == "endpoint":
                                    event["transaction"] = match[1]["endpoint"].__name__
                                elif integration.transaction_style == "url":
                                    event["transaction"] = route.path

                    return event

                return inner

            sentry_scope._name = StarletteIntegration.identifier
            sentry_scope.add_event_processor(
                _make_request_event_processor(request, integration)
            )

            await self.app(scope, receive, send)
