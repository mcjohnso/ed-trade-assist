"""In-game overlay output for EDTradeAssist.

The overlay is drawn by a SEPARATE plugin the user may or may not have installed
(EDMCOverlay, or the EDMCModernOverlay fork). `edmcoverlay` is never bundled, so
everything here is defensive: import lazily, tolerate absence, never raise, and
expose `available` so load.py knows whether the EDMC panel is the only output.

Layout note: the whole block is sent as ONE newline-joined message rather than a
message per row. The overlay server lays out lines using its own font metrics, so
a hand-computed y per row drifts out of place under display scaling.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)

#: Deliberately NOT the edta_ module prefix. EDMCModernOverlay applies a
#: position-normalising group transform to any message id matching a registered
#: plugin group; an unregistered prefix keeps the x/y we send honoured directly.
BLOCK_ID = "tradeasst_block"

#: Longer than the gap between updates, so the block does not blink out while
#: docked (Status.json goes quiet at a station).
TTL = 8

COLOR_BUY = "#ff9f1c"
COLOR_SELL = "#3ddc84"
COLOR_WARN = "#ff4040"

DEFAULT_X = 20
DEFAULT_Y = 40

#: Font sizes the overlay servers understand. EDMCModernOverlay accepts all four
#: (overlay_client/paint_commands.py); the original EDMCOverlay knows only
#: "normal" and "large" and renders anything else at its normal size. Neither
#: errors on an unknown value, but _send latches back to "normal" if one ever does.
SIZES = ("small", "normal", "large", "huge")
DEFAULT_SIZE = "small"


class OverlayManager:
    def __init__(self, x: int = DEFAULT_X, y: int = DEFAULT_Y, enabled: bool = True,
                 size: str = DEFAULT_SIZE):
        self.x = x
        self.y = y
        self.enabled = enabled
        self.size = size if size in SIZES else DEFAULT_SIZE
        self.available = False
        self._overlay = None
        self._showing = False
        #: Latches false if the overlay server rejects our size, so we stop
        #: trying it rather than losing the block entirely.
        self._size_ok = True

    # --- connection ---------------------------------------------------------

    def _ensure_connected(self) -> bool:
        if self._overlay is not None:
            return True
        if not self.enabled:
            return False
        try:
            from edmcoverlay import Overlay        # provided by the overlay plugin
        except Exception:
            logger.debug("edmcoverlay not available; using the EDMC panel only")
            self.available = False
            return False
        try:
            overlay = Overlay()
            connect = getattr(overlay, "connect", None)
            if callable(connect):
                connect()                          # some forks connect lazily
        except Exception as exc:
            logger.debug("could not connect to the overlay: %s", exc)
            self._overlay = None
            self.available = False
            return False
        self._overlay = overlay
        self.available = True
        return True

    def _drop(self) -> None:
        """Forget the handle so the next send reconnects - the overlay plugin may
        simply have restarted."""
        self._overlay = None
        self.available = False

    # --- drawing ------------------------------------------------------------

    def set_position(self, x: int, y: int) -> None:
        self.x, self.y = x, y

    def set_enabled(self, enabled: bool) -> None:
        if not enabled and self.enabled:
            self.clear()
        self.enabled = enabled

    def set_size(self, size: str) -> None:
        if size in SIZES and size != self.size:
            self.size = size
            self._size_ok = True          # give the new size its own chance

    def _send(self, text: str, color: str, ttl: int) -> bool:
        """One send, retrying at the default size if ours is rejected."""
        size = self.size if self._size_ok else "normal"
        try:
            self._overlay.send_message(BLOCK_ID, text, color, self.x, self.y,
                                       ttl=ttl, size=size)
            return True
        except Exception as exc:
            if self._size_ok and size != "normal":
                # Some overlay servers may not know this size. Fall back once
                # rather than losing the block entirely.
                logger.debug("overlay rejected size %r (%s); using normal", size, exc)
                self._size_ok = False
                try:
                    self._overlay.send_message(BLOCK_ID, text, color, self.x, self.y,
                                               ttl=ttl, size="normal")
                    return True
                except Exception as retry_exc:
                    exc = retry_exc
            logger.debug("overlay send failed: %s", exc)
            self._drop()
            return False

    def show(self, lines: Sequence[str], color: str = COLOR_BUY,
             ttl: Optional[int] = None) -> bool:
        """Draw the block. Returns whether it actually reached the overlay."""
        if not self.enabled:
            return False
        text = "\n".join(str(line) for line in lines if line is not None)
        if not text:
            return self.clear()
        if not self._ensure_connected():
            return False
        if not self._send(text, color, TTL if ttl is None else ttl):
            return False
        self._showing = True
        return True

    def clear(self) -> bool:
        if self._overlay is None or not self._showing:
            self._showing = False
            return False
        try:
            sent = self._send("", COLOR_BUY, 1)
        finally:
            self._showing = False
        return sent

    def stop(self) -> None:
        self.clear()
        self._drop()


def _self_test() -> None:
    sent: List[tuple] = []

    class FakeOverlay:
        def connect(self):
            pass

        def send_message(self, msgid, text, color, x, y, ttl=4, size="normal"):
            sent.append((msgid, text, color, x, y, ttl, size))

    manager = OverlayManager(x=5, y=6)
    manager._overlay = FakeOverlay()
    manager.available = True

    assert manager.show(["one", "two"], COLOR_SELL)
    assert sent[-1] == (BLOCK_ID, "one\ntwo", COLOR_SELL, 5, 6, TTL, "small")

    assert manager.clear()
    assert sent[-1][1] == "" and sent[-1][5] == 1

    # An overlay server that dislikes our size gets one retry at "normal",
    # rather than the block being dropped.
    picky = []

    class PickyOverlay(FakeOverlay):
        def send_message(self, msgid, text, color, x, y, ttl=4, size="normal"):
            picky.append(size)
            if size != "normal":
                raise ValueError("unknown size")
            sent.append((msgid, text, color, x, y, ttl, size))

    fussy = OverlayManager(x=1, y=2)
    fussy._overlay = PickyOverlay()
    fussy.available = True
    assert fussy.show(["x"]) is True
    assert picky == ["small", "normal"] and fussy._size_ok is False
    picky.clear()
    assert fussy.show(["y"]) is True
    assert picky == ["normal"], picky        # does not keep retrying

    fussy.set_size("large")
    assert fussy._size_ok is True and fussy.size == "large"
    assert OverlayManager(size="enormous").size == DEFAULT_SIZE

    # A send failure drops the handle rather than raising, so the next update
    # reconnects on its own.
    class Broken(FakeOverlay):
        def send_message(self, *a, **k):
            raise OSError("socket gone")

    manager._overlay = Broken()
    manager._showing = True
    assert manager.show(["x"]) is False
    assert manager._overlay is None and manager.available is False

    # With no overlay plugin installed at all, everything is a quiet no-op.
    absent = OverlayManager()
    assert absent.show(["x"]) is False and absent.available is False
    assert absent.clear() is False
    absent.stop()

    disabled = OverlayManager(enabled=False)
    assert disabled.show(["x"]) is False

    print("edta_overlay self-test: OK")


if __name__ == "__main__":
    _self_test()
