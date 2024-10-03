import logging
import weakref
from typing import Optional, Callable
from pydantic import BaseModel
from pydantic_core import from_json
import json
from asyncio import Queue

from ..trace import TraceHandler
from ..visual import GuidanceMessage, TraceMessage, ResetDisplayMessage
from ._trace import trace_node_to_html

try:
    from IPython.display import clear_output, display, HTML
    from IPython import get_ipython

    ipython_imported = True
except ImportError:
    ipython_imported = False

try:
    import stitch

    stitch_installed = True
except ImportError:
    stitch_installed = False

logger = logging.getLogger(__name__)


class RenderUpdate(BaseModel):
    messages: list = []
    need_new_display: bool = False


class UpdateController:
    def __init__(self, trace_handler: TraceHandler):
        self._trace_handler = trace_handler
        self._messages: list[GuidanceMessage] = []

        self._prev_trace_id: Optional[int] = None
        self._prev_cell_msg_id: Optional[int] = None
        self._prev_cell_exec_count: Optional[int] = None

    def update(self, message: GuidanceMessage) -> RenderUpdate:
        if not isinstance(message, TraceMessage):
            return RenderUpdate()

        trace_node = self._trace_handler[message.trace_id]
        need_reset = False
        need_new_display = False

        if len(self._messages) == 0:
            # If no messages, reset
            logger.debug("NEED_RESET:empty")
            need_reset = True
            need_new_display = True
        else:
            # If we diverge from the model path, truncate and reset
            *_, last_trace_message = (x for x in reversed(self._messages) if isinstance(x, TraceMessage))
            last_trace_node = self._trace_handler[last_trace_message.trace_id]

            if last_trace_node not in trace_node.path():
                logger.debug(f"NEED_RESET:divergence:curr:{trace_node}")
                logger.debug(f"NEED_RESET:divergence:prev:{last_trace_node}")
                need_reset = True

                # Truncate path that is no longer used by current trace node
                ancestor_idx = -1
                ancestors = set(trace_node.ancestors())
                for idx, prev_message in enumerate(self._messages):
                    if isinstance(prev_message, TraceMessage):
                        if self._trace_handler[prev_message.trace_id] in ancestors:
                            ancestor_idx = idx
                if ancestor_idx == -1:
                    logger.debug(f"PARENT_NOT_FOUND:{trace_node}")
                    self._messages.clear()
                else:
                    self._messages = self._messages[:ancestor_idx]

        # If we are in a new Jupyter cell or execution, reset
        if ipython_imported and get_ipython() is not None:
            ipy = get_ipython()
            cell_msg_id = ipy.get_parent()["msg_id"]
            cell_exec_count = ipy.execution_count
            if (
                cell_msg_id != self._prev_cell_msg_id
                or cell_exec_count != self._prev_cell_exec_count
            ):
                need_reset = True
                need_new_display = True
                logger.debug(f"NEED_RESET:jupyter:{cell_msg_id}|{cell_exec_count}")
                self._prev_cell_msg_id = cell_msg_id
                self._prev_cell_exec_count = cell_exec_count
                self._messages = []

        out_messages = []
        # Add previous messages if reset required
        if need_reset:
            out_messages.append(ResetDisplayMessage())
            out_messages.extend(self._messages)
        # Add current message
        out_messages.append(message)
        self._messages.append(message)

        return RenderUpdate(messages=out_messages, need_new_display=need_new_display)


class Renderer:
    """Renders guidance model to a visual medium."""

    def __init__(self):
        self._observers = []

    def notify(self, message: GuidanceMessage):
        for observer in self._observers:
            observer(message)

    def subscribe(self, callback: Callable[[GuidanceMessage], None]) -> None:
        self._observers.append(callback)

    def update(self, message: GuidanceMessage) -> None:
        raise NotImplementedError("Update not implemented.")


