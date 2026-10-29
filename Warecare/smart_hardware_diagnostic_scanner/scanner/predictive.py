"""Predictive disk-full forecasting for the Predictive Health card.

Pure-stdlib forecaster behind the "Predictive Health" card: it takes a
short history of disk ``used_pct`` samples for one mounted volume and
fits a straight line through it (ordinary least squares) to estimate
when that volume reaches 100% used. Deliberately dependency-free --
standard library only, no numpy/scipy/matplotlib, and no imports from
this repo: the whole selling point is that this runs inside the packaged
scanner with zero extra installs.

Data in: ``series`` is a list of ``(ts_unix_int, used_pct)`` tuples
sorted ascending, roughly one sample every 60 s, as recorded by
``scanner.metrics_db.disk_series``. Percentages may be ``None`` (the
recorder stores NULL) and timestamps may repeat. Cleaning policy,
applied identically before any fit:

* an entry whose percentage is ``None`` or non-finite is dropped;
* duplicate timestamps collapse into one point by averaging their
  known (non-None) percentages -- a duplicate is a re-read, and the
  average blunts a flaky sample without inventing a new timestamp;
  a duplicate group that is all None disappears with its timestamp;
* entries whose timestamp is not a whole number of seconds are floored
  to the second, the result is sorted ascending (idempotent), and the
  ``min_samples`` count refers to these unique kept points.

The exhaustive meaning of every ``Prediction.status`` value lives on the
:data:`Prediction` docstring -- UI code branches on those exact strings.

Metric-level extension (same discipline, beyond disk): :func:`forecast_metric`
fits the same OLS through any percent series (CPU %, RAM %, per-mount disk
%) and projects its level at a whole-day horizon, gated by the same
minimum-sample / minimum-span / R^2 quality discipline and additionally by
a MAD-based outlier rejection. :func:`detect_anomalies` flags statistically
unusual recent windows -- sustained elevation against the series' own
weekday/hour seasonal baseline when history supports one (so a nightly
backup's CPU spike is not an anomaly), or a slow baseline creep -- using
only :mod:`statistics` (z-scores, means, medians): still zero dependencies.
:func:`forecast_health` combines the component projections into a 0..100
health-score trajectory with the GUI's renormalisation policy (a component
without a usable projection is left out, never invented), and
:func:`health_forecast_sentence` renders the plain-English verdict.
:func:`predict_health` and :func:`predict_health_payload` are the
never-raise integration entry points: they pull hourly-aggregated series
from ``scanner.metrics_db`` (30 days of 60 s sampling is ~43k raw rows but
only 720 hourly buckets) and build the whole Predictive Health card's data
payload; every failure degrades to an honest status, never an exception.

Every function here is pure and deterministic: timestamps in, verdicts
out, no ``time.time()`` below the ``predict_*`` plumbing (and there the
clock is an injectable ``now_ts``).
"""
from __future__ import annotations

import logging
import math
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

__all__ = [
    "OlsFit",
    "Prediction",
    "MetricPrediction",
    "Anomaly",
    "AnomalySet",
    "HealthForecast",
    "fit_ols",
    "forecast_disk_full",
    "forecast_metric",
    "detect_anomalies",
    "detect_all_anomalies",
    "forecast_health",
    "health_forecast_sentence",
    "predict_all",
    "predict_health",
    "predict_health_payload",
]

log = logging.getLogger("shds.predictive")

#: Slopes with |slope| at or below this many percentage points per hour
#: count as flat ("no_trend"); anything above is a real filling rate.
SLOPE_EPS_PCT_PER_HOUR = 1e-9


@dataclass
class OlsFit:
    """Ordinary least squares of ``y`` against ``x``.

    ``slope``/``intercept`` define ``y ~ slope*x + intercept``. ``r2`` is
    the coefficient of determination clamped into [0.0, 1.0], or None
    when it is undefined (all x identical, or all y identical): a fake
    0.0 quality for a fit that explains nothing would be worse than
    "unknown", and a zero-slope fit needs no quality gate anyway.
    """

    slope: float
    intercept: float
    r2: float | None

    @classmethod
    def from_points(cls, points: Sequence[tuple[float, float]]) -> OlsFit:
        """Fit ``(x, y)`` pairs by OLS; see :func:`fit_ols` for details."""
        return fit_ols(points)


def fit_ols(points: Sequence[tuple[float, float]]) -> OlsFit:
    """Fit ``(x, y)`` points with ordinary least squares.

    ``slope = cov(x, y) / var(x)``, with both moments taken around the
    mean so raw unix timestamps in x cannot lose precision to
    catastrophic cancellation. ``r2 = Sxy^2 / (Sxx * Syy)`` is exactly
    the coefficient of determination for a linear fit (it cannot be
    negative in exact arithmetic; floating-point dust is clamped into
    [0.0, 1.0]). When the reducing denominator is 0.0 -- all x equal,
    or all y equal -- ``r2`` is None instead of a fake 0.0; an
    all-x-identical input degenerates to slope 0.0 / intercept mean(y),
    and an empty or single-point input to slope 0.0, intercept mean(y)
    (0.0 when empty) and r2 None. Non-finite pairs dropped before fit.
    """
    usable = [(float(x), float(y)) for x, y in points
              if math.isfinite(float(x)) and math.isfinite(float(y))]
    if not usable:
        return OlsFit(0.0, 0.0, None)
    n = len(usable)
    mean_x = sum(x for x, _ in usable) / n
    mean_y = sum(y for _, y in usable) / n
    var_x = sum((x - mean_x) ** 2 for x, _ in usable)
    cov_xy = sum((x - mean_x) * (y - mean_y) for x, y in usable)
    if var_x <= 0.0:  # x never varies: the constant fit is all there is
        return OlsFit(0.0, mean_y, None)
    slope = cov_xy / var_x
    intercept = mean_y - slope * mean_x
    ss_yy = sum((y - mean_y) ** 2 for _, y in usable)
    if ss_yy <= 0.0:  # y never varies: r2 is 0/0, report None not 0.0
        return OlsFit(slope, intercept, None)
    return OlsFit(slope, intercept, min(1.0, max(0.0, cov_xy * cov_xy
                                                 / (var_x * ss_yy))))


