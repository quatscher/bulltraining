# Changelog

## 0.6.0

- Absences (surgery, illness, travel) on the plans page: per sport a date from which it is allowed again. The
  generator plans nothing blocked (not even recurring sessions like commuting), the rules reject proposals into an
  absence, and the pause weeks lower the load corridor so the comeback starts conservatively
- Generator: no second hard session on a test day, no strength sessions on consecutive days
- Calendar, inbox and session page show the sport as a coloured tag (test titles like "FTP-Test" don't name it)
- Fix: a missed past session no longer blocks the "at least one free day" rule, and it can be deleted from the plan
  (moving or changing past sessions stays locked)

## 0.5.0

- Performance page: card *Schwellen in intervals.icu* compares the thresholds from your tests with the sport
  settings in intervals.icu (threshold HR, max HR, threshold pace, FTP, HR zones) and writes them on click –
  so the watch and intervals.icu use the same zones. Never automatic; sports without a test are left untouched

## 0.4.3

- Fix: weeks before the date of a self-reported baseline keep counting with the stated volume once real data
  arrives (data quality "mixed"). Previously one old activity made the data look "ok", the weeks before a data
  restart counted as zero and the weekly load cap collapsed (e.g. to 91)
- Test result hint updated for absolute targets

## 0.4.2

- Workout targets are absolute (pace per km/100 m, watts) instead of percentages: intervals.icu and the watch
  evaluate percentages against their own thresholds, which are often missing or different
- Test protocols without percentage targets; hard sections are `intensity=interval` steps (by feel)
- Ramp test as 1-minute steps in watts

## 0.4.1

- Swim workouts are planned in metres (50 m grid) so Garmin advances steps by lap count in the pool;
  pace comes from the CSS test or, without one, from recent swims
- Rest steps use `intensity=rest`: shown as rest on the watch and advance automatically

## 0.4.0

- New page *Rad*: bike fit setup per bike (saddle, cockpit, pads, extensions, custom fields) with dated versions,
  change history and photos; MCP tool `get_bike_setup`

## 0.3.2

- "Test connection" replaces athlete ID `0` (owner of the key) with the real ID, e.g. `i123456`

## 0.3.1

- intervals.icu API key and athlete ID can be managed on the settings page (masked, applied without restart);
  the add-on option remains as fallback
- "Test connection" shows account, Garmin status, latest activity and wellness days
- The automatic sync starts as soon as a key is set, no restart needed

## 0.3.0

- First public release as a Home Assistant add-on repository (AGPL-3.0)
- Prebuilt images for `aarch64` and `amd64`
- Web UI via ingress only by default; MCP port disabled by default
- JavaScript libraries bundled (no CDN requests)
- English/German option translations and documentation

## 0.2.x

- Add-on operation: ingress web UI, MCP over HTTP with token, periodic sync, one-time database import
- Review fixes: atomic plan confirmation, conflict-checked undo, publisher revisions, strict workout parser
