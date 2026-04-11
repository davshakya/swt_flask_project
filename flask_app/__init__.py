__all__ = ["app"]


def __getattr__(name):
    if name == "app":
        from .server import app

        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