@dataclass
class Prediction:
    """Forecast verdict for one mounted volume.

    Status contract (exact strings; the UI branches on these and never
    on prose -- the field order is mount, minutes, eta, rate, r2, to
    keep positional construction readable):

    ``ok``
        Data sufficient, the fitted slope is meaningfully positive
        (filling, slope > 1e-9 pct/hour) and qualifies on quality
        (``r2 >= r2_floor``), and the horizon lands within
        ``far_future_days``. All fields populated and usable.
    ``insufficient_data``
        No attempt was made. Fewer unique samples than ``min_samples``,
        a first-to-last span below ``min_span_hours`` (exactly at the
        boundary passes both gates), or no usable percentage at all:
        a missing history never becomes a forecast. Every numeric field
        is None.
    ``no_trend``
        Data suffices but the fitted slope is flat (|slope| <= 1e-9
        pct/hour): the volume is not filling at a measurable rate.
        ``minutes_to_full``/``eta`` are None; ``rate_pct_per_day``
        (approximately 0) and ``r2`` follow from the real fit.
    ``shrinking``
        The fitted slope is negative: usage fell over the window
        (cleanup, retention, user action). No full-disk forecast.
        Like ``no_trend``, only ``rate_pct_per_day`` (negative) and
        ``r2`` are populated; ``minutes_to_full``/``eta`` are None.
    ``unreliable``
        A positive slope exists but the fit fails the quality gate --
        ``r2`` present but below ``r2_floor``, ``r2`` undefined
        (degenerate variance), or the arithmetic degenerated to a
        non-finite value. An ETA would be invented precision:
        ``minutes_to_full``/``eta`` are None while ``rate_pct_per_day``
        and the too-low (or missing) ``r2`` stay visible for honesty.
    ``far_future``
        The fit qualifies and a real forecast was computed, but the
        horizon lies strictly beyond ``far_future_days * 1440``
        minutes. ``minutes_to_full`` keeps the raw computed value and
        ``eta`` is populated only when it still fits inside a
        ``datetime`` (else None); treat this as "not filling soon
        enough to matter", not as a date.

    ``minutes_to_full`` is whole minutes via ``ceil``; ``rate_pct_per_day``
    is always ``slope * 24.0`` where slope is pct/hour; ``eta`` is local
    time at ``last_ts + minutes*60`` and, pathologically, may be None
    even for ``ok`` if that arithmetic leaves what ``datetime`` can
    represent (never expected for sane unix seconds).
    """

    mount: str
    minutes_to_full: int | None
    eta: datetime | None
    rate_pct_per_day: float | None
    r2: float | None
    status: str


def _clean_series(
    series: Sequence[tuple[int, float | None]],
) -> list[tuple[int, float]]:
    """Apply the documented cleaning policy to a raw series (see module)."""
    grouped: dict[int, list[float]] = {}
    for entry in series or ():
        try:
            ts, pct = entry
            key = int(ts)
            value = float(pct)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        grouped.setdefault(key, []).append(value)
    return [(ts, sum(values) / len(values))
            for ts, values in sorted(grouped.items())]


def forecast_disk_full(
    mount: str,
    series: Sequence[tuple[int, float | None]],
    min_samples: int = 12,
    min_span_hours: float = 6.0,
    r2_floor: float = 0.60,
    far_future_days: float = 3650.0,
) -> Prediction:
    """Estimate when volume ``mount`` reaches 100% used.

    ``min_samples`` is the unique-sample floor, ``min_span_hours`` the
    first-to-last coverage floor (both inclusive as documented), and
    ``far_future_days`` is measured per day (times 1440 for minutes).
    Never raises on bad series data: unusable entries are dropped and a
    series with nothing usable is ``insufficient_data`` (see
    :data:`Prediction` for the exact status contract). Both gates and
    the far-future bound are inclusive-at-boundary; only strictly-below
    or strictly-beyond demotes.
    """
    def _bare(status: str) -> Prediction:
        """A verdict with no numbers at all behind it."""
        return Prediction(mount, None, None, None, None, status)

    kept = _clean_series(series)
    if len(kept) < min_samples:
        log.debug("%s: %d usable samples, needs %d", mount, len(kept),
                  min_samples)
        return _bare("insufficient_data")
    last_ts = kept[-1][0]
    if (last_ts - kept[0][0]) / 3600.0 < min_span_hours:
        log.debug("%s: span below %.3fh gate", mount, min_span_hours)
        return _bare("insufficient_data")

    # Fit in hours with the origin at the last sample: the slope comes
    # out in pct/hour directly, and the intercept is exactly the fitted
    # percentage at the last sample (used as the ETA's starting point).
    fit = fit_ols(((ts - last_ts) / 3600.0, pct) for ts, pct in kept)
    rate = fit.slope * 24.0
    if not math.isfinite(fit.slope) or (fit.r2 is not None
                                        and not math.isfinite(fit.r2)):
        return Prediction(mount, None, None, None, fit.r2, "unreliable")
    if abs(fit.slope) <= SLOPE_EPS_PCT_PER_HOUR:
        return Prediction(mount, None, None, rate, fit.r2, "no_trend")
    if fit.slope < 0.0:
        return Prediction(mount, None, None, rate, fit.r2, "shrinking")
    # Filling: qualify the fit before quoting any date.
    if fit.r2 is None or fit.r2 < r2_floor:
        return Prediction(mount, None, None, rate, fit.r2, "unreliable")

    fitted_last = fit.intercept  # fitted pct at the last sample
    if not math.isfinite(fitted_last):  # unreachable on sane data; guard
        return Prediction(mount, None, None, rate, None, "unreliable")
    minutes = 0
    if fitted_last < 100.0:
        hours_to_full = (100.0 - fitted_last) / fit.slope
        minutes = max(0, math.ceil(hours_to_full * 60.0))  # clamp fp dust
    if minutes > far_future_days * 1440.0:
        try:
            eta = datetime.fromtimestamp(last_ts) + timedelta(minutes=minutes)
        except (OverflowError, OSError, ValueError):
            eta = None  # beyond what a datetime can represent
        return Prediction(mount, minutes, eta, rate, fit.r2, "far_future")
    try:
        eta = datetime.fromtimestamp(last_ts + minutes * 60)
    except (OSError, OverflowError, ValueError):
        log.debug("%s: eta out of datetime range; kept minutes only", mount)
        eta = None
    return Prediction(mount, minutes, eta, rate, fit.r2, "ok")


