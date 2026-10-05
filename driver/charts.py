"""Strip charts on a Tk canvas: one canvas per quantity, redrawn on every
GUI poll.

    make_chart(parent, title) -> canvas
    draw_chart(canvas, data, font, …)
"""

import math
import re

import tkinter as tk

from .config import CHART_SECONDS
from .palette import BG, BORDER, BRIGHT, DIM, GRID, REF


def make_chart(parent, title):
    # Title is drawn inside the graph (saves a text row per graph). Small
    # requested height + expand: the graphs share whatever height the
    # window has left, instead of pushing the event log off-screen.
    row = len(parent.grid_slaves())
    c = tk.Canvas(parent, bg=BG, height=30,
                  highlightbackground=BORDER, highlightthickness=1)
    c.grid(row=row, column=0, sticky="nsew", pady=(0, 3))
    parent.rowconfigure(row, weight=1, uniform="charts")   # equal share, shrink evenly
    c.title = f"{title}  ·  last {CHART_SECONDS}s"
    return c


def draw_chart(canvas, data, font, fmt="{:.3f}", log=False,
           ref=None, floor=None, min_span=None,
           color=BRIGHT, width=1, band=None, band_color=None, out_color=None,
           show_last=False):
    """Draw a strip chart on *canvas*.

    data  : sequence of values (oldest → newest)
    fmt   : format string for the y-axis tick labels
    log   : if True, plot log10 of the data (for vacuum pressure, which
            spans orders of magnitude). Non-positive values are skipped.
    ref   : optional reference value (target / setpoint), drawn dashed and
            always kept inside the y-range
    floor : optional (lo, hi) the y-range will always include
    min_span : smallest y-range shown (plot units; decades if log), so
            sensor quantisation isn't magnified into apparent swings.
            Tick labels gain decimals automatically if they'd repeat.
    color : line colour of the data curve
    width : line width of the data curve, px
    band  : optional (lo, hi) tolerance band, edges drawn dashed and kept in range
    band_color : colour of the band edges
    out_color  : colour of the curve where it lies outside the band
    show_last  : print the latest value (in fmt) at the right end of the
            curve, so it reads against the axis labels at a glance
    """
    c = canvas
    c.delete("all")
    w = c.winfo_width() or 580
    h = c.winfo_height() or 90
    pad_l, pad_r, pad_y = 74, 8, 8
    n_div = 4 if h >= 110 else (2 if h >= 55 else 1)   # label rows that fit

    for i in range(1, n_div):
        y = pad_y + (h - 2 * pad_y) * i / n_div
        c.create_line(pad_l, y, w - pad_r, y, fill=GRID)
    title = getattr(c, "title", "")
    if title:
        c.create_text(pad_l + 6, 2, text=title, fill=DIM, font=font,
                      anchor="nw", tags="title")

    if log:
        plot_vals = [math.log10(v) for v in data if v is not None and v > 0]
    else:
        plot_vals = [v for v in data if v is not None]
    if len(plot_vals) < 2:
        c.create_text(w / 2, h / 2, text="waiting for data",
                      fill=DIM, font=font)
        return

    ref_p = None
    if ref is not None and (not log or ref > 0):
        ref_p = math.log10(ref) if log else ref

    # Decimate to ~one min/max pair per pixel column. A 300 s window is
    # 1200 points per chart; min/max (not every-Nth) keeps short spikes.
    cols = max(2, int((w - pad_l - pad_r) / 2))
    if len(plot_vals) > 2 * cols:
        step, dec = math.ceil(len(plot_vals) / cols), []
        for k in range(0, len(plot_vals), step):
            b = plot_vals[k:k + step]
            i_lo = min(range(len(b)), key=b.__getitem__)
            i_hi = max(range(len(b)), key=b.__getitem__)
            dec.extend(b[i] for i in sorted({i_lo, i_hi}))
        plot_vals = dec

    lo, hi = min(plot_vals), max(plot_vals)
    if ref_p is not None:
        lo, hi = min(lo, ref_p), max(hi, ref_p)
    if floor is not None:
        lo, hi = min(lo, floor[0]), max(hi, floor[1])
    if band is not None:                       # the band, with a little room above and below
        room = 0.5 * (band[1] - band[0])
        lo, hi = min(lo, band[0] - room), max(hi, band[1] + room)
    need = max(min_span or 0.0, 1e-9)
    if hi - lo < need:
        mid = (hi + lo) / 2
        lo, hi = mid - need / 2, mid + need / 2
    span = hi - lo
    n = len(plot_vals)

    def ypix(v):
        return (h - pad_y) - (h - 2 * pad_y) * (v - lo) / span

    ticks = [lo + span * i / n_div for i in range(n_div + 1)]
    reals = [(10 ** v) if log else v for v in ticks]
    m = re.search(r"\.(\d+)([fe])", fmt)
    labels = [fmt.format(v) for v in reals]
    if m:
        for extra in range(1, 4):              # add digits until labels differ
            if len(set(labels)) == len(labels):
                break
            f2 = "{:." + str(int(m.group(1)) + extra) + m.group(2) + "}"
            labels = [f2.format(v) for v in reals]
    for v, text in zip(ticks, labels):
        c.create_text(pad_l - 4, ypix(v), text=text,
                      fill=DIM, font=font, anchor="e")

    if ref_p is not None:
        y = ypix(ref_p)
        c.create_line(pad_l, y, w - pad_r, y, fill=REF, dash=(4, 3))

    if band is not None:
        for edge in band:
            y = ypix(edge)
            c.create_line(pad_l, y, w - pad_r, y, fill=band_color or REF, dash=(2, 3))

    pts = []
    for i, v in enumerate(plot_vals):
        pts.extend((pad_l + (w - pad_l - pad_r) * i / (n - 1), ypix(v)))
    c.create_line(*pts, fill=color, width=width)
    if band is not None and out_color:                 # recolour the stretches outside the band
        outside = [not (band[0] <= v <= band[1]) for v in plot_vals]
        i = 0
        while i < n:
            if outside[i]:
                j = i
                while j + 1 < n and outside[j + 1]:
                    j += 1
                a, b = max(i - 1, 0), min(j + 1, n - 1)
                seg = pts[2 * a:2 * b + 2]
                if len(seg) >= 4:
                    c.create_line(*seg, fill=out_color, width=width + 1)
                i = j + 1
            else:
                i += 1
    if show_last:
        last = plot_vals[-1]
        y = ypix(last)
        above = y - pad_y > 18                      # room above the curve, else below
        c.create_text(w - pad_r - 4, y - 3 if above else y + 3,
                      text=fmt.format(10 ** last if log else last), fill=color, font=font,
                      anchor="se" if above else "ne")
    c.tag_raise("title")
