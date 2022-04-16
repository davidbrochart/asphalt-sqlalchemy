from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional, Type, Union, cast

from asphalt.core import (
    Component,
    Context,
    context_teardown,
    executor,
    resolve_reference,
)
from sqlalchemy.engine.url import URL, make_url
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.future.engine import Connection, Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import Pool
from typeguard import check_argument_types

from asphalt.sqlalchemy.utils import apply_sqlite_hacks

logger = logging.getLogger(__name__)


class SQLAlchemyComponent(Component):
    """
    Creates resources necessary for accessing relational databases using SQLAlchemy.

    This component supports both synchronous (``sqlite``, ``psycopg2``, etc.) and
    asynchronous (``asyncpg``, ``asyncmy``, etc.) engines, and the provided resources
    differ based on that.

    For synchronous engines, the following resources are provided:

    * :class:`~sqlalchemy.future.engine.Engine`
    * :class:`~sqlalchemy.orm.session.sessionmaker`
    * :class:`~sqlalchemy.orm.session.Session`

    For asynchronous engines, the following resources are provided:

    * :class:`~sqlalchemy.ext.asyncio.AsyncEngine`
    * :class:`~sqlalchemy.orm.session.sessionmaker`
    * :class:`~sqlalchemy.ext.asyncio.AsyncSession`

    .. note:: The following options will always be set to fixed values in sessions:

      * ``expire_on_commit``: ``False``
      * ``future``: ``True``

    :param url: the connection url passed to
        :func:`~sqlalchemy.future.engine.create_engine`
        (can also be a dictionary of :class:`~sqlalchemy.engine.url.URL` keyword
        arguments)
    :param bind: a connection or engine to use instead of creating a new engine
    :param engine_args: extra keyword arguments passed to
        :func:`sqlalchemy.future.engine.create_engine` or
        :func:`sqlalchemy.ext.asyncio.create_engine`
    :param session_args: extra keyword arguments passed to
        :class:`~sqlalchemy.orm.session.Session` or
        :class:`~sqlalchemy.ext.asyncio.AsyncSession`
    :param commit_executor_workers: maximum number of worker threads to use for tearing
        down synchronous sessions (default: 5; ignored for asynchronous engines)
    """

    commit_executor: ThreadPoolExecutor

    def __init__(
        self,
        *,
        url: Union[str, URL, Dict[str, Any]] = None,
        bind: Union[Connection, Engine, AsyncConnection, AsyncEngine] = None,
        engine_args: Optional[Dict[str, Any]] = None,
        session_args: Optional[Dict[str, Any]] = None,
        commit_executor_workers: int = 5,
        poolclass: Union[str, Pool] = None,
        resource_name: str = "default",
    ):
        check_argument_types()
        self.resource_name = resource_name
        self.commit_executor_workers = commit_executor_workers
        engine_args = engine_args or {}
        session_args = session_args or {}
        session_args["expire_on_commit"] = False
        session_args["future"] = True

        if bind:
            self.bind = bind
            self.engine = bind.engine
        else:
            if isinstance(url, dict):
                url = URL.create(**url)
            elif isinstance(url, str):
                url = make_url(url)
            elif url is None:
                raise TypeError('both "url" and "bind" cannot be None')

            # This is a hack to get SQLite to play nice with asphalt-sqlalchemy's
            # juggling of connections between multiple threads. The same connection
            # should, however, never be used in multiple threads at once.
            if url.get_dialect().name == "sqlite":
                connect_args = engine_args.setdefault("connect_args", {})
                connect_args.setdefault("check_same_thread", False)

            if isinstance(poolclass, str):
                poolclass = resolve_reference(poolclass)

            pool_class = cast(Type[Pool], poolclass)
            try:
                self.engine = self.bind = create_async_engine(
                    url, poolclass=pool_class, **engine_args
                )
            except InvalidRequestError:
                self.engine = self.bind = create_engine(
                    url, poolclass=pool_class, **engine_args
                )

            if url.get_dialect().name == "sqlite":
                apply_sqlite_hacks(self.engine)

        if isinstance(self.engine, AsyncEngine):
            session_args.setdefault("class_", AsyncSession)

        self.sessionmaker = sessionmaker(bind=self.bind, **session_args)

    def create_session(self, ctx: Context) -> Session:
        @executor(self.commit_executor)
        def teardown_session(exception: Optional[BaseException]) -> None:
            try:
                if session.in_transaction():
                    if exception is None:
                        session.commit()
                    else:
                        session.rollback()
            finally:
                session.close()
                del session.info["ctx"]

        session = self.sessionmaker(info={"ctx": ctx})
        ctx.add_teardown_callback(teardown_session, pass_exception=True)
        return session

    def create_async_session(self, ctx: Context) -> AsyncSession:
        async def teardown_session(exception: Optional[BaseException]) -> None:
            try:
                if session.in_transaction():
                    if exception is None:
                        await session.commit()
                    else:
                        await session.rollback()
            finally:
                await session.close()
                del session.info["ctx"]

        session: AsyncSession = self.sessionmaker(info={"ctx": ctx})
        ctx.add_teardown_callback(teardown_session, pass_exception=True)
        return session

    @context_teardown
    async def start(self, ctx: Context):
        ctx.add_resource(self.engine, self.resource_name)
        ctx.add_resource(self.sessionmaker, self.resource_name)
        if isinstance(self.engine, AsyncEngine):
            ctx.add_resource_factory(
                self.create_async_session,
                [AsyncSession],
                self.resource_name,
            )
        else:
            self.commit_executor = ThreadPoolExecutor(self.commit_executor_workers)
            ctx.add_teardown_callback(self.commit_executor.shutdown)

            ctx.add_resource_factory(
                self.create_session,
                [Session],
                self.resource_name,
            )

        logger.info(
            "Configured SQLAlchemy resources (%s; dialect=%s, driver=%s)",
            self.resource_name,
            self.bind.dialect.name,
            self.bind.dialect.driver,
        )

        yield

        if isinstance(self.bind, Engine):
            self.bind.dispose()
        elif isinstance(self.bind, AsyncEngine):
            await self.bind.dispose()

        logger.info("SQLAlchemy resources (%s) shut down", self.resource_name)