def predict_all(
    db_path: str | Path | None = None,
    *,
    now_ts: int | None = None,
    **forecast_kwargs: Any,
) -> list[Prediction]:
    """Forecast every mount in the metrics history (optional integration).

    ``scanner.metrics_db`` is the sibling SQLite module of this feature
    and is imported lazily inside this function so the pure math above
    stays importable standalone (tests import just this module freely).
    ``db_path=None`` resolves to ``metrics_db.default_db_path()``;
    ``now_ts`` is forwarded to ``disk_series`` so tests can pin the
    clock; ``forecast_kwargs`` pass through to
    :func:`forecast_disk_full`.

    Failure policy (no errors escape): a missing/unopenable module or a
    mount listing that fails yields ``[]``; one mount whose series
    cannot be fetched or forecast degrades to an ``insufficient_data``
    Prediction so the card still lists the drive instead of silently
    dropping it.
    """
    try:
        from . import metrics_db  # noqa: PLC0415 -- deliberate lazy import
    except Exception as exc:  # the optional sibling may simply not exist
        log.debug("metrics_db unavailable; no predictions: %s", exc)
        return []
    try:
        path = db_path if db_path is not None else metrics_db.default_db_path()
        mounts = metrics_db.disk_mounts(path)
    except Exception as exc:
        log.debug("metrics_db mount listing failed: %s", exc)
        return []
    predictions: list[Prediction] = []
    for mount in mounts or []:
        try:
            rows = metrics_db.disk_series(path, mount, now_ts=now_ts)
            points = [(int(row[0]), row[1]) for row in rows if len(row) >= 2]
            predictions.append(
                forecast_disk_full(mount, points, **forecast_kwargs))
        except Exception as exc:
            log.debug("%s: series/forecast failed: %s", mount, exc)
            predictions.append(Prediction(mount, None, None, None, None,
                                          "insufficient_data"))
    return predictions


# ---------------------------------------------------------------------------
# Metric-level forecasting (CPU / RAM / disk level at a horizon)
# ---------------------------------------------------------------------------

#: Unique-sample floor for a metric fit, mirroring forecast_disk_full's
#: gate: 12 hourly buckets over at least 6 h is the least history a line
#: is allowed to be honest about.
METRIC_MIN_SAMPLES = 12
#: First-to-last coverage floor in hours (inclusive at the boundary),
#: same discipline and value as the disk forecast.
METRIC_MIN_SPAN_HOURS = 6.0
#: Quality gate: a fit whose r2 lands below this does not get to quote a
#: horizon projection. Same floor as the disk forecast.
METRIC_R2_FLOOR = 0.60
#: Fitted rates at or below this many percentage points per DAY count as
#: flat ("no_trend") for CPU/RAM-style metrics: a 0.01%/day RAM drift is
#: measurement dust, not pressure. (The disk forecast keeps its own much
#: smaller per-hour epsilon -- a disk only ever fills in one direction.)
METRIC_SLOPE_EPS_PCT_PER_DAY = 0.05
#: Outlier rejection: points whose deviation from the series median
#: exceeds this many MADs (median absolute deviation) are dropped before
#: any fit or baseline. Generous on purpose -- a real ramp must survive;
#: this only removes the single-sample glitch (a driver's 100% CPU tick).
OUTLIER_MAD_MULT = 6.0
#: Component weights for the health-score forecast, mirroring the GUI's
#: ``ewarecare.HEALTH_WEIGHTS`` (cpu .30 / mem .25 / disk .25 / os .20):
#: the OS component needs scan findings, which a metrics history does not
#: carry, so it is renormalised away exactly like an unavailable sensor.
HEALTH_WEIGHTS = {"cpu": 0.30, "ram": 0.25, "disk": 0.25}
#: A projected score at least this many points below the current one is
#: quoted as a "may drop" verdict in the plain-English sentence; less is
#: noise and is reported as "holds near".
HEALTH_DROP_ALERT_PCT = 2.0


@dataclass
class MetricPrediction:
    """Level forecast for one metric (CPU %, RAM %, one mount's disk %).

    Unlike the disk-full :data:`Prediction` this quotes a *level* at a
    horizon, not an ETA. Status contract (exact strings; the UI branches
    on these; field order is metric, subject, horizon_days, current_pct,
    projected_pct, rate_pct_per_day, r2, status):

    ``ok``
        Data sufficient, the fitted slope rises beyond the meaningfulness
        floor (rate > ``slope_eps`` pct/day) and qualifies on quality
        (``r2 >= r2_floor``). ``current_pct`` is the fitted level at the
        last sample; ``projected_pct`` is that fit carried to the
        horizon, clamped into [0, 100]. All fields populated.
    ``no_trend``
        Data suffices but the fitted rate is flat (|rate| <= slope_eps
        pct/day): drift at that scale is measurement dust, not pressure.
        ``projected_pct`` is still populated -- it is the fitted current
        level (clamped), which IS the honest horizon projection of a
        flat series. This is the only status that quotes a number
        without the quality gate.
    ``shrinking``
        The fitted slope falls beyond the floor (usage declining --
        cleanup, a workload gone) and qualifies on quality.
        ``projected_pct`` is the clamped declining projection; a falling
        projection is quoted only through the same gate as a rising one.
    ``unreliable``
        A meaningful slope exists but the fit fails the quality gate --
        ``r2`` below ``r2_floor``, undefined (degenerate variance), or
        non-finite arithmetic. A horizon number would be invented
        precision: ``projected_pct`` is None while ``rate_pct_per_day``
        and the too-low ``r2`` stay visible; ``current_pct`` stays
        populated (the fitted last level is still the best estimate).
    ``insufficient_data``
        No attempt was made: fewer usable points than ``min_samples``
        after the documented cleaning *and* outlier rejection, a
        first-to-last span below ``min_span_hours`` (exactly at the
        boundary passes both gates), an unusable ``horizon_days``, or no
        usable percentage at all. Every numeric field is None.
    """

    metric: str
    subject: str
    horizon_days: int | None
    current_pct: float | None
    projected_pct: float | None
    rate_pct_per_day: float | None
    r2: float | None
    status: str