class LegacyHtmlRenderer(Renderer):
    """Original HTML renderer for guidance."""

    def __init__(self, trace_handler: TraceHandler) -> None:
        self._trace_handler = trace_handler
        super().__init__()

    def update(self, message: GuidanceMessage) -> None:
        if not isinstance(message, TraceMessage):
            return

        trace_node = self._trace_handler[message.trace_id]
        if trace_node is not None:
            clear_output(wait=True)
            display(HTML(trace_node_to_html(trace_node, prettify_roles=False)))


def _create_stitch_widget():
    from stitch import StitchWidget
    import pkg_resources

    if _create_stitch_widget.src_doc_template is None:
        with open(
            pkg_resources.resource_filename("guidance", "resources/graphpaper-inline.html"), "r"
        ) as f:
            _create_stitch_widget.src_doc_template = f.read()
    w = StitchWidget()
    w.initial_width = "100%"
    w.initial_height = "auto"
    w.srcdoc = _create_stitch_widget.src_doc_template

    return w


_create_stitch_widget.src_doc_template = None


from ._async import run_async_task, ThreadSafeAsyncCondVar, async_loop


class JupyterWidgetRenderer(Renderer):
    def __init__(self, trace_handler: TraceHandler) -> None:
        self._jupyter_widget = None
        self._update_controller = UpdateController(trace_handler)

        self._loop = async_loop()
        self._send_queue = Queue(loop=self._loop)
        self._recv_queue = Queue(loop=self._loop)
        self._client_ready = ThreadSafeAsyncCondVar(async_loop())

        run_async_task(self.handle_recv_messages())
        run_async_task(self.handle_send_messages())

        super().__init__()

    def update(self, message: GuidanceMessage) -> None:
        display_update = self._update_controller.update(message)

        if display_update.need_new_display:
            logger.debug(f"NEED_NEW_DISPLAY:new widget")
            self._jupyter_widget = _create_stitch_widget()
            self._jupyter_widget.observe(self._client_msg_cb, names='clientmsg')

            # clear_output(wait=True)
            display(self._jupyter_widget)

        for out_message in display_update.messages:
            self._loop.call_soon_threadsafe(self._send_queue.put_nowait, out_message)

    def _client_msg_cb(self, change: dict) -> None:
        # NOTE(nopdive): Widget callbacks do not print to stdout/stderr nor module log.

        new_val = change['new']
        try:
            msg_di = json.loads(new_val)
            if msg_di.get('class_name', None) == 'HeartbeatMessage':
                self._client_ready.notify()
            self._loop.call_soon_threadsafe(self._recv_queue.put_nowait, new_val)
        except Exception as e:
            self._jupyter_widget.log.error(f"Failed to process client message:{new_val}:{repr(e)}")

    async def handle_recv_messages(self):
        logger.debug("RECV:init")
        while True:
            value = await self._recv_queue.get()
            logger.debug(f"RECV:raw:{value}")
            message = from_json(value)
            logger.debug(f"RECV:msg:{message}")
            self.notify(message)
            self._recv_queue.task_done()

    async def handle_send_messages(self):
        logger.debug("SEND:init")
        # Wait until ready
        await self._client_ready.wait()
        logger.debug("SEND:ready")

        while True:
            message = await self._send_queue.get()
            # logger.debug(f"SEND:msg:{message}")
            message_json = message.model_dump_json(indent=2, serialize_as_any=True)
            # logger.debug(f"SEND:json:{message_json}")
            self._jupyter_widget.kernelmsg = message_json
            self._send_queue.task_done()

class AutoRenderer(Renderer):
    def __init__(self, trace_handler: TraceHandler):
        if stitch_installed:
            self._renderer = JupyterWidgetRenderer(trace_handler=trace_handler)
        else:
            self._renderer = LegacyHtmlRenderer(trace_handler=trace_handler)
        super().__init__()

    def notify(self, message: GuidanceMessage):
        self._renderer.notify(message)

    def subscribe(self, callback: Callable[[GuidanceMessage], None]) -> None:
        self._renderer.subscribe(callback)

    def update(self, message: GuidanceMessage) -> None:
        self._renderer.update(message)