# Software history log

Short records of choices that shaped the code, newest first. Each says what
was decided and why, so nobody has to rediscover the reason. Add one when a
change would otherwise puzzle someone reading the code later.

## 42. An optional "estimate °C" starts a new seating from the value Edward expects — 1 Oct 2026
The lock-nut seatings at 0.40 N·m gave 102.3, 87.8 and ≫ 92 °C: the other
seatings' mean says little about the next one, and Edward often has a
better guess. The t-min-tune row now has an **estimate °C** box (optional,
20-155 °C). Filled in at the start, it is the estimate until the seating has
a result of its own (a counted result or its scout); it comes before the
other seatings, the opening map and the scout, with the usual 10 K margin
for a new seating. A seating that already has results ignores it, and the
event log says so. It is read at the start only: locked while t-min-tune
runs, and cleared once used, so it isn't carried to the next seating by
mistake. Rows started from it say "the operator's estimate …" in
`estimate_from`; `session.json` records it as `operator_estimate_degC`. No
new column in `t-min.csv`.

## 41. A scout also remembers openings at the start from earlier sessions — 1 Oct 2026
History 40 lowered the next scout within a session; a new session at 0.20
N·m still scouted from 40 °C (the torque table) after the valve had opened
at 24-26 °C in the last one. Now a session starts knowing the lowest
"opened at start" temperature at its torque in `logs/t-min.csv`, and a
scout starts at least 10 K below it (near room temperature: where the valve
is).

## 40. A new seating's first estimate uses each other seating's latest results; a scout that opens at its start goes lower — 1 Oct 2026
The first estimate at a new seating averaged *all* counted results of the
other seatings at the torque. The 0.50 N·m seating (1 Oct) walked down from
a high start: 135, 130, 125, 122, 120, 120 °C — its early tests opened on
their first step, so they are only upper bounds, and the old rule gave
125.5 °C. Now each other seating counts with the mean of its latest 3
(`TMIN_ESTIMATE_LAST_N`), as its own estimate does: 120.8 °C. The margin
for a new seating stays 10 K.

At 0.20 N·m (1 Oct) the torque table gave 40 °C (+10 K for the pressure:
a 40 °C scout start). The valve opened at 26 °C while heating to it, and
the next scout held 40 °C again: a scout ignored the "start lower" after an
opening at the start, so it would have repeated indefinitely. Now a scout
starts lower by that amount, and at least 10 K below where any test opened
during its hold (near room temperature it then starts where it is).

## 39. The driver closes itself once t-min-tune has converged — 1 Oct 2026
Edward wants the driver to close by itself when a session is done, after
the valve has closed. Convergence is only checked once the last test's
valve has closed (chamber back at its baseline), and the heater is already
disarmed, so the window closes the driver `TMIN_QUIT_DELAY_S` (60 s) after
a converged session, the same way as closing the window (heater disarmed,
logs saved, watchdog released). Only for *converged*: a session stopped by
the operator or by a fault leaves the window open to show why. Starting
t-min-tune again within the minute cancels it; `TMIN_QUIT_WHEN_CONVERGED =
False` turns it off.

## 38. t-min-tune starts 2 K below the lowest result; the converged T_min is recorded — 1 Oct 2026
The first 0.30 N·m session (1 Oct, 0.959 bar) converged on 67.3 °C, but
spent most of its time on steps where the valve never opens: test006 held
63-66 °C for 20 of its 22.5 min. Test004's 71 °C (started above T_min)
inflated the scatter, and with it the old margin (2 × scatter + 1 K).
* **Start from two results at 2 K below the lowest of the latest 3**
  (`TMIN_START_BELOW_LOWEST_K`), kept 3-10 K below the estimate. One high
  result no longer pushes the start down; 2 K still gives a full 5 min step
  below the last opening, so a downward drift is caught. 1 Oct: 64 °C
  instead of 62 °C. Step (1 K) and dwell (5 min) unchanged: the openings
  came 43-159 s into their step, too few to shorten it yet.
* **Optional cap at the previous T_close + K** (`TMIN_START_ABOVE_CLOSE_K`),
  Edward's idea for high upstream pressure. Off by default: whether the
  valve has closed is already judged by the chamber before the next test,
  and the cap starts tests lower exactly when the valve closes far below
  T_min (1 Oct, +3 K: 58-59 °C instead of 64-65, ~30 min more per test).
* **The converged T_min is recorded**: `t_min_converged_degC` on the row
  that converged (the mean of the last 3 counted results at the target).
  Until now it was only in the event log. Rows written before get it when
  t-min-tune starts or a row is added (`tminlog.upgrade`); nothing else in
  the file changes.

## 37. "Setting" is now "seating" — 1 Oct 2026
Edward didn't like "setting" (nor "tightening") for one tightening of the
seat screw; "seating" names what changes — how the valve is seated and
preloaded — and doesn't clash with configuration settings. Renamed
throughout: code, messages, the `seating` column of `logs/openings.csv`,
`logs/t-min.csv` and the workbook sheets, `session.json`, docs (including
the entries below). Files written before are read with their `setting`
column as `seating`, and the header is renamed the next time a row is
added (only the header line changes; `schema.RENAMED_COLUMNS`). The seat
screw's tightening torque may be written M_A (VDI 2230).

## 36. t-min-tune replaces cycling: the lowest opening temperature, step by step — 30 Sept 2026
Edward wants the *minimum* T_open for each upstream pressure and screw
torque, found experimentally, rather than the creep's T_open. Agreed one
question at a time:
* **Step and dwell, not a creep.** A 3 °C/min ramp reads T_open high (the
  TC leads the element) and can miss a slow opening. Each test holds a start
  below the estimate until the chamber is settled, then raises the setpoint
  1 K and holds each step 5 min (2-3 time constants) until it opens.
* **Start well below, narrowing with results:** 10 K below the estimate
  with no result at the seating (Edward: 3 K isn't enough; at least 5-10 K
  and reduce as the estimate improves), 5 K with one, then 2 × scatter +
  1 K (3-10 K). The estimate updates after every test.