@dataclass
class Anomaly:
    """One detected statistically-unusual window in a metric's history.

    ``kind`` is exactly one of:

    ``spike``
        A sustained recent window elevated above the series' own
        baseline -- per weekday/hour bucket where enough same-bucket
        history exists (so a nightly backup's CPU spike reads as normal),
        the overall baseline otherwise. Evidence: ``z`` is the largest
        per-point z-score in the window, ``lift_pct`` the peak value's
        distance above its baseline mean.
    ``creep``
        A slow level shift: the last ``creep_window_hours`` average
        sits ``lift_pct`` points above the overall baseline mean (a RAM
        baseline creeping up 15% over 3 days is the canonical case).
        Compared against the overall baseline, not the seasonal one --
        a shift too slow to break any single window is exactly what the
        seasonal comparison washes out; a history shorter than a
        fortnight may therefore let a weekly rhythm read as creep, so a
        creep is advice, not a verdict.

    ``baseline`` records which baseline actually fired ("seasonal" when
    every point in the window had a same-bucket baseline, "overall"
    otherwise). ``z`` may be None for a creep with a degenerate baseline
    spread; ``start_ts``/``end_ts``/``peak_ts`` are raw epoch seconds.
    """

    metric: str
    subject: str
    kind: str
    start_ts: int
    end_ts: int
    peak_ts: int
    peak_value: float
    baseline_mean: float
    z: float | None
    lift_pct: float
    baseline: str


@dataclass
class AnomalySet:
    """Per-metric anomaly verdict; the honesty wrapper around the list.

    ``status`` is "ok" when detection actually ran (``anomalies`` may
    still be empty -- evaluated and clean is a result, not a failure) or
    "insufficient_data" when the history could not support an honest
    baseline. UI code must not treat an empty list with "ok" the same as
    "insufficient_data": the first says "nothing unusual", the second
    says "not enough history to know".
    """

    metric: str
    subject: str
    status: str
    anomalies: list[Anomaly]


@dataclass
class HealthForecast:
    """Projected 0..100 health score at one horizon, with its drivers.

    ``score`` is the weighted headroom (100 - projected usage) over the
    components that produced a usable projection, renormalised over the
    remaining weights -- exactly ``system_health_score``'s policy of
    leaving unavailable components out instead of pretending them
    unhealthy. ``drivers`` holds plain-English clauses (``"RAM pressure
    is trending up"``) for the components actually pulling the forecast;
    ``status`` is "ok" or "insufficient_data" (no usable component at
    all -- a missing history never becomes a score).
    """

    horizon_days: int
    current_score: float | None
    score: float | None
    status: str
    drivers: tuple[str, ...]


def _reject_outliers(
    points: Sequence[tuple[int, float]],
    mult: float = OUTLIER_MAD_MULT,
) -> list[tuple[int, float]]:
    """Drop points straying more than ``mult`` MADs from the median.

    MAD-based (median absolute deviation) so one 100% glitch cannot drag
    its own yardstick the way a mean/std rule would; a degenerate MAD of
    0.0 (more than half the points identical) rejects nothing -- there
    is no honest spread to reject against. Input order preserved.
    """
    pts = list(points)
    if len(pts) < 2:
        return pts
    values = sorted(v for _, v in pts)
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values)
    if not math.isfinite(mad) or mad <= 0.0:
        return pts
    limit = mult * mad
    return [(t, v) for t, v in pts if abs(v - med) <= limit]


def forecast_metric(
    metric: str,
    subject: str,
    points: Sequence[tuple[int, float | None]],
    horizon_days: int,
    *,
    min_samples: int = METRIC_MIN_SAMPLES,
    min_span_hours: float = METRIC_MIN_SPAN_HOURS,
    r2_floor: float = METRIC_R2_FLOOR,
    slope_eps: float = METRIC_SLOPE_EPS_PCT_PER_DAY,
    outlier_mult: float = OUTLIER_MAD_MULT,
) -> MetricPrediction:
    """Project one metric's percent level ``horizon_days`` ahead.

    The same OLS discipline as :func:`forecast_disk_full`, aimed at a
    level instead of an ETA: the series is cleaned (module cleaning
    policy), outlier-rejected (:func:`_reject_outliers`), gated on
    sample count and first-to-last span (both inclusive at the
    boundary), fitted in hours with the origin at the last sample, and
    only a quality-qualified slope may quote a horizon projection.
    ``metric`` is a machine tag ("cpu", "ram", "disk"); ``subject``
    carries the mount for per-disk rows ("" otherwise).

    ``points`` is ``(ts_unix_int, pct | None)`` as produced by
    ``metrics_db.cpu_ram_series_hourly`` / ``disk_series_hourly`` (raw
    60 s series work too -- the fit is timestamp-aware, internal gaps
    cost nothing but noise). Never raises on bad data: see the
    :data:`MetricPrediction` status contract; an unusable
    ``horizon_days`` is "no forecast attempted" (``insufficient_data``),
    not a crash. ``projected_pct`` and ``current_pct`` are clamped into
    [0, 100].
    """
    def _bare(status: str) -> MetricPrediction:
        """A verdict with no numbers at all behind it."""
        return MetricPrediction(metric, subject, None, None, None, None,
                                None, status)

    try:
        horizon_h = float(horizon_days) * 24.0
    except (TypeError, ValueError, OverflowError):
        log.debug("%s/%s: unusable horizon %r", metric, subject, horizon_days)
        return _bare("insufficient_data")
    if not math.isfinite(horizon_h) or horizon_h < 0.0:
        return _bare("insufficient_data")

    kept = _reject_outliers(_clean_series(points), outlier_mult)
    if len(kept) < min_samples:
        log.debug("%s/%s: %d usable samples, needs %d", metric, subject,
                  len(kept), min_samples)
        return _bare("insufficient_data")
    last_ts = kept[-1][0]
    if (last_ts - kept[0][0]) / 3600.0 < min_span_hours:
        log.debug("%s/%s: span below %.3fh gate", metric, subject,
                  min_span_hours)
        return _bare("insufficient_data")

    fit = fit_ols(((ts - last_ts) / 3600.0, pct) for ts, pct in kept)
    rate = fit.slope * 24.0
    if not math.isfinite(fit.slope) or (fit.r2 is not None
                                        and not math.isfinite(fit.r2)):
        return MetricPrediction(metric, subject, int(horizon_days), None,
                                None, rate, fit.r2, "unreliable")
    current = max(0.0, min(100.0, fit.intercept))
    if not math.isfinite(current):  # unreachable on sane data; guard
        return MetricPrediction(metric, subject, int(horizon_days), None,
                                None, rate, fit.r2, "unreliable")
    if abs(rate) <= slope_eps:
        return MetricPrediction(metric, subject, int(horizon_days), current,
                                current, rate, fit.r2, "no_trend")
    if fit.r2 is None or fit.r2 < r2_floor:
        return MetricPrediction(metric, subject, int(horizon_days), current,
                                None, rate, fit.r2, "unreliable")
    projected = max(0.0, min(100.0, fit.intercept + fit.slope * horizon_h))
    status = "ok" if fit.slope > 0.0 else "shrinking"
    return MetricPrediction(metric, subject, int(horizon_days), current,
                            projected, rate, fit.r2, status)


