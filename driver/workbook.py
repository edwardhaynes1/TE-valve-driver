"""The opening map: an Excel workbook with a Runs sheet (a row per run) and
a Batches sheet (a row per batch), appended as a batch goes.

    append(path, sheet, columns, row) -> (ok, message)

Excel locks a workbook it has open (on Windows). Then the row goes to a
side file next to it, <workbook>.pending-<sheet>.csv, and the message says
so; the next append that can save the workbook moves those rows in first
and deletes the side file. Without openpyxl the side file is used too.
Values are written as numbers where they are numbers; the rows are data,
not formulas.
"""

import csv
import os

try:
    import openpyxl
    from openpyxl.styles import Font
    OPENPYXL = True
except ImportError:                     # pragma: no cover — CI installs it
    OPENPYXL = False

SHEETS = ('Runs', 'Batches')
_FONT = "Arial"


def pending_path(path, sheet):
    stem = path[:-5] if path.endswith('.xlsx') else path
    return f"{stem}.pending-{sheet.lower()}.csv"


def _cell(v):
    if isinstance(v, str):
        try:
            f = float(v)
            return int(f) if f.is_integer() and 'e' not in v.lower() and '.' not in v else f
        except ValueError:
            return v
    return v


def _read_pending(path, sheet, columns):
    p = pending_path(path, sheet)
    if not os.path.exists(p):
        return []
    with open(p, newline='', encoding='utf-8') as f:
        return [[r.get(c, '') for c in columns] for r in csv.DictReader(f)]


def _to_pending(path, sheet, columns, row):
    p = pending_path(path, sheet)
    new = not os.path.exists(p)
    with open(p, 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        if new:
            w.writerow(columns)
        w.writerow([row.get(c, '') for c in columns])
    return p


def _open(path, columns_by_sheet):
    if os.path.exists(path):
        wb = openpyxl.load_workbook(path)
    else:
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
    for sheet, columns in columns_by_sheet.items():
        if sheet not in wb.sheetnames:
            ws = wb.create_sheet(sheet)
            ws.append(list(columns))
            for c in ws[1]:
                c.font = Font(name=_FONT, bold=True)
            ws.freeze_panes = "A2"
    return wb


def append(path, sheet, columns, row):
    """Append one row (a dict) to `sheet`. Returns (ok, message): ok False
    means it went to the side file instead (message says where and why)."""
    columns = tuple(columns)
    if not OPENPYXL:
        p = _to_pending(path, sheet, columns, row)
        return False, f"openpyxl not installed — row saved to {p}"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        wb = _open(path, {sheet: columns})
        ws = wb[sheet]
        header = [c.value for c in ws[1]]
        missing = [c for c in columns if c not in header]
        for c in missing:                       # columns added since: append them
            ws.cell(row=1, column=len(header) + 1, value=c).font = Font(name=_FONT, bold=True)
            header.append(c)
        pending = _read_pending(path, sheet, header)
        for values in pending + [[row.get(c, '') for c in header]]:
            ws.append([_cell(v) for v in values])
            for c in ws[ws.max_row]:
                c.font = Font(name=_FONT)
        wb.save(path)
    except (PermissionError, OSError) as e:
        p = _to_pending(path, sheet, columns, row)
        return False, (f"opening map {os.path.basename(path)} can't be saved "
                       f"({type(e).__name__}: is it open in Excel?) — row saved to "
                       f"{os.path.basename(p)}, moved in next time")
    if pending:
        os.remove(pending_path(path, sheet))
        return True, f"opening map: {len(pending)} waiting row(s) moved in from the side file"
    return True, ""