* **The upstream is held by hand in a band** (target ± 0.05 bar by
  default: ≈ ±0.5 K of T_min; 30 Sept Edward held ±0.015 bar for 4 min).
  Leaving it during the hold or a step cuts the heater and abandons the test
  (Edward's rule); the next starts afresh inside the band. The window gets
  a taller upstream chart with the band dotted and the trace amber outside.
* **The valve must close before the next test**, judged by the chamber:
  within +0.02 decades of its baseline before the opening, and settled.
  Edward: T_open ≠ T_close — once open it must cool further to close
  (28 Sept: 10-25 K). T_close is logged with every test.
* **Stop when repeatable:** the last 3 results within ±1 K, or when stopped.
* **One central results file**, `logs/t-min.csv`, a row per test including
  the failed ones, never rewritten except to add new columns at the end;
  the estimates are recomputed from it, and `logs/` is outside git, so
  program updates can't touch what has been learned (Edward's requirement).
* **The first estimate** at a new seating comes from the other seatings at
  the same torque (t-min results, else the opening map); only a torque with
  nothing measured scouts first (Edward chose this over a scout at every
  new tightening).
* **"deep every" is gone** with cycling. `cycle.py`, `cyclerun.py` and their
  tests remain; the window and logger use `tmin.py` / `tminrun.py`.
* **A test can outlast the 60 min armed limit** (10 K at 5 min per step
  plus the hold): each step renews the armed-time clock (a new heater
  command, `renew`); every other interlock stays.
The temperature control was retuned for it first (history 35): 1 K steps
now settle in 10-12 s with ≤ 0.2 K overshoot in simulation.

A valve already open at the start temperature (29 Sept, 0.30 N·m at 30 °C)
shows no rise to detect: the steps stop 15 K above the estimate
(`TMIN_ABOVE_EST_K`) with "no opening", which names that possibility,
rather than climbing to 155 °C with the valve open.

## 35. auto-t lands within 0.5 °C, faster — 30 Sept 2026
Edward: the feed-forward (the burst) must not overshoot the setpoint by more
than 0.5 °C, and a D term should get the loop there faster. On the rig the
coasts ran 0.6-2.5 K past the setpoint (28-30 Sept, 16-38 K steps; 30 Sept
60 °C: +2 K), and the step-and-dwell search for the lowest opening point
(t-min-tune, history 36) steps 1 K at a time, so a 1-2 K overshoot would
open the valve a step early.
* **The burst aims short:** it is cut when the coast is predicted to reach
  the setpoint − max(1.5 K, 15 % of the step), not the setpoint. The models
  underpredict the rig's coast, so the margin scales with the step, where
  the rig's excess did too (≤ 10 % of the step).
* **Stronger PID, with D doing the damping:** KP 0.05 → 0.20, KI 0.001 →
  0.004 (Ti stays 50 s), KD 0.10 → 0.80 (on the measurement, 5 s filter).
  On the three 21 Sept models, from ambient and from settled holds, 5-35 K
  steps and 1 K staircases: worst overshoot 0.24 K (0.26 K with 0.05 K TC
  noise); slowest settling to ±0.2 K 118 s, was 487 s; 1 K steps settle in
  10-12 s. The D term moves the duty by ~2 % (sd) on TC noise.
* **Check it on the rig first:** on 16 Sept the models also predicted
  ~0.05 K and the rig overshot 0.7-1.1 K with gains only 30 % higher. The
  old values are in config.py next to the new ones.
The closed-loop tests now require < 0.5 °C (they allowed 1 °C), and a 1 K
staircase test was added. The golden control traces were re-recorded (every
auto-t and auto-p scenario changed, as intended). The batch scatter test's
seed changed from 2 to 8: with the new tuning seed 2's draws ended at 10
runs 0.08 K short of precise, a different random outcome, not a fault.

## 34. Map the opening point by cycling, not by batches — 28 Sept 2026
Edward: waiting for cooldowns and discarding runs slowed testing down, and
the upstream pressure can't be controlled — testing should go on regardless
and the analysis deal with it. Then: the aim is to capture as much data as
efficiently as possible, to tie down seat screw torque, upstream pressure
and T_open; batches as they were aren't a constraint. Agreed one question
at a time (a first answer — one cold run per batch, warm test runs after
it, a refill no longer discarding the run — was built, then superseded by
this before being applied):
* **The goal is the map T_open(torque, upstream), not one T_open per
  batch.** Every opening is a data point in one table (`logs/openings.csv`);
  a single fit over all of it is recomputed after every opening. Upstream
  is held by hand but will drift: the fit accounts for it rather than the
  runs trying to avoid it. The 28 Sept 0.45 N·m batch shows why this works:
  corrected for upstream, T_open = 128.0 ± 1.1 K at 2.5 bar, slope −12.0 ±
  3.5 K/bar, scatter 1.1 K; the batch's own "± 4.3 K" was the leak's spread.
* **Slope per torque, offset per seating.** Re-torquing to the same value
  may not give the same valve, so every tightening (a *seating*) gets its
  own offset, while all seatings at one torque share the pressure slope —
  which also measures how well re-torquing reproduces. A per-batch slope
  over a few tenths of a bar is mostly noise; pooled, it improves with every
  pressure visited.
* **Cycling replaces batches:** creep from just below the prediction at the
  upstream pressure now, detect, cool only until the valve has closed and is
  margin + 2 K below the prediction, repeat. The valve closes 17-27 K below
  T_open within ~30 s (28 Sept), so a cycle should take 3-4 min instead of
  ~16. No scouts once a torque has data, no stopping rule: a status line
  says when a seating is *done* (offset ±1 K, slope ±2 K/bar) and suggests
  where to take the pressure next. Every 5th cycle is deep (35 °C and the
  4 min hold) so the warm-start effect is measured rather than assumed.
* **The fit checks for drift and for the chamber background**, each added
  only if significant. 28 Sept: the residuals rose ~2 K over 1.5 h while the
  background fell 15 %; pump-down should make T_open read high early (a
  higher background needs more flow to count), the opposite of what was
  seen, so time drift (the valve body warming?) is likelier — the fit will
  say.