# ---------------------------------------------------------------------------
# Anomaly detection (seasonal-baseline aware, stdlib statistics only)
# ---------------------------------------------------------------------------

#: Baseline depth: how much history (in hours) a series may use to learn
#: its own "normal". Two weeks captures a full weekly rhythm.
ANOMALY_BASELINE_HOURS = 336.0
#: How far back the *recent* region being judged reaches (hours).
ANOMALY_RECENT_HOURS = 48.0
#: Width of the sliding windows judged inside the recent region (hours):
#: an anomaly is a *sustained* elevation, not one hot sample.
ANOMALY_WINDOW_HOURS = 6.0
#: A window is elevated when at least this fraction of its points are
#: individually flagged (enough coverage that one glitch cannot carry it).
ANOMALY_WINDOW_FLAG_FRACTION = 0.7
#: Fewer points than this in a window (or in the creep window) and the
#: window is simply not judged -- coverage too thin to be evidence.
ANOMALY_MIN_WINDOW_POINTS = 3
#: Minimum same-bucket (weekday, hour) history points for a bucket to
#: earn its own seasonal baseline.
ANOMALY_MIN_BUCKET_SAMPLES = 4
#: Per-point flag: a z-score at or above this AND an absolute lift of at
#: least ANOMALY_MIN_LIFT_PCT points over that point's baseline mean.
#: The absolute floor keeps a tiny-spread baseline from flagging 1-point
#: wiggles as "3 sigma".
ANOMALY_Z_FLOOR = 3.0
ANOMALY_MIN_LIFT_PCT = 8.0
#: Creep check: the last N hours vs the overall baseline, flagging at
#: this many percentage points of mean lift (RAM +15% over 3 days).
ANOMALY_CREEP_WINDOW_HOURS = 72.0
ANOMALY_CREEP_LIFT_PCT = 15.0
#: Per-metric cap on reported anomalies (most severe win) so a noisy
#: fortnight cannot flood the card.
ANOMALY_MAX_PER_METRIC = 3
#: Default day offset for :func:`_weekday_hour_bucket`: 1970-01-01 (unix
#: day 0) was a Thursday, i.e. weekday 3 with Monday == 0.
_EPOCH_WEEKDAY_OFFSET = 3


