"""The GUI's colours, in one place. Every module that draws uses these
names; no hex code appears anywhere else."""

# Base palette (window, text, controls)
BG        = "#0c0c0c"   # window background
TEXT      = "#d0d0d0"   # normal text
BRIGHT    = "#ffffff"   # highlighted values
DIM       = "#505050"   # labels, inactive items
BORDER    = "#2a2a2a"   # chart and entry borders
WARN      = "#ff4040"   # errors, trips, warnings
PROMPT    = "#ff9a1f"   # an input the operator must fill in before anything else (orange)
FIELD     = "#1a1a1a"   # entry boxes and buttons
FIELD_HOT = "#303030"   # button while pressed

# Charts
GRID      = "#1a1a1a"   # horizontal grid lines
REF       = "#8a8a8a"   # dashed target / setpoint / budget lines

# Chart traces
TEMP_LINE = "#ff2a2a"   # valve temperature (red)
VAC_LINE  = "#2f8cff"   # vacuum chamber pressure (blue)
UP_LINE   = "#cfe6cf"   # upstream pressure (whitish green, secondary)
PWR_LINE  = "#ffffff"   # heater power (white)