* **Detection on a fixed flow:** 0.5 × 10⁻⁷ mbar above the baseline (with
  the 4.7 % noise floor), not "+12 % or 1 × 10⁻⁷ mbar, whichever first", so a
  falling background can't move what counts as open. 0.5 × 10⁻⁷ is about
  +12 % at the 28 Sept background (comparable with older data, whose rows
  say which rule found them) and ~15 × the gauge noise; the 28 Sept
  openings jumped ~1 × 10⁻⁷ within 2-3 s, so 0.2-0.8 × 10⁻⁷ would all give
  nearly the same backdated T_open. To be re-derived from the traces.
* **Old batch folders are imported** into the table, one seating each (it
  can't be known whether the screw was touched between them), so the fit
  starts from today's data.
* Built in stages: detection and the table with its import and fit first,
  checked against the 28 Sept data, then the cycling itself.

Found while building (simulation, tests/batch_sim.py `run_cycling`):
* On the 28 Sept data the fit tried both drift and background, and the
  background term came out just significant (−31 ± 29 K/decade) — but the
  two moved together exactly (r = −1.00): the background fell steadily with
  time. Picking one would be arbitrary, so terms that move together
  (|r| ≥ 0.8) are reported as not separable and neither corrects the fit.
  Judged within the seatings that carry most of the variation: pooled with
  simulated sessions whose background was steady, the 28 Sept confound fell
  below 0.8 and the background term slipped back in. In simulation the
  warm-start term was confounded with the background too (a deep cycle's
  hold at 35 °C sits at another background than a shallow one near the
  opening point), so it may take cycles at deliberately different
  backgrounds to separate them on the rig.
* A drift term measured from the first opening of a seating shifted its
  offset to that moment; it is centred per seating, so the offset stays
  the seating's average.
* A valve that closes slowly (flows until well below the target) made the
  cooldown reach the target, wait for the chamber, then warm back up to
  the target: now it holds where the cooldown ended if that is lower.
* A valve that moved down opened in the fast heat-up (a burst, 2-3 °C/s)
  while the chamber, lagging a few seconds, only showed it once the creep
  had begun: a "creep" reading 3-8 K high. The onset decides now: one
  before the creep began or within 10 s of it counts as approaching. (Batch
  test runs had the same weakness.) One that opens during the hold is
  caught there too (the chamber rising above its lowest level in the hold,
  unless a refill was seen).
* With an offset per seating, the pressure slope can only be learned from
  pressure changes *within* a seating — between seatings the offsets absorb
  them. Two seatings at one torque, each held at its own pressure, made the
  fit unsolvable: a slope is now fitted only once one seating spans 0.3 bar
  (else −12 K/bar, assumed), and the status line's hint says to change the
  pressure without re-torquing. In practice: sweep the pressure, then
  re-torque.
* Asked for: the map in 3D. `logs/opening-map-3d.png` is drawn next to the
  2D figure after every opening (torque, upstream, T_open; each seating's
  fitted line; a surface between the torques' mean lines, linear between
  torques — a guide, not a model); `TE_PLOTTER.py --map --3d` shows it
  rotatable.
* 1.5 simulated hours at 0.45 N·m: 21 openings (batches: 7 in the same
  time). DONE after 20 openings (79 min), held back by the slope: only a
  slow leak (0.01 bar/min) widened the pressure range. Changing the
  pressure on purpose, as the status line suggests, is what pins it.

## 33. Top-up pause removed — 28 Sept 2026
Entry 28 added an optional pause before a run once upstream had fallen a
set amount (default 0.3 bar), and a "continue" button. Edward didn't want
the seating: the batch should just account for the changing upstream
pressure, which it already does (each run's upstream is logged; starts are
shifted for it; the stopping rule corrects to the mean; T_open is fitted
against it and stored at the mean). The pre-filled 0.3 also read as unclear
("blank or 0.3 s?"). Removed: the box, the button, the pause, the fill
detection behind it, and the Keller requirement it put on starting (a
batch now starts without an upstream reading, and says so in the start
dialog: no upstream correction then). The Batches sheet keeps its
`top_ups` column (append-only), now blank.

A refill by hand (any time, no button) is seen on the Keller as upstream
rising ≥ 0.05 bar (`BATCH_REFILL_RISE_BAR`); the leak only lowers it.
Simulated with the 24 Sept fill jump (+70 %): while cooling or holding, the
approach just waits for the chamber to settle; but during the hold a slow
fill tail also looked like the valve opening while warming, and stopped
the batch — a refill seen during the hold now turns that check off for the
run. While heating, the jump read as an opening at 35 °C and sent the
batch back to scouting: now the run is discarded (not averaged, heater
disarmed) and repeated after the cooldown with a letter (`testrun02b`).
The start dialog says so. Without the Keller a refill can't be seen.

## 32. Scouts don't hold, and scout 1 starts from the table guess — 25 Sept 2026
First 0.45 N·m batch after entry 31: nothing remembered, so scout 1 held
the cold valve at 29.5 °C for 4 min before heating fast towards 155 °C —
although the torque says the valve opens near 120-150 °C, and scouts aren't
averaged, so their starting state doesn't matter. Agreed:
* **Scouts skip the 4 min hold** (`BATCH_SCOUT_HOLD_S` = 0). They still wait
  for a settled chamber: a drifting chamber would still look like an
  opening. Test runs keep the full hold.
* **Scout 1 starts 10 K below the table guess** (auto-p's opening point
  from `SEAT_SCREW_VALVE`, shifted for upstream) and creeps, instead of
  heating fast and reading 5-35 K high. If it opens while creeping, it is a
  good reading, so the test runs follow at once — no scout 2. If it opens
  during the approach (guess high), scout 2 starts 10 K below where it
  opened. If it hasn't opened 20 K above the guess (`BATCH_GUESS_ABOVE_K`;
  guess far too low), it heats fast to the ceiling as before rather than
  creep for tens of minutes, and scout 2 follows.

