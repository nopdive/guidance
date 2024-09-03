"""User interface and other visual UX considerations."""

from typing import Any, Callable, Dict, Optional, Pattern
from pydantic import BaseModel
import re

import logging
logger = logging.getLogger(__name__)

ipython_imported = True
try:
    from IPython.display import clear_output, display, HTML
    from IPython import get_ipython
except ImportError:
    ipython_imported = False

import pkg_resources

MODEL_TOPIC = "/model/"
SRC_DOC_TEMPLATE = open(pkg_resources.resource_filename('guidance', 'resources/graphpaper-inline.html'), 'r').read()


class Message(BaseModel):
    message_type: str = ""

    def __init__(self, **kwargs):
        kwargs["message_type"] = self.__class__.__name__
        super().__init__(**kwargs)


class ModelUpdateMessage(Message):
    model_id: int
    parent_model_id: int
    content_type: str
    content: str = ""
    prob: float = 0
    token_count: int = 0
    is_generated: int = 0
    is_special: int = 0
    role: str = ""


class ModelTrackingMessage(Message):
    model_id: int
    parent_model_id: int


class ResetDisplayMessage(Message):
    pass


def _create_stitch_widget():
    from stitch import StitchWidget

    w = StitchWidget()
    w.initial_width = '100%'
    w.initial_height = 'auto'
    w.srcdoc = SRC_DOC_TEMPLATE
    return w


class UpdateController:
    def __init__(self) -> None:
        self._prev_msg_id: str = ""
        self._prev_exec_count: int = -1
        self._model_path: list = []
        self._model_messages: list[list[Message]] = []

    def update(self, message: Message) -> tuple[list[Message], bool, bool]:
        # Filter out irrelevant messages
        if not isinstance(message, (ModelTrackingMessage, ModelUpdateMessage)):
            return ([], False, False)

        need_refresh = False
        need_new_display = False
        if len(self._model_messages) == 0:
            logger.debug("NEED_RESET: empty")
            need_refresh = True
            need_new_display = True
        else:
            # If we diverge from the model path, truncate and reset
            last_model_message = self._model_messages[-1][-1]
            if last_model_message.model_id not in [message.model_id, message.parent_model_id]:
                logger.debug(f"NEED_RESET: diverge {message.parent_model_id, message.model_id, last_model_message.model_id}")
                logger.debug(last_model_message)
                logger.debug(message)
                need_refresh = True

                # Truncate path that is no longer used
                parent_idx = -1
                for idx, messages in enumerate(self._model_messages):
                    if len([True for x in messages if x.model_id == message.parent_model_id]) > 0:
                        parent_idx = idx
                        break
                if parent_idx == -1:
                    logger.debug(f"PARENT_NOT_FOUND: {message.parent_model_id, message.model_id}")

                self._model_path = self._model_path[:parent_idx]
                self._model_messages = self._model_messages[:parent_idx]

        # If we are in a new Jupyter cell or execution, reset
        if ipython_imported:
            ipy = get_ipython()
            msg_id = ipy.get_parent()['msg_id']
            exec_count = ipy.execution_count
            if msg_id != self._prev_msg_id or exec_count != self._prev_exec_count:
                need_refresh = True
                need_new_display = True
                logger.debug("NEED_RESET: jupyter")
                self._prev_msg_id = msg_id
                self._prev_exec_count = exec_count
                self._model_path = []
                self._model_messages = []
        
        out_messages = []
        if need_refresh:
            out_messages.append(ResetDisplayMessage())
            for messages in self._model_messages:
                out_messages.extend(messages)
            
        # Add current message
        if len(self._model_path) == 0 or self._model_path[-1] != message.model_id:
            logger.debug("NEW_MESSAGE_BLOCK")
            self._model_path.append(message.model_id)
            self._model_messages.append([message])

        else:
            self._model_messages[-1].append(message)
        out_messages.append(message)

        filt_out_messages = []
        for out_message in out_messages:
            # Filter out tracking messages (client does not need to see this)
            if isinstance(out_message, ModelTrackingMessage):
                continue
            filt_out_messages.append(out_message)

        return filt_out_messages, need_new_display


class Renderer:
    def update(self, message: Message) -> None:
        raise NotImplementedError


class JupyterWidgetRenderer(Renderer):
    def __init__(self) -> None:
        self._jupyter_widget = None
        self._update_controller = UpdateController()

    def update(self, message:Message) -> None:
        out_messages, need_new_display = self._update_controller.update(message)
        if need_new_display:
            logger.debug("NEW_DISPLAY")
            self._jupyter_widget = _create_stitch_widget()
            clear_output(wait=True)
            display(self._jupyter_widget)

        for out_message in out_messages:
            message_json = out_message.model_dump_json(indent=2)
            logger.debug(f"OUT_MSG: {message_json}")
            self._jupyter_widget.kernelmsg = message_json


class JupyterHTMLRenderer(Renderer):
    def __init__(self) -> None:
        self._update_controller = UpdateController()
        self._formatted = []
    
    def update(self, message: Message) -> None:
        format_msg = lambda message: f"<span style='background-color: rgba({165*(1-message.prob)}, {165*(message.prob)}, 0, 0.15); border-radius: 3px;'>{message.content}</span>"

        out_messages, _ = self._update_controller.update(message)
        formatted = []
        for out_message in out_messages:
            if isinstance(out_message, ResetDisplayMessage):
                self._formatted = []
                formatted = []
            elif isinstance(out_message, ModelUpdateMessage):
                formatted.append(format_msg(out_message))

        self._formatted.extend(formatted)

        clear_output(wait=True)
        display(HTML("".join(self._formatted)))


class SubscriberEntry(BaseModel):
    pattern: Pattern
    cb: Callable[[Any], None]


class TopicExchange:
    def __init__(self) -> None:
        self._subscribers: Dict[str, SubscriberEntry] = {}
        self._next_id: int = 0
        self._id_generator = IdGenerator()
    
    def gen_id(self) -> int:
        return self._id_generator.gen()

    def publish(self, route: str, msg: Any) -> None:
        for subscriber_entry in self._subscribers.values():
            if subscriber_entry.pattern.match(route):
                subscriber_entry.cb(msg)

    def subscribe(self, route: str, cb: Callable[[Any], None], identifier: Optional[int] = None) -> int:
        if identifier is None:
            identifier = self.gen_id()
        key = f"{identifier}:{route}"
        pattern = re.compile(route)
        self._subscribers[key] = SubscriberEntry(pattern=pattern, cb=cb)
        return identifier

    def unsubscribe(self, route: str, identifier: int) -> None:
        key = f"{identifier}:{route}"
        del self._subscribers[key]


class IdGenerator:
    def __init__(self):
        self._next_id = 0

    def gen(self) -> int:
        _id = self._next_id
        self._next_id += 1
        return _id


class VisualRegistry:
    __topic_exchange: TopicExchange = None
    __renderer: Renderer = None
    __id_generator: IdGenerator = None

    @classmethod
    def renderer(cls) -> Renderer:
        if cls.__renderer is None:
            r = JupyterWidgetRenderer()
            # r = JupyterHTMLRenderer()

            _ = cls.topic_exchange().subscribe(f"{MODEL_TOPIC}.*", r.update)
            cls.__renderer = r

        return cls.__topic_exchange

    @classmethod
    def topic_exchange(cls) -> TopicExchange:
        if cls.__topic_exchange is None:
            cls.__topic_exchange = TopicExchange()
        return cls.__topic_exchange

    @classmethod
    def id_generator(cls) -> IdGenerator:
        if cls.__id_generator is None:
            cls.__id_generator = IdGenerator()
        return cls.__id_generator