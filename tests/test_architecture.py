"""Dependency rules from the README, enforced."""
import ast
import re
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "driver"

# module -> package modules it may import
ALLOWED = {
    "schema": set(),
    "config": set(),
    "controller": {"config"},
    "shared": {"config"},
    "control": {"config", "shared", "controller"},
    "thermocouple": {"config", "shared"},
    "keller": {"config", "shared"},
    "labjack": {"config", "shared", "control", "thermocouple"},
    "logfile": {"config", "shared", "control", "schema"},
    "gui": {"config", "shared", "control", "labjack", "thermocouple", "logfile"},
    "app": {"config", "shared", "control", "keller", "labjack", "logfile", "gui"},
}
HARDWARE_OR_GUI = {"serial", "u3", "keller_protocol", "tkinter"}


def imports_of(module):
    tree = ast.parse((PKG / f"{module}.py").read_text(encoding="utf-8"))
    local, external = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            if node.module:
                local.add(node.module.split(".")[0])
            else:
                local.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            external.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            external.update(a.name.split(".")[0] for a in node.names)
    return local, external


def test_every_module_is_listed():
    assert {p.stem for p in PKG.glob("*.py")} - {"__init__"} == set(ALLOWED)


def test_package_imports_follow_the_layering():
    for module, allowed in ALLOWED.items():
        local, _ = imports_of(module)
        assert local <= allowed, f"{module} imports {sorted(local - allowed)}"


def test_controller_has_no_clock_threads_or_io():
    # Only pure-computation standard modules: time, threads, files and
    # hardware come in through the arguments of controller.step().
    _, external = imports_of("controller")
    assert external <= {"math", "collections", "datetime"}, external


def test_control_and_logs_need_no_hardware_or_gui():
    for module in ("config", "controller", "shared", "control", "schema", "logfile"):
        _, external = imports_of(module)
        assert not external & HARDWARE_OR_GUI, f"{module} imports {external & HARDWARE_OR_GUI}"


def test_no_module_reaches_into_another_modules_private_names():
    # e.g. control._heater or shared._readings: go through the functions
    for module in ALLOWED:
        tree = ast.parse((PKG / f"{module}.py").read_text(encoding="utf-8"))
        imported = {a.asname or a.name for n in ast.walk(tree)
                    if isinstance(n, ast.ImportFrom) and n.level for a in n.names}
        for n in ast.walk(tree):
            if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                    and n.value.id in imported and n.attr.startswith("_")):
                raise AssertionError(f"{module}: {n.value.id}.{n.attr} (line {n.lineno})")
            if isinstance(n, ast.ImportFrom) and n.level:
                private = [a.name for a in n.names if a.name.startswith("_")]
                assert not private, f"{module} imports private {private}"


def test_shared_and_control_expose_no_raw_state():
    import threading
    import types
    from driver import control, shared
    immutable = (int, float, str, tuple)
    for mod, allowed in ((shared, immutable + (threading.Event,)), (control, immutable)):
        for name in dir(mod):
            if name.startswith("_"):
                continue
            obj = getattr(mod, name)
            if isinstance(obj, (types.FunctionType, types.ModuleType, type)) or callable(obj):
                continue
            assert isinstance(obj, allowed), f"{mod.__name__}.{name} is a {type(obj).__name__}"


def test_heater_formulas_live_only_in_config():
    # duty x V^2 / R and friends: config.heater_power_w() and the other
    # helpers are the one definition; nothing else recomputes them.
    formulas = ("HEATER_V_RAIL ** 2", "HEATER_V_RAIL / HEATER_R_OHM",
                "/ HEATER_R_OHM", "* HEATER_V_RAIL")
    for path in PKG.glob("*.py"):
        if path.name == "config.py":
            continue
        source = path.read_text(encoding="utf-8")
        code = "\n".join(re.sub(r"#.*", "", line) for line in source.splitlines())
        for formula in formulas:
            assert formula not in code, f"{path.name}: use the config.heater_… helpers"