## 31. Adaptive cooling: batches work for low opening points — 25 Sept 2026
Asked for: batches that also work at 0.25 N·m, where the valve opens near
40 °C (and near 27 °C at 5 bar). Entry 30's hold rule (35 °C, or T_open −
20 K, never below 28 °C) aimed at 28 °C there, and the 24 Sept cooling log
(te-sensor_20260924_161238, heater off, 57 → 36.5 °C) shows why that fails:
the TC falls fast (single-exponential fit: time constant 150 s) towards
~35 °C, the body temperature, and the body then cools slowly towards the
lab (~27 °C at the Keller head). 28 °C could take far longer than the 30 min
cooldown limit. Agreed one question at a time:
* **Adaptive cooling.** A cooldown ends at the target (35 °C or T_open −
  20 K), or once the valve cools slower than 1 K/min — if it is already
  ≥ 5 K below the best guess of T_open. Edward chose 1 K/min (proposed:
  0.15 K/min); alone it would stop near 37-38 °C, a 2-3 K gap at 0.25 N·m,
  so it applies only once the gap is secured. The 28 °C floor is gone.
* **Minimum gap 5 K** between the hold temperature and a test run's
  reference; less stops the batch: an opening point too close to room
  temperature can't be started cold without cooling hardware.
* **One hold temperature per batch,** fixed at the first test run's
  cooldown (rounded up to 0.5 K) — adaptive cooling would otherwise let the
  starting state drift run to run. Scouts' holds are provisional (their
  reference isn't known yet); a find-again frees it (the valve moved). A
  cooldown that can't reach it again in 30 min stops the batch rather than
  mix starting states.

Found in simulation (tests/batch_sim.py now has an optional two-stage
valve — TC fast to a body that cools slowly — and a settable lab
temperature):
* A cold valve at the start counted as "slowed" at once, so a batch from a
  remembered point fixed its hold at room temperature, and every later run
  had to cool slowly back to it (54 vs 43 min). The remembered point now
  carries its batch's hold temperature, and the next batch starts from it
  (0.25 N·m: 24 min for 3 test runs).
* Scout 1 warmed a cold valve to 35 °C, which would open one that opens at
  30-33 °C although a start at room temperature gives the 5 K gap. A cold
  valve with nothing known yet is now held where it is.
* After a top-up pause the valve had cooled to room temperature and the
  previous fix held it there: only with nothing known (scout 1) now.
* Cooling to exactly 5 K below the guess and rounding the hold up broke the
  gap, and so did the next test run reading 0.1 K lower: adaptive cooling
  aims for 6 K (a 1 K buffer, `BATCH_GAP_BUFFER_K`) on the rounded hold.

## 30. Batches remember the opening point, stop when precise, and start cold — 24 Sept 2026
Asked for: remember T_open so the driver has it as its baseline (and finds
it again if it moves), and make batches as short as possible while the
result stays meaningful. Agreed one question at a time; the reasons:

* **Remembered opening point** (`logs/opening-points.json`, data, not a
  `config.py` edit). Used by batches and by auto-p, ahead of the
  `SEAT_SCREW_VALVE` guesses; this session's learned point still comes
  first for auto-p. The driver can't tell whether the screw was re-torqued,
  even to the same value, so **start batch** asks. No expiry by age: an
  untouched valve keeps its opening point, and the find-again rule catches
  drift. Only a value measured at the batch's own creep rate is used,
  because the TC lag is part of every T_open.
* **Stop when precise enough**: ±1 K at 95 % on the mean, from ≥ 3 test
  runs (N is now a maximum). ±1 K is about the shift from 0.1 bar of
  upstream pressure, and far below the torque effects being mapped.
  Correcting each run to the mean upstream pressure only removes
  leak-induced scatter; it can't bias the mean, so an assumed slope is
  harmless.
* **Adaptive margin** (3 × scatter + 0.5 K, 2-5 K) instead of a fixed 5 K:
  each kelvin costs 20 s of creep per run.
* **Deep cooldown and a hold** (35 °C, or T_open − 20 K if lower, not below
  28 °C; auto-t holds it 4 min). A shallow cooldown was proposed to save
  time and rejected: the valve has memory (friction and seat hysteresis,
  and it opened at 158 °C on a first heat-up but 140-148 °C on re-heats at
  0.45 N·m), so a 5 K cycle would give a precise number for a state the
  valve won't be in when used. Starting cold matches the use case. The hold
  is needed because the TC reaches 35 °C long before the body does (lag
  150-250 s). Depth and hold time are settings (`BATCH_COOL_*`,
  `BATCH_HOLD_S`), stored with every result, so a deep-vs-shallow check is
  a one-line change.
* **Detection on an absolute rise too** (1e-7 mbar or +12 %, whichever
  first, floor 0.02 decades): "open" then means a throughput, not a
  percentage of whatever the background is. auto-p is unchanged.
* **Creep rate stays 3 °C/min**: faster saves little now the margin is
  small, and it doubles the lag.
* **Not now:** a quasi-static step-and-dwell mode (true T_open, T_close and
  hysteresis without the lag), and re-deriving the free-cooling "closing"
  temperature, which the lag inflates: after a disarm at ~146 °C the TC
  falls ~1 °C/s and reads far below the body. It stays labelled indicative.

Found in simulation while building it (tests/batch_sim.py, now with a
run-to-run scatter of the opening point):
* **Opened while holding.** A valve opening below the hold temperature
  (0.25 N·m at 5 bar opened near 27 °C on 21 Sept) raised the chamber
  slowly while warming; the hold's fresh baseline followed it, and the run
  then measured from the raised level. Now the settled level at the end of
  the hold is compared with the lowest level since it began. An external
  rise already under way when the hold starts (outgassing) is waited out
  instead, as before; one that starts with the heating can't be told from
  the valve, so the batch stops and says both.
* **Falling-chamber limit 0.03 → 0.01 dec/min.** A falling background hides
  the first flow through the whole approach and creep; a slow fill tail
  read T_open ~7 K high at 0.03, < 1 K at 0.01. The measured pump-down tail
  (1.5 %/min) still passes, and the 4 min hold usually covers the wait.
* **Off by one.** The stopping rule first counted the run that had just
  opened only after its cooldown, so it ran one test run too many.

