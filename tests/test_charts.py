"""Strip charts: up the screen is up the axis, and the upstream chart says
its latest value (history 48).

Agreed behaviour (5 Oct 2026):
  * a higher value is drawn higher, against the axis labels; a pressure
    above the target is drawn above the dashed target line
  * the upstream chart labels its axis with 4 decimals, like the readout,
    and prints the latest value at the right end of the trace
"""
from driver import charts
from driver.palette import REF


class FakeCanvas:
    """Records what draw_chart draws, without a window."""

    def __init__(self, w=1300, h=180):
        self.w, self.h, self.items = w, h, []

    def delete(self, *_):
        self.items = []

    def winfo_width(self):
        return self.w

    def winfo_height(self):
        return self.h

    def create_line(self, *xy, **kw):
        self.items.append(("line", xy, kw))

    def create_text(self, x, y, **kw):
        self.items.append(("text", (x, y), kw))

    def tag_raise(self, *_):
        pass


def draw(values, target=0.9526, band=0.05, **kw):
    c = FakeCanvas()
    charts.draw_chart(c, values, None, fmt="{:.4f}", min_span=6 * band, ref=target,
                      band=(target - band, target + band), band_color="band",
                      out_color="out", color="data", width=2, **kw)
    return c


def data_ys(c):
    xy = next(xy for kind, xy, kw in c.items if kind == "line" and kw.get("fill") == "data")
    return xy[1::2]


def target_y(c):
    return next(xy[1] for kind, xy, kw in c.items
                if kind == "line" and kw.get("fill") == REF)


def test_a_rising_pressure_is_drawn_rising():
    ys = data_ys(draw([0.93 + 0.0005 * i for i in range(60)]))
    assert ys[-1] < ys[0]                      # screen y grows downwards


def test_above_the_target_is_drawn_above_the_target_line():
    # 5 Oct 2026, 10:31: 0.9601 bar against a target of 0.9526 bar
    c = draw([0.9601] * 50)
    assert data_ys(c)[-1] < target_y(c)
    c = draw([0.9451] * 50)
    assert data_ys(c)[-1] > target_y(c)


def test_the_axis_and_the_latest_value_have_4_decimals():
    c = draw([0.9601] * 50, show_last=True)
    texts = [kw["text"] for kind, _, kw in c.items if kind == "text" and kw.get("text")]
    assert "0.9526" in texts                   # the target's tick, not 0.953
    assert "0.9601" in texts                   # the latest value, on the chart
