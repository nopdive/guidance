"""User interface and other visual UX considerations."""

# TODO(nopdive): implement stdout renderer

from ._message import GuidanceMessage, TraceMessage, ResetDisplayMessage, HeartbeatMessage
from ._renderer import AutoRenderer, LegacyHtmlRenderer
from ._trace import trace_node_to_str, display_trace_tree, trace_node_to_html