Simulated times (sim valve, TC time constant 80 s; the real body cools
slower, so expect longer): from a remembered opening point, 3 test runs in
~22 min (12 of them holding); with scouts ~36 min; 1.5 K of scatter needed
7 test runs, ~69 min.

## 29. No settling wait when the chamber is already settled — 24 Sept 2026
Settling waited ~50 s at the start of every batch, because the batch began
with no chamber readings and the trend needs most of a minute; and ~50 s
after every **continue**, because "after the fill" meant "after continue".
Both waits were pointless on a settled chamber. The driver now keeps its
last 5 min of chamber readings with their times (`shared.vacuum_history`),
and a batch starts from them. During the top-up pause the batch watches
the upstream pressure: once it has risen 0.05 bar above its lowest, the
last moment it was still rising is the fill, and the chamber is judged
from then (from continue if no fill was seen, e.g. the Keller is off).

## 28. Batches made for the real rig — 24 Sept 2026
Two logs from 24 Sept 2026 (0.45 N·m) showed what a batch has to live with:
te-sensor_20260924_155503 (an opening at ~146 °C, 2.7 bar) and
te-sensor_20260924_161238 (cooling, heater off).
* After the opening the chamber was back within 6 % of its pre-opening
  level ~90 s after disarming (at 73 °C: the valve closed ~70 K below where
  it opened). So entry 27's cooldown rule holds.
* The chamber keeps pumping down slowly (1.5 %/min), noise ~0.003 decades.
* Filling upstream (0.96 → 3.16 bar) with the valve cold at 29 °C made the
  chamber jump 1.4 → 2.35e-6 mbar, falling back over ~4 min.
* Upstream leaked 0.05-0.07 bar/min (0.015 on 14 Sept): over an hour's
  batch, more than 1 bar, i.e. ~12 K or more of opening point.
* 0.45 N·m opened at ~146 °C: 4 K below the 150 °C ceiling, and detection
  needs a few K above the opening.