def _weekday_hour_bucket(ts: int,
                         utc_offset_seconds: int = 0) -> tuple[int, int]:
    """Pure ``(weekday, hour)`` key for a ts (Monday == 0), shifted by
    ``utc_offset_seconds`` so the caller pins the notion of "local time"
    (0 = UTC; the app can pass ``-int(time.timezone)``). No time-module
    calls: the bucketing is deterministic arithmetic, testable anywhere."""
    shift = int(ts) + int(utc_offset_seconds)
    days, rem = divmod(shift, 86400)
    return ((days + _EPOCH_WEEKDAY_OFFSET) % 7, rem // 3600)


def detect_anomalies(
    metric: str,
    subject: str,
    points: Sequence[tuple[int, float | None]],
    *,
    now_ts: int | None = None,
    baseline_hours: float = ANOMALY_BASELINE_HOURS,
    recent_hours: float = ANOMALY_RECENT_HOURS,
    window_hours: float = ANOMALY_WINDOW_HOURS,
    min_window_points: int = ANOMALY_MIN_WINDOW_POINTS,
    min_baseline_samples: int = 24,
    min_bucket_samples: int = ANOMALY_MIN_BUCKET_SAMPLES,
    z_floor: float = ANOMALY_Z_FLOOR,
    min_lift_pct: float = ANOMALY_MIN_LIFT_PCT,
    creep_window_hours: float = ANOMALY_CREEP_WINDOW_HOURS,
    creep_lift_pct: float = ANOMALY_CREEP_LIFT_PCT,
    max_anomalies: int = ANOMALY_MAX_PER_METRIC,
    utc_offset_seconds: int = 0,
) -> AnomalySet:
    """Flag statistically unusual recent windows in one metric's history.

    Layout, all relative to ``now_ts`` (default: the series' own last
    sample -- history that stopped before ``now - recent_hours`` is
    stale and yields ``insufficient_data``, honest about being unable to
    judge the present): a *baseline* region of ``baseline_hours``
    ending where the *recent* region of ``recent_hours`` begins. Each
    recent point is scored against a seasonal baseline -- the mean and
    spread of the *same weekday/hour bucket* when that bucket holds at
    least ``min_bucket_samples`` baseline points (nightly backups,
    weekday work rhythms stop being "anomalies") -- else against the
    overall baseline. Spreads are floored at 1.0 pct point: a perfectly
    flat baseline gets an absolute-lift judgement, never a 0-division.

    A sliding ``window_hours`` window with at least
    ``min_window_points`` points is a ``spike`` when at least
    ANOMALY_WINDOW_FLAG_FRACTION of its points individually clear
    ``z_floor`` sigmas AND ``min_lift_pct`` points above their baseline;
    adjacent elevated windows merge into one span so a 20-hour episode
    is one anomaly with one peak, not four overlapping ones. Separately,
    the last ``creep_window_hours`` form a ``creep`` anomaly when their
    mean sits at least ``creep_lift_pct`` points above the overall
    baseline.

    Only high-usage anomalies exist: for a health engine, unusually LOW
    cpu/ram/disk is good news, not a finding. Deterministic and pure;
    the returned :data:`AnomalySet` is capped at ``max_anomalies``
    (creep first, then spikes by z). Never raises: any unusable history
    is ``insufficient_data``.
    """
    def _bare() -> AnomalySet:
        return AnomalySet(metric, subject, "insufficient_data", [])

    def _span_to_anomaly(span) -> Anomaly:
        """One merged span -> its Anomaly (peak evidence kept)."""
        peak = max(span, key=lambda p: (p[1], -p[0]))
        return Anomaly(
            metric, subject, "spike", span[0][0], span[-1][0], peak[0],
            peak[1], statistics.fmean(m for _, _, m, _, _ in span),
            max(z for _, _, _, z, _ in span), peak[1] - peak[2],
            "seasonal" if all(s for _, _, _, _, s in span) else "overall")

    kept = _clean_series(points)
    if not kept:
        return _bare()
    try:
        ref = int(now_ts) if now_ts is not None else kept[-1][0]
    except (TypeError, ValueError, OverflowError):
        return _bare()
    recent_start = ref - recent_hours * 3600.0
    baseline_start = ref - baseline_hours * 3600.0
    baseline = [(t, v) for t, v in kept if baseline_start <= t < recent_start]
    recent = [(t, v) for t, v in kept if recent_start <= t <= ref]
    if len(baseline) < min_baseline_samples or len(recent) < min_window_points:
        return _bare()

    base_values = [v for _, v in baseline]
    base_mean = statistics.fmean(base_values)
    base_std = (max(statistics.pstdev(base_values), 1.0)
                if len(base_values) >= 2 else 1.0)

    by_bucket: dict[tuple[int, int], list[float]] = {}
    for t, v in baseline:
        by_bucket.setdefault(_weekday_hour_bucket(t, utc_offset_seconds),
                             []).append(v)
    bucket_stats = {bucket: (statistics.fmean(vs),
                             max(statistics.pstdev(vs), 1.0))
                    for bucket, vs in by_bucket.items()
                    if len(vs) >= min_bucket_samples}

    # Per-point scoring: (ts, value, baseline mean used, z, seasonal?).
    scored = []
    for t, v in recent:
        stats = bucket_stats.get(_weekday_hour_bucket(t, utc_offset_seconds))
        seasonal = stats is not None
        mean, std = stats if seasonal else (base_mean, base_std)
        scored.append((t, v, mean, (v - mean) / std, seasonal))

    def _flagged(z: float, value: float, mean: float) -> bool:
        return z >= z_floor and (value - mean) >= min_lift_pct

    # Consecutive non-overlapping windows anchored at the recent region's
    # start; elevated ones merge into spans.
    anomalies: list[Anomaly] = []
    span_points: list[tuple[int, float, float, float, bool]] = []
    window_points: list[tuple[int, float, float, float, bool]] = []
    window_end = scored[0][0] + window_hours * 3600.0

    def _flush_window() -> None:
        """Judge one full window; merge it into the span when elevated."""
        nonlocal span_points, window_points
        if len(window_points) >= min_window_points:
            flagged = sum(1 for p in window_points
                          if _flagged(p[3], p[1], p[2]))
            if flagged >= math.ceil(ANOMALY_WINDOW_FLAG_FRACTION
                                    * len(window_points)):
                span_points.extend(window_points)
                window_points = []
                return
        if span_points:
            anomalies.append(_span_to_anomaly(span_points))
            span_points = []
        window_points = []

    for t, v, mean, z, seasonal in scored:
        if t >= window_end:
            _flush_window()
            while t >= window_end:
                window_end += window_hours * 3600.0
        window_points.append((t, v, mean, z, seasonal))
    _flush_window()
    if span_points:
        anomalies.append(_span_to_anomaly(span_points))

    # Creep: the recent tail vs the OVERALL baseline (a shift too slow
    # to break any seasonal window is exactly what the seasonal check
    # washes out -- see the Anomaly docstring).
    creep_start = ref - creep_window_hours * 3600.0
    creep_points = [(t, v) for t, v in recent if t >= creep_start]
    if len(creep_points) >= min_window_points:
        lift = statistics.fmean(v for _, v in creep_points) - base_mean
        if lift >= creep_lift_pct:
            peak_ts, peak_value = max(creep_points,
                                      key=lambda p: (p[1], -p[0]))
            anomalies.append(Anomaly(
                metric, subject, "creep", creep_points[0][0],
                creep_points[-1][0], peak_ts, peak_value, base_mean,
                lift / base_std, lift, "overall"))

    anomalies.sort(key=lambda a: (0 if a.kind == "creep" else 1,
                                  -(a.z if a.z is not None else 0.0)))
    return AnomalySet(metric, subject, "ok", anomalies[:max_anomalies])


def detect_all_anomalies(
    db_path: str | Path | None = None,
    *,
    now_ts: int | None = None,
    hours: float = 720.0,
    **kwargs: Any,
) -> list[AnomalySet]:
    """Run :func:`detect_anomalies` over every series in the metrics db.

    CPU, RAM and each recorded mount, on the hourly-aggregated series
    (30 days -> 720 buckets per metric). ``now_ts``/``hours`` forward to
    the db reads and anchor the recent windows; ``kwargs`` forward to
    :func:`detect_anomalies`. Failure policy matches :func:`predict_all`:
    a missing/unopenable module or db yields ``[]``; one series that
    fails degrades to that metric's ``insufficient_data`` set instead of
    dropping it.
    """
    try:
        from . import metrics_db  # noqa: PLC0415 -- deliberate lazy import
    except Exception as exc:  # the optional sibling may simply not exist
        log.debug("metrics_db unavailable; no anomaly sets: %s", exc)
        return []
    try:
        path = db_path if db_path is not None else metrics_db.default_db_path()
        rows = metrics_db.cpu_ram_series_hourly(path, hours=hours,
                                                now_ts=now_ts)
        mounts = metrics_db.disk_mounts(path)
    except Exception as exc:
        log.debug("metrics_db anomaly reads failed: %s", exc)
        return []

    def _set(m: str, subj: str, series: Sequence) -> AnomalySet:
        try:
            return detect_anomalies(
                m, subj, [(int(r[0]), r[1]) for r in series if len(r) >= 2],
                now_ts=now_ts, **kwargs)
        except Exception as exc:  # noqa: BLE001 - one series must not kill all
            log.debug("%s/%s anomaly detection failed: %s", m, subj, exc)
            return AnomalySet(m, subj, "insufficient_data", [])

    sets = [
        _set("cpu", "", [(r[0], r[1]) for r in rows]),
        _set("ram", "", [(r[0], r[2]) for r in rows]),
    ]
    for mount in mounts or []:
        try:
            series = metrics_db.disk_series_hourly(path, mount, hours=hours,
                                                   now_ts=now_ts)
        except Exception as exc:  # noqa: BLE001
            log.debug("%s: hourly disk series failed: %s", mount, exc)
            series = []
        sets.append(_set("disk", mount, series))
    return sets


# ---------------------------------------------------------------------------
# Health-score forecast + integration plumbing
# ---------------------------------------------------------------------------


def forecast_health(
    components: dict[str, list[MetricPrediction]],
    horizons: Sequence[int],
    *,
    weights: dict[str, float] | None = None,
) -> list[HealthForecast]:
    """Combine component level-forecasts into health-score trajectories.

    ``components`` maps a weight key ("cpu", "ram", "disk") to ONE
    :class:`MetricPrediction` per horizon, same order as ``horizons``.
    For each horizon the usable components (any status that populated
    ``projected_pct``: ``ok``, ``no_trend``, ``shrinking``) contribute
    headroom ``100 - projected_pct``; the component weights renormalise
    over whoever actually contributed -- ``system_health_score``'s exact
    policy of leaving unavailable sensors out instead of pretending them
    unhealthy. The disk component is expected to already be the worst
    mount (see :func:`_metric_predictions`). The ``current_score`` is
    the same weighted headroom using ``current_pct``, so the drop the
    sentence quotes is fitted-level to fitted-level, not fitted to raw.

    With no usable component at a horizon the verdict is honest:
    ``insufficient_data`` with a None score. Never raises.
    """
    w = dict(weights) if weights is not None else dict(HEALTH_WEIGHTS)
    out: list[HealthForecast] = []
    for i, horizon in enumerate(horizons):
        try:
            key = int(horizon)
        except (TypeError, ValueError, OverflowError):
            continue
        per: dict[str, tuple[float, float]] = {}  # key -> (usage, rate)
        currents: dict[str, float] = {}
        for comp, preds in components.items():
            if comp not in w or not isinstance(preds, (list, tuple)):
                continue
            pred = preds[i] if i < len(preds) else None
            if pred is None or getattr(pred, "projected_pct", None) is None:
                continue
            per[comp] = (float(pred.projected_pct),
                         float(pred.rate_pct_per_day or 0.0))
            if getattr(pred, "current_pct", None) is not None:
                currents[comp] = float(pred.current_pct)
        if not per:
            out.append(HealthForecast(key, None, None,
                                      "insufficient_data", ()))
            continue
        total_w = sum(w[c] for c in per)
        score = max(0.0, min(100.0,
                             sum((100.0 - usage) * w[c]
                                 for c, (usage, _rate) in per.items())
                             / total_w))
        current = None
        if currents:
            cur_w = sum(w[c] for c in currents)
            current = max(0.0, min(100.0,
                                   sum((100.0 - u) * w[c] for c, u
                                       in currents.items()) / cur_w))
        drivers = []
        for comp in ("cpu", "ram", "disk"):
            if comp not in per:
                continue
            usage, rate = per[comp]
            if rate <= 0.0:
                continue
            if comp == "disk":
                pred = components["disk"][i]
                drivers.append(f"Disk {pred.subject or '?'} is filling")
            else:
                drivers.append(f"{comp.upper()} pressure is trending up")
        out.append(HealthForecast(key, current, score, "ok",
                                  tuple(drivers)))
    return out


def health_forecast_sentence(hf: HealthForecast) -> str:
    """Plain-English verdict for one :class:`HealthForecast` (or "").

    ``"RAM pressure is trending up; health may drop to ~72 within 2
    weeks."`` is the shape: drivers first, then the outcome. A drop of
    at least HEALTH_DROP_ALERT_PCT points quotes "may drop to ~N"; less
    is noise and reads "should hold near ~N". ``insufficient_data``
    renders as "" (the card shows nothing rather than inventing words).
    Never raises; a garbage object renders "".
    """
    try:
        if str(getattr(hf, "status", "")) != "ok":
            return ""
        score = hf.score
        current = hf.current_score
        if score is None:
            return ""
        days = int(getattr(hf, "horizon_days", 0) or 0)
        if days <= 7:
            phrase = "within a week"
        elif days >= 25:
            phrase = "within a month"
        else:
            weeks = max(2, int(round(days / 7.0)))
            phrase = f"within {weeks} weeks"
        parts = [str(d) for d in (getattr(hf, "drivers", None) or ())]
        dropping = (current is not None
                    and current - float(score) >= HEALTH_DROP_ALERT_PCT)
        if dropping:
            parts.append(f"health may drop to ~{round(float(score))} "
                         f"{phrase}")
        elif not parts:
            parts.append(f"Health should hold near ~{round(float(score))} "
                         f"{phrase}.")
            return " ".join(parts)
        else:
            parts.append(f"health should hold near ~{round(float(score))} "
                         f"{phrase}")
        sentence = "; ".join(parts)
        return sentence[0].upper() + sentence[1:] + "."
    except Exception:  # noqa: BLE001 - one odd object must not kill a card
        return ""


def _metric_predictions(
    db_path: Path,
    *,
    now_ts: int | None,
    horizons: Sequence[int],
    hours: float,
    **metric_kwargs: Any,
) -> tuple[dict[str, list[MetricPrediction]], list[MetricPrediction]]:
    """All per-component/per-horizon MetricPredictions from one db.

    Returns ``(components, all_predictions)``: ``components`` is the
    ``forecast_health`` input (cpu, ram and the disk component -- the
    mount with the highest *current* fitted usage, since the fullest
    disk now is the likeliest score-dragger, tracked across all
    horizons); ``all_predictions`` also carries every other mount's
    per-horizon rows for the card's per-drive section. Never raises:
    any read/fit failure degrades that series to ``insufficient_data``
    rows.
    """
    from . import metrics_db  # noqa: PLC0415 -- callers already imported it

    rows = metrics_db.cpu_ram_series_hourly(db_path, hours=hours,
                                            now_ts=now_ts)
    cpu_pts = [(int(r[0]), r[1]) for r in rows]
    ram_pts = [(int(r[0]), r[2]) for r in rows]
    mounts = metrics_db.disk_mounts(db_path)
    disk_pts: dict[str, list[tuple[int, float | None]]] = {}
    for mount in mounts or []:
        series = metrics_db.disk_series_hourly(db_path, mount, hours=hours,
                                               now_ts=now_ts)
        disk_pts[mount] = [(int(r[0]), r[1]) for r in series]

    def _per_horizon(m: str, subj: str, pts) -> list[MetricPrediction]:
        """One series forecast at every horizon (insufficient included)."""
        return [forecast_metric(m, subj, pts, h, **metric_kwargs)
                for h in horizons]

    cpu = _per_horizon("cpu", "", cpu_pts)
    ram = _per_horizon("ram", "", ram_pts)
    disk_all = [(mount, _per_horizon("disk", mount, pts))
                for mount, pts in disk_pts.items()]

    # The disk component: fullest current fitted usage wins and is kept
    # consistent across horizons (see the docstring).
    disk_component = []
    if disk_all:
        def _current(entry):
            first = entry[1][0] if entry[1] else None
            cur = getattr(first, "current_pct", None)
            return cur if cur is not None else -1.0
        best_mount, best_preds = max(disk_all, key=_current)
        disk_component = best_preds

    components = {"cpu": cpu, "ram": ram}
    if disk_component:
        components["disk"] = disk_component
    all_predictions = [*cpu, *ram]
    for _mount, preds in disk_all:
        all_predictions.extend(preds)
    return components, all_predictions


def predict_health(
    db_path: str | Path | None = None,
    *,
    now_ts: int | None = None,
    horizons: Sequence[int] = (7, 14, 30),
    hours: float = 720.0,
    **metric_kwargs: Any,
) -> list[HealthForecast]:
    """Forecast the health score at every horizon; never raises.

    Reads the hourly-aggregated history (default window ``hours`` = 30
    days), forecasts each component per horizon and combines them via
    :func:`forecast_health`. ``db_path=None`` resolves to
    ``metrics_db.default_db_path()``. On any failure -- missing module,
    unreadable db -- returns one ``insufficient_data`` forecast per
    requested horizon (an empty list only if ``horizons`` itself is
    empty), so the card always gets an honest verdict per horizon.
    """
    def _bare() -> list[HealthForecast]:
        return [HealthForecast(int(h), None, None, "insufficient_data", ())
                for h in horizons]

    try:
        from . import metrics_db  # noqa: PLC0415 -- deliberate lazy import
    except Exception as exc:
        log.debug("metrics_db unavailable; no health forecast: %s", exc)
        return _bare()
    try:
        path = db_path if db_path is not None else metrics_db.default_db_path()
        components, _all_preds = _metric_predictions(
            path, now_ts=now_ts, horizons=horizons, hours=hours,
            **metric_kwargs)
        return forecast_health(components, horizons)
    except Exception as exc:
        log.debug("predict_health failed: %s", exc)
        return _bare()


def predict_health_payload(
    db_path: str | Path | None = None,
    *,
    now_ts: int | None = None,
    horizons: Sequence[int] = (7, 14, 30),
    hours: float = 720.0,
    **forecast_kwargs: Any,
) -> dict:
    """Build the whole Predictive Health card's data payload; never raises.

    The single off-thread call the GUI's job makes. Shape (every section
    independently degrades to empty -- a failed section never poisons
    the others):

        {
          "generated_ts": int,        # the pinned clock (now_ts, else time.time())
          "status": "ok" | "degraded",
          "reason": str,              # "" normally; prose when degraded
          "disk": list[Prediction],   # disk-full ETAs (hourly series, same gates)
          "metrics": list[MetricPrediction],  # cpu/ram/every mount x horizons
          "anomaly_sets": list[AnomalySet],   # per-metric verdicts
          "health": list[HealthForecast],     # one per horizon
        }

    ``forecast_kwargs`` pass through to :func:`forecast_disk_full` (the
    disk section) and, minus its own defaults, the metric gates. Runs
    entirely on SQL-aggregated hourly series, so 30 days of 60 s
    sampling costs ~720 points per metric, well inside the card's
    off-thread budget.
    """
    try:
        clock = int(now_ts) if now_ts is not None else int(time.time())
    except (TypeError, ValueError, OverflowError):
        clock = int(time.time())
    payload: dict = {
        "generated_ts": clock,
        "status": "ok",
        "reason": "",
        "disk": [],
        "metrics": [],
        "anomaly_sets": [],
        "health": [],
    }
    try:
        from . import metrics_db  # noqa: PLC0415 -- deliberate lazy import
    except Exception as exc:
        payload["status"] = "degraded"
        payload["reason"] = f"metrics module unavailable: {exc}"
        return payload

    try:
        path = db_path if db_path is not None else metrics_db.default_db_path()
    except Exception as exc:
        payload["status"] = "degraded"
        payload["reason"] = f"metrics path unavailable: {exc}"
        return payload

    # Disk-full ETAs on the hourly series (same function and gates as
    # the classic Trends card -- 60x less data than the raw 60 s rows).
    try:
        mounts = metrics_db.disk_mounts(path)
        for mount in mounts or []:
            try:
                series = metrics_db.disk_series_hourly(path, mount,
                                                       hours=hours,
                                                       now_ts=now_ts)
                payload["disk"].append(
                    forecast_disk_full(
                        mount, [(int(r[0]), r[1]) for r in series],
                        **forecast_kwargs))
            except Exception as exc:  # noqa: BLE001 - one mount degrades alone
                log.debug("%s: disk forecast failed: %s", mount, exc)
                payload["disk"].append(Prediction(mount, None, None, None,
                                                  None, "insufficient_data"))
    except Exception as exc:  # noqa: BLE001
        log.debug("payload disk section failed: %s", exc)

    try:
        _components, all_preds = _metric_predictions(
            path, now_ts=now_ts, horizons=horizons, hours=hours)
        payload["metrics"] = all_preds
    except Exception as exc:  # noqa: BLE001
        log.debug("payload metrics section failed: %s", exc)

    try:
        payload["anomaly_sets"] = detect_all_anomalies(
            path, now_ts=now_ts, hours=hours)
    except Exception as exc:  # noqa: BLE001
        log.debug("payload anomaly section failed: %s", exc)

    try:
        payload["health"] = predict_health(
            path, now_ts=now_ts, horizons=horizons, hours=hours)
    except Exception as exc:  # noqa: BLE001
        log.debug("payload health section failed: %s", exc)

    return payload
