from __future__ import annotations


def create_app():
    from ..app.factory import create_app as factory_create_app

    return factory_create_app()


__all__ = ["create_app"]
