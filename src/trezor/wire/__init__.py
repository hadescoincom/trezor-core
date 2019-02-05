import protobuf
from trezor import log, loop, messages, utils, workflow
from trezor.wire import codec_v1
from trezor.wire.errors import *

from apps.common import seed

if False:
    from typing import (
        Any,
        Awaitable,
        Callable,
        Dict,
        Iterable,
        List,
        Tuple,
    )  # noqa: F401
    from trezorio import WireInterface  # noqa: F401

workflow_handlers = {}  # type: Dict[int, Tuple[Callable, Iterable]]


def add(mtype: int, pkgname: str, modname: str, namespace: List = None) -> None:
    """Shortcut for registering a dynamically-imported Protobuf workflow."""
    if namespace is not None:
        register(
            mtype,
            protobuf_workflow,
            keychain_workflow,
            namespace,
            import_workflow,
            pkgname,
            modname,
        )
    else:
        register(mtype, protobuf_workflow, import_workflow, pkgname, modname)


def register(mtype: int, handler: Callable, *args: Any) -> None:
    """Register `handler` to get scheduled after `mtype` message is received."""
    if isinstance(mtype, type) and issubclass(mtype, protobuf.MessageType):
        mtype = mtype.MESSAGE_WIRE_TYPE
    if mtype in workflow_handlers:
        raise KeyError
    workflow_handlers[mtype] = (handler, args)


def setup(iface: WireInterface) -> None:
    """Initialize the wire stack on passed USB interface."""
    loop.schedule(session_handler(iface, codec_v1.SESSION_ID))


class Context:
    def __init__(self, iface: WireInterface, sid: int) -> None:
        self.iface = iface
        self.sid = sid

    async def call(
        self, msg: protobuf.MessageType, *types: int
    ) -> protobuf.MessageType:
        """
        Reply with `msg` and wait for one of `types`. See `self.write()` and
        `self.read()`.
        """
        await self.write(msg)
        del msg
        return await self.read(types)

    async def read(self, types: Tuple[int, ...]) -> protobuf.MessageType:
        """
        Wait for incoming message on this wire context and return it.  Raises
        `UnexpectedMessageError` if the message type does not match one of
        `types`; and caller should always make sure to re-raise it.
        """
        reader = self.getreader()

        if __debug__:
            log.debug(
                __name__, "%s:%x read: %s", self.iface.iface_num(), self.sid, types
            )

        await reader.aopen()  # wait for the message header

        # if we got a message with unexpected type, raise the reader via
        # `UnexpectedMessageError` and let the session handler deal with it
        if reader.type not in types:
            raise UnexpectedMessageError(reader)

        # look up the protobuf class and parse the message
        pbtype = messages.get_type(reader.type)
        return await protobuf.load_message(reader, pbtype)

    async def write(self, msg: protobuf.MessageType) -> None:
        """
        Write a protobuf message to this wire context.
        """
        writer = self.getwriter()

        if __debug__:
            log.debug(
                __name__, "%s:%x write: %s", self.iface.iface_num(), self.sid, msg
            )

        # get the message size
        fields = msg.get_fields()
        size = protobuf.count_message(msg, fields)

        # write the message
        writer.setheader(msg.MESSAGE_WIRE_TYPE, size)
        await protobuf.dump_message(writer, msg, fields)
        await writer.aclose()

    def wait(self, *tasks: Awaitable) -> Any:
        """
        Wait until one of the passed tasks finishes, and return the result,
        while servicing the wire context.  If a message comes until one of the
        tasks ends, `UnexpectedMessageError` is raised.
        """
        return loop.spawn(self.read(()), *tasks)

    def getreader(self) -> codec_v1.Reader:
        return codec_v1.Reader(self.iface)

    def getwriter(self) -> codec_v1.Writer:
        return codec_v1.Writer(self.iface)


class UnexpectedMessageError(Exception):
    def __init__(self, reader: codec_v1.Reader) -> None:
        super().__init__()
        self.reader = reader


async def session_handler(iface: WireInterface, sid: int) -> None:
    reader = None
    ctx = Context(iface, sid)
    while True:
        try:
            # wait for new message, if needed, and find handler
            if not reader:
                reader = ctx.getreader()
                await reader.aopen()
            try:
                handler, args = workflow_handlers[reader.type]
            except KeyError:
                handler, args = unexpected_msg, ()

            m = utils.unimport_begin()
            w = handler(ctx, reader, *args)
            try:
                workflow.onstart(w)
                await w
            finally:
                workflow.onclose(w)
                utils.unimport_end(m)

        except UnexpectedMessageError as exc:
            # retry with opened reader from the exception
            reader = exc.reader
            continue
        except Error as exc:
            # we log wire.Error as warning, not as exception
            if __debug__:
                log.warning(__name__, "failure: %s", exc.message)
        except Exception as exc:
            # sessions are never closed by raised exceptions
            if __debug__:
                log.exception(__name__, exc)

        # read new message in next iteration
        reader = None


async def protobuf_workflow(
    ctx: Context, reader: codec_v1.Reader, handler: Awaitable, *args: Any
) -> None:
    from trezor.messages.Failure import Failure

    req = await protobuf.load_message(reader, messages.get_type(reader.type))
    try:
        res = await handler(ctx, req, *args)
    except UnexpectedMessageError:
        # session handler takes care of this one
        raise
    except Error as exc:
        # respond with specific code and message
        await ctx.write(Failure(code=exc.code, message=exc.message))
        raise
    except Exception:
        # respond with a generic code and message
        await ctx.write(
            Failure(code=FailureType.FirmwareError, message="Firmware error")
        )
        raise
    if res:
        # respond with a specific response
        await ctx.write(res)


async def keychain_workflow(
    ctx: Context,
    req: protobuf.MessageType,
    namespace: List,
    handler: Callable[[Context, protobuf.MessageType, Any], Awaitable],
    *args: Any
) -> Any:
    keychain = await seed.get_keychain(ctx, namespace)
    args += (keychain,)
    try:
        return await handler(ctx, req, *args)
    finally:
        keychain.__del__()


def import_workflow(
    ctx: Context, req: protobuf.MessageType, pkgname: str, modname: str, *args: Any
) -> Any:
    modpath = "%s.%s" % (pkgname, modname)
    module = __import__(modpath, None, None, (modname,), 0)
    handler = getattr(module, modname)
    return handler(ctx, req, *args)


async def unexpected_msg(ctx: Context, reader: codec_v1.Reader) -> None:
    from trezor.messages.Failure import Failure

    # receive the message and throw it away
    while reader.size > 0:
        buf = bytearray(reader.size)
        await reader.areadinto(buf)

    # respond with an unknown message error
    await ctx.write(
        Failure(code=FailureType.UnexpectedMessage, message="Unexpected message")
    )
