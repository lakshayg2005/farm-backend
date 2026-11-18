"""Pytest fixtures and environment setup for notification service tests."""

import sys
import types

# Gracefully provide a mock for aio_pika if running in minimal test environment without RabbitMQ
if "aio_pika" not in sys.modules:
    try:
        import aio_pika  # noqa: F401
    except ImportError:
        aio_pika_mock = types.ModuleType("aio_pika")
        aio_pika_mock.__path__ = []
        aio_pika_mock.Message = type("Message", (), {})
        aio_pika_mock.DeliveryMode = type("DeliveryMode", (), {"PERSISTENT": 2})

        abc_mock = types.ModuleType("aio_pika.abc")
        abc_mock.AbstractRobustConnection = type("AbstractRobustConnection", (), {})
        abc_mock.AbstractChannel = type("AbstractChannel", (), {})
        abc_mock.AbstractExchange = type("AbstractExchange", (), {})
        abc_mock.AbstractQueue = type("AbstractQueue", (), {})
        abc_mock.AbstractIncomingMessage = type("AbstractIncomingMessage", (), {})

        aio_pika_mock.abc = abc_mock
        sys.modules["aio_pika"] = aio_pika_mock
        sys.modules["aio_pika.abc"] = abc_mock

# Mock jwt if pyjwt is not installed in the local environment
if "jwt" not in sys.modules:
    try:
        import jwt  # noqa: F401
    except ImportError:
        jwt_mock = types.ModuleType("jwt")
        jwt_mock.PyJWTError = Exception
        jwt_mock.decode = lambda *a, **k: {}
        sys.modules["jwt"] = jwt_mock