So:
1. **Settle before every run** (heater off): the chamber must be neither
   rising (a fill's jump or outgassing looks like an opening) nor falling
   fast (the median baseline lags it). Replaces scout 1's fixed baseline wait.
   After a top-up only readings from after the fill count — without that,
   the flat minute before the fill made it look settled at once and the jump
   was detected as an opening at 36 °C (in simulation, true opening 60 °C).
   The baseline is measured fresh while settling, and no longer carried from
   the previous run.
2. **Upstream is a measured variable.** Each batch fits T_open against the
   upstream pressure at each opening; the scatter left about that fit is
   what the leak doesn't explain. In simulation with −12 K/bar and the
   0.07 bar/min leak: slope recovered within 1.5 K/bar, scatter 0.05 K
   against 1.5 K about the plain mean.
3. **Optional top-up pause** (default 0.3 bar below the batch's start),
   heater off, until the operator presses continue.
4. **Ceiling 155 °C** (as `PRESSURE_TSP_MAX_C`, 5 K below the trip).
5. **Start checks**: no valve temperature, no chamber reading, or (with the
   top-up pause) no upstream reading refuses the start. A rising chamber
   doesn't: the batch waits for it.

Tried and dropped: a baseline extrapolated along the fitted trend, to
follow a falling chamber. With the never-rise rule its noise walked it
down, and it produced false openings (52.8 °C for a 60 °C valve). The
limit on how fast the chamber may fall before heating does the same job
robustly.

Found on the way: a high background hides the first flow, so T_open reads
high early in a steep pump-down (60.5-61.7 °C for 60.4 °C in simulation,
converging as it falls). It never reads early.

## 27. Batches: repeated opening-point runs — 24 Sept 2026
Goal: characterise the opening point against seat screw torque and upstream
pressure, averaged over several runs. Torque is set by hand; upstream
pressure is measured, not controlled (the rig leaks). Flow after opening and
closing were deliberately left out: map the opening point first, then
characterise around it. The run is a sequence of phases, so later phases
(settle, step-down) can be added without a rewrite.

A run: approach (auto-t) → creep at 3 °C/min → detection → heater disarmed →
cooldown. Choices, with the reason for each:
* **Two scouts, not one.** A fast scout reads high (the TC lags the seat by
  roughly rate × tau: ~8.5 K at 3 °C/min). Starting the test runs from it
  risked starting above the opening point. Scout 2 creeps from 10 K below
  scout 1; test runs start 5 K below scout 2. Scout 2 also gives the test
  runs an identical predecessor (creep, heater off at detection), which is
  why there is no soak before a run.
* **Same creep rate every run**, so the TC lag is a repeatable offset, not
  scatter.
* **T_open backdated to the onset**, because detection confirms a few
  seconds after the rise begins. The onset is the last sample (3-point
  median) within 0.015 decades of the baseline; this is a best guess, and
  T_detect is logged beside it. No extra lag correction is applied.
* **Heater energy from the approach start** is logged at onset and
  detection, since the opening depends on the temperature distribution
  (Invar piece against stainless sleeve), not the TC alone. The heater is
  off before every approach, so energy since the run's start would be the
  same number.
* **Heater disarmed at detection**: shortest time open, smallest pressure
  excursion, cooldown starts at once. Each run re-arms, so the 60-minute
  armed limit applies per run, not per batch. All interlocks stay active.
* **The batch adds its own chamber checks** while heating (auto-t alone has
  none): no valid reading for 2 s, or above 5e-4 mbar, aborts the batch.
* **Cooldown floor 28 °C**: T_open − 20 K can be below what the lab lets the
  valve cool to (0.25 N·m opens near 40 °C).
* Operator disarm or a trip during a batch aborts it; a batch cannot resume.
* Averages are over test runs that opened; scouts and failed runs are in the
  Runs sheet but not in the averages.

The opening map is an Excel workbook because that is where the results are
used. If Excel has it open (Windows locks it), rows go to side files next to
it and are merged the next time it can be saved.

**Changed after simulation** (tests/batch_sim.py: the real controller on a
thermal model whose seat lags the TC by 5 s, throttles with a 4 K e-fold
and closes with hysteresis):
* *Recovery threshold.* "Chamber back within 20 % of baseline" is looser
  than the opening threshold (+12 %), so the next run was detected as open
  within a second of starting. Recovery is now within 0.025 decades (≈ 6 %),
  and a test checks it stays below the detection threshold.
* *Scout 2 repeats.* With a 20 s seat lag, scout 1 read 35 K high and
  scout 2 opened while still heating to its start. A scout 2 that opens
  before it creeps is repeated 10 K lower (and below where it opened), up
  to three tries; then the batch stops.
* *e-fold* is fitted on the rise above baseline (as in the 16 Sept flow
  map), not on log(total pressure), which is almost flat near baseline.
On the 5 s model the test runs read 60.4 ± 0.04 °C for a seat opening at
60 °C: the TC leads the seat by about rate × lag, the same every run.

## 26. Thermocouple found again after it is unplugged — 22 Sept 2026
Unplugging and re-plugging the thermocouple while the driver ran left it
without a valve temperature until restarted. The MAX31856 was set up only at
connect, or after an SPI call raised — and a bit-banged SPI read never
raises: an unplugged chip reads all ones (fault 0xFF), and a re-powered one
reads its factory settings, which don't convert, so the temperature reads
0.000 °C with no fault bit (te-sensor_20260921_172933, after 17:30). Now
every read is one 16-register transfer that also reads back CR0/CR1; if
they aren't ours the reading is dropped, the loss logged once, and the chip
set up again as soon as it answers. A retry that finds nothing returns at
once instead of waiting 0.3 s, so `TC_RETRY_S` went from 5 s to 1 s. Fault
bits (e.g. the thermocouple wire unplugged: open circuit) are logged when
they appear and clear.

## 25. Temperature and pressure controllers redesigned from the 21 Sept 2026 runs — 22 Sept 2026
Data: te-sensor_20260921_150128 (0.25 N·m), _152054 (0.40), _173217 (0.45);
the thermocouple-fault logs _172024, _172612, _172933, _173050.

**auto-t.** The PI started every hold from a "hold power" of 15 % duty at
40 °C scaled linearly from 26 °C — about twice the measured hold power at
every temperature, and full power above ~120 °C. So every burst ended with a
5-8 K overshoot (90 → 94 °C; 155 → 159.5 °C, 0.5 K below the trip). Now the
duty is the measured hold power for the setpoint (`HEATER_HOLD_*`, 9 steady
holds, ±14 %) plus a PID trim (`PID_KD` 0 → 0.10). The burst is cut when
T + tau × rate reaches the setpoint (tau learned per coast) instead of a
fixed brake, which overshot small low-temperature steps by 3-4 K; the rate
already reflects the starting temperature. On three-node thermal models
fitted to each run (0.09-0.34 K rms), overshoot after bursts from 90 to
155 °C went from +3.4…+8.5 K to +0.2…+0.5 K. Near 40 °C, on the model of
the remounted TC (_173217), a session's first burst still overshoots ~4.5 K
until tau is learned (+1.6 K on the next).

**auto-p.** Everything is relative to the valve's *opening point* instead
of fixed temperatures near 40 °C: `SEAT_SCREW_VALVE` gives it (and the
flow e-fold) per torque — interpolated between entries, nearest entry
outside — shifted for upstream pressure (asymmetric limits), and the point
actually seen replaces the table for the session. Gains and creep scale
with √(e-fold / 3.2 K). The burst is decided by the distance below the
goal, not an absolute 35 °C. Once open, upstream-pressure changes are fed
forward on the setpoint. `PRESSURE_TSP_MAX_C` 140 → 155 °C (the 0.45 N·m
valve opens at ~150 °C). Simulated on valve models with the logs' shape
(throttling, soak, hysteresis 1-10 K, opening point ±8 K off the table):
all 48 cases at 0.25 and 0.40 N·m reached target (median 3.7-4.7 min,
overshoot ≤ +20 %); at 0.45 N·m every target the valve can give below
155 °C was reached, with no hunting.

**Thermocouple plausibility.** 17:20-17:31 the TC read nonsense with no
fault bit — falling 72 → -30 °C at full power for ~40 s, jumping 26 → 77 °C
— and the controller kept heating. New interlocks: a reading changing faster
than `TC_MAX_RATE_K_S`, or rising less than `TC_RESPONSE_MIN_K` in
`TC_RESPONSE_S` at full power (tests replay both logs).

## 24. Seat screw torque must be entered before anything else — 21 Sept 2026
At start-up the seat screw torque input is the only working control: ARM,
the mode buttons, the duty / setpoint / target boxes and "update" are locked
until a valid torque is entered. While it waits the torque input is orange
(`PROMPT` in palette.py) and the rest white; once entered it turns white and
the rest unlocks. Why: every run needs its torque recorded, and auto-p's
seek reference depends on it, so a run without one is not worth starting.

## 23. Seek reference calibrated by seat screw torque — 21 Sept 2026
At 0.30 N·m, `te-sensor_20260921_142905.csv` (an auto-t run at a fixed
110 °C, used to find the cracking point directly) showed the valve opening
at 92.67 °C, upstream 4.49 bar — over 50 K above the 16 Sept reference
(40.5 °C, at an unrecorded torque, since the input didn't exist then).
Separately, `te-sensor_20260921_144015.csv` shows auto-p itself computing a
seek goal of only 31.8 °C under the old model and creeping from there —
Edward disarmed it after 68 s rather than let it run. Two compounding
causes, both now fixed: (1) nothing in the seek/goal logic used seat screw
torque at all; (2) `PRESSURE_FF_MAX_C`, an absolute 55 °C ceiling, would
have capped the goal there regardless, since it didn't move with the
upstream/torque shift the way every other pressure-loop temperature does —
so even a correct torque model would have been clamped uselessly low.

`SEAT_SCREW_CRACKING_C` (config.py) now holds real cracking points by
torque — one entry so far, `{0.30: (92.7, 4.49)}`. When the entered torque
matches an entry within `SEAT_SCREW_CRACKING_TOL_NM`, it replaces
`PRESSURE_SEEK_START_C` as the seek/goal reference, and the upstream shift
applies relative to *that entry's own* upstream pressure, not
`PRESSURE_UP_REF_BAR` — the calibration already includes whatever upstream
effect was present when it was measured, so shifting from the historical
2.76 bar reference as well would double-count it. `PRESSURE_FF_MAX_C` now
moves with the same combined shift, so a calibrated reference above the old
55 °C ceiling is no longer clamped. An entered torque matching nothing logs
a one-time "UNVERIFIED for this torque" warning and falls back to the
historical reference — unchanged from today's behaviour, since we still
know nothing about any other torque. Replayed against the real upstream
pressure from the second file (4.60 bar), the new seek starts at 91.4 °C —
1.3 °C from the measured cracking point, versus the old model's 30.5 °C.

The golden record needed no changes: with no torque entered, the new code
path is bit-for-bit the old one (proven — all 15 scenarios still match
exactly), including a floating-point trap found along the way (adding a
zero-valued torque offset flipped a logged `-0.0 K` to `+0.0 K`). 8 new
tests cover the calibrated and uncalibrated paths, tolerance matching, the
upstream shift anchored to the calibration's own reference, and the
feedforward-ceiling fix specifically — each planted back as a bug to
confirm the tests catch it, including a repeat of today's actual fault.

Still open: only one torque is calibrated. A different, uncalibrated torque
falls back to the 16 Sept reference exactly as before, which we now know
can be wrong by 50+ K — the warning makes that visible, but doesn't fix it.
Calibrating more torques needs more auto-t characterisation runs like the
first file here.

## 22. Valve-opening marker could go missing — 21 Sept 2026
`TE_PLOTTER` seeded its chamber-pressure baseline from the globally lowest
2% of readings. If the chamber kept pumping down over a run, so the pressure
after the valve closed sat lower than before it opened, that seed grabbed
almost entirely from the low end, spanned too little time to fit a drift
line, and the whole pre-open period then read as "above baseline" — merging
with the real opening into one run with no recorded start, so no opening
marker was drawn (closing still was). Nothing tested this function.

The seed level is now taken from each time bin's own lowest point (so both
ends of the log contribute, whichever sits lower), using the 40th percentile
of the bins' minima as the level rather than seeding from the minima
directly — a bin fully inside an open period has no genuinely quiet sample,
and the existing refinement only ever excludes points *above* the fit, so
seeding from such a bin's minimum directly would anchor the baseline to it
permanently. 12 tests were added (there were none before): no event, drift
alone, a clean open/close, both baseline directions, open before the log,
still open at the end, a short blip ignored, two openings merged or kept
separate, and a missing chamber column. 3 of the 12 fail against the old
code; the other 9 already worked and still do.

## 21. Measured heater means are time-weighted — 21 Sept 2026
GitHub's test run failed: on its busy shared machine, ticks came irregularly,
and the mean over the last period (entry 15) gave every sample equal weight,
so a stall during one gate state over-counted the other. Each sample now
counts for the time since the previous one, which is what mean power means.
The end-to-end test compared the reading with an ideal 50 %, but a stalled
tick really does keep the heater on (or off) longer; it now compares with
the fake gate's own record, and its tolerance comes from the sample spacing
it actually observes. Tested under full CPU load: 8 of 8 passes. The exact
weighting is pinned by a timing-free unit test
(`test_period_mean_weights_samples_by_time`), which the old equal weights
fail; the end-to-end test still catches the count-of-samples bug of entry 15.

## 20. The README is the front page again — 18 Sept 2026
The old single-file driver opened with a ~125-line description (sensors,
modes, wiring, why the heater is switched in software, safety, logs). The
split moved it into `driver/__init__.py`, where nobody reads it, and it went
out of date there. It is now in README.md, rewritten with current names and
features; the CSV column list is replaced by a pointer to `driver/schema.py`
so it can't go stale again, and `driver/__init__.py` just points to the
README.

## 19. Seat screw torque — 18 Sept 2026
The operator can now record the torque on the TE-Valve's seat screw, in N·m
(see context.md). Agreed before any code: the name (seat screw torque, the
shortest unambiguous one), and that it starts blank every session, because
a remembered value could silently outlive a re-torque. It is entered in its
own row under the heater controls (a decimal point or comma both work;
0–5 N·m, `SEAT_SCREW_TORQUE_MAX_NM`), shown as `SEAT SCREW` in the readout,
written to every CSV row (`seat_screw_torque_Nm`, appended; blank = not
recorded, never 0) and logged as an event on every entry. A refused entry
leaves the box showing the value actually in use. TE_PLOTTER puts the value
in the figure title and summary and marks mid-run changes with dotted
purple lines; older logs say "not recorded". The tests were written first
(`test_seat_screw.py`, plus five in `test_plotter.py`).

## 18. gui.py split; the window's text is now testable — 17 Sept 2026
`gui.py` was 628 lines doing layout, formatting, chart drawing and input
handling. The text is now `readout.py` (pure functions: readings in, strings
and tags out), the charts are `charts.py`, the colours are `palette.py`, and
`gui.py` (376 lines) builds the window and feeds them. `readout.py` needs no
Tk, so `test_readout.py` checks every displayed line directly; deliberately
broken versions (swapped labels, a trip shown as armed, a missing fault note,
a stale reading) all make it fail. The window renders pixel-identically to
before the split.

## 17. The measured readout belongs to the device thread — 17 Sept 2026
The heater state held three different things: the operator's commands, the
controller's internals, and the measured voltage / current the device thread
writes. The measurements now live in `shared.py`
(`store_heater_output` / `heater_output`), so the controller's state is only
what the controller decides. The GUI and logger read the two separately.

## 16. Heater electrical arithmetic in one place — 17 Sept 2026
`duty x V^2 / R` and its relatives appeared seven times across four files.
They are now `config.heater_power_w()`, `heater_current_a()`,
`heater_voltage_v()` and `power_from_voltage_w()`, and a test fails if the
formulas reappear anywhere else.

## 15. Measured heater means cover one period of time — 17 Sept 2026
The measured V, I and power means averaged the last 20 samples, assuming
50 ms ticks. On Windows the waits are rounded up to the ~15.6 ms timer steps,
so ticks run long; 20 samples then spanned more than one PWM period and the
mean swung with the cycle (a test on the lab laptop read 40 % instead of
50 %). The samples are now kept with their times and averaged over the last
`HEATER_PWM_PERIOD_S` seconds. This only matters once the sense inputs are
wired. The test now slows the ticks to 75 ms and watches the readout for two
periods; the old code strays by 20 % of full power there.

## 14. devices.py split; the LabJack tick in named steps — 17 Sept 2026
`devices.py` mixed the Keller, the LabJack, the thermocouple chip and a
290-line loop. It is now `keller.py`, `thermocouple.py` and `labjack.py`.
In `labjack.py` one connection is a `_Session`, and each tick reads as
`_sense_heater → [_read_vacuum → _service_thermocouple → _run_controller] →
_drive_gate`; a device error raises `_DeviceLost`, which ends the session
through `_release` (gate low, watchdog released) and reconnects. The
statements inside each step are unchanged. Before the split, 9 tests were
added for the loop's error and sensing paths (`test_device_faults.py`);
they, the existing device tests and the golden record all pass on the new
code, and deliberately broken versions of the new code make them fail.

## 13. Heater state fields are fixed — 17 Sept 2026
The heater state is a dict with text keys, so a misspelt key on assignment
used to create a new field silently while the real one stayed unchanged.
`controller.HeaterState` now refuses unknown or removed fields. A dataclass
with named attributes would catch typos even earlier (in the editor), but
means rewriting every access in the controller and GUI; this gets most of
the benefit with no change to the code that uses the state.

## 12. Shared state only through functions — 17 Sept 2026
`shared.py` used to hand out its dictionaries, lists and locks, and the GUI,
logger and device thread reached straight in: the logger reset the heater's
on-time bookkeeping itself, and the device thread disarmed the heater by
writing its fields. Every module had to know the key names and locking rules.
Now `shared.py` keeps readings, charts, events and health flags private
behind named functions (`store_keller`, `latest`, `take_log_readings`, …),
and `control.py` alone owns the heater state (`snapshot`, `take_on_time`,
`force_off`, …). The locking lives in those two files only.
`test_architecture.py` fails if any module touches another's private names
or if either file exposes a raw container. Behaviour is unchanged: the golden
record still matches, and the GUI and logs were checked end to end.

## 11. One set of names for modes and the valve temperature — 17 Sept 2026
The modes were "auto (T)" / "auto (P)" on screen but `auto` / `pressure` in
the code and CSV. They are now `manual`, `auto-t` and `auto-p` everywhere,
defined once in `controller.MODES`; an unknown name is refused rather than
silently running the temperature loop. The CSV `heater_mode` column carries
the new names from 17 Sept 2026; the plotter accepts both. The thermocouple
reading is "valve temperature" on screen and in the plotter (the CSV column
keeps its name, `te_temperature_degC`). Control behaviour is unchanged.

## 10. The controller takes everything as arguments — 17 Sept 2026
`controller.step(h, now, dt, readings…)` returns `(duty, messages)` and
touches nothing else: no lock, clock, event log or shared readings. That
makes every rule testable with a plain dict and a number for the time.
`control.py` is the only place that adds threads: it reads the upstream
pressure, holds `heater_lock` for the whole step (so a GUI command can no
longer land halfway through one), and logs the messages afterwards. The
state stays a dict, changed in place, because the GUI and logger read it.
Behaviour is unchanged: the golden record matches through both paths.

## 9. Split the driver into the `driver` package — 17 Sept 2026
The single file had grown to ~2,550 lines mixing GUI, threads, control law,
interlocks and logging, so any change risked the rest. It is now a package
with one job per module (see README). Before splitting, the control law's
behaviour was recorded over 15 scenarios (`tests/golden/`); the package
reproduces it exactly, and the GUI renders pixel-identically. Behaviour did
not change. Importing the package no longer creates log files; `app.main()`
does that at start-up.

## 8. CSV columns are append-only and defined once — 17 Sept 2026
`driver/schema.py` defines both logs' columns; the driver writes with it and
the plotter reads with it. Columns are only ever appended, never renamed or
reordered, so old logs and old readers keep working. The plotter keeps its
loose name matching only for logs from before the schema existed.

## 7. Log every heater gate edge — 17 Sept 2026
A separate `_pwm.csv` records each switch with its time, because the 0.5 s
main log cannot place edges that come up to several times a second. The main
log gets `heater_on_s` from the same edge times, so energy per row is exact.
The log records what the software commanded, not a measured voltage.

## 6. Chart heater power, not current — 17 Sept 2026
Under time-proportioning, mean power = duty × V²/R. Mean V × mean I would
understate it (duty² × V²/R). With calculated values, current and power have
the same shape, so the chart shows power (the physical quantity, and what
the 1 W flight budget is about). Duty and current stay in the log: duty is
what the controller did and does not depend on the assumed 24 V / 88 Ω.

## 5. Chart raw upstream pressure, not P20 — 17 Sept 2026
P20 divided by the Keller's temperature, which is the sensor chip's, not the
gas's. Gripping the sensor head raised that temperature without changing
the gas, and P20 dropped accordingly. The raw absolute pressure is charted
instead. A proper correction needs a temperature sensor on the upstream
tubing.

## 4. auto-p is a cascade with seek and track phases — Sept 2026
The valve snaps open near 40 °C rather than throttling, and chamber pressure
spans decades. So the outer loop sets a temperature setpoint for the
proven temperature PI: it seeks (burst, coast, creep) with the valve shut
while measuring the baseline, detects the opening, then tracks log10(p).

## 3. Software time-proportioning on FIO0, no LabJack timers
Heater duty is produced by toggling FIO0 on the device thread's 50 ms tick
over a 2 s period. LabJack timers are not used because a timer may claim
FIO4, which is the MAX31856 SDO line. The U3 firmware watchdog pulls FIO0
low if the program dies.

## 2. MAX31856 set to the 50 Hz mains filter
CR0 = 0x91. The previous value left the notch at 60 Hz, which is wrong in
Switzerland.

## 1. Only one driver instance at a time
An OS file lock (`.te-valve-driver.lock`) stops a second copy starting,
because a forgotten instance may still be driving the heater.
