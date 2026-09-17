"""Dependency rules from the README, enforced."""
import ast
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "driver"

# module -> package modules it may import
ALLOWED = {
    "schema": set(),
    "config": set(),
    "controller": {"config"},
    "shared": {"config", "controller"},
    "control": {"config", "shared", "controller"},
    "devices": {"config", "shared", "control"},
    "logfile": {"config", "shared", "control", "schema"},
    "gui": {"config", "shared", "control", "devices", "logfile"},
    "app": {"config", "shared", "control", "devices", "logfile", "gui"},
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
