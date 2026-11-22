"""
VPN panel integrations. A server is 3x-ui (2.x or 3.x), Marzban,
Remnawave or our own VPN Seller Node; panel_for() returns the right client,
all have the same methods.
"""

from . import marzban, node, remnawave
from .marzban import MarzbanPanel
from .node import NodePanel
from .remnawave import RemnawavePanel
from .xui import ClientSpec, PanelError, XuiPanel, decode_sub, parse_ids  # noqa: F401
from .xui import drop_session as _drop_xui
from .xui import normalize_url as _normalize_xui

KINDS = ("3xui", "marzban", "remnawave", "node")
# Kinds whose inbounds are named by tags/uuids, not 3x-ui numbers.
TAG_KINDS = ("marzban", "remnawave", "node")


def panel_for(server, timeout: float = 15.0):
    kind = getattr(server, "kind", "")
    if kind == "marzban":
        return MarzbanPanel(server, timeout=timeout)
    if kind == "remnawave":
        return RemnawavePanel(server, timeout=timeout)
    if kind == "node":
        return NodePanel(server, timeout=timeout)
    return XuiPanel(server, timeout=timeout)


def normalize_url(url: str, kind: str = "3xui") -> str:
    if kind == "marzban":
        return marzban.normalize_url(url)
    if kind == "remnawave":
        return remnawave.normalize_url(url)
    if kind == "node":
        return node.normalize_url(url)
    return _normalize_xui(url)


def drop_session(server_id: int) -> None:
    _drop_xui(server_id)
    marzban.drop(server_id)
    remnawave.drop(server_id)
    node.drop(server_id)
