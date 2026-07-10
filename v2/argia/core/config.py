"""Config loader — Stage 7.3.

Adds two optional Plants columns: ``pr_baseline`` (clean-state PR for
soiling math) and ``tariff_mxn_per_kwh`` (energy price for dollar
projections).

Stage 7.3 also adds **load-time validation warnings** when kwp_dc looks
suspicious — the live data revealed several plants where the field is
under-set, which produces nonsensical PR values downstream. We don't
refuse to load — we WARN, because fixing the sheet is independent of
the pipeline being functional.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from argia.core.normalize import normalize_sn, normalize_text, safe_float
from argia.core.sheets import SheetsClient

LOG = logging.getLogger("argia.core.config")


@dataclass(frozen=True)
class PlantConfig:
    plant_key: str
    customer: str
    brand: str
    site_id: str

    kwp_dc: float
    kwp_ac: float

    lat: Optional[float]
    lon: Optional[float]

    expected_factor: float
    pr_target: float
    installation_date: str

    secret_api_name: str
    secret_user_name: str
    secret_pass_name: str

    weather_plant_id: str
    datalogger_sn: str
    datalogger_addr: int

    active: bool

    # Stage 7.1
    module_count: Optional[int] = None
    module_wp: Optional[float] = None
    string_count: Optional[int] = None
    tilt_deg: Optional[float] = None
    azimuth_deg: Optional[float] = None
    system_losses_pct: Optional[float] = None
    commissioning_date: str = ""
    notes: str = ""

    # Stage 7.3
    pr_baseline: Optional[float] = None
    """Clean-state PR baseline for soiling comparison. Set this manually
    after observing the plant for ~30 days post-cleaning. Stays None
    until set; soiling check skips plants without a baseline."""

    tariff_mxn_per_kwh: Optional[float] = None
    gamma_pmax: Optional[float] = None  # Pmax temp coeff /degC; blank -> pipeline default
    """Energy price for the customer at this site, MXN/kWh. Used to
    convert PR loss into pesos for cost-benefit decisions."""

    # v61 — finance layer
    om_cost_monthly_mxn: Optional[float] = None
    """Average monthly O&M cost for this plant, MXN (manual entry).
    Feeds the investor report's opex line; prorated for partial
    ranges. None/blank = not provided, opex line shows 0 for the
    plant with a footnote."""

    # v74 — visibility flags. Two axes, deliberately separate:
    # `active` is the MACHINE axis (telemetry/KPI/alerts) and is the
    # only flag that can stop data capture. The columns below are the
    # REPORT axis: they control where a plant appears, never what is
    # collected — a wrong value here can hide a plant but can never
    # silently lose data. `portfolio` is a pure label (grouping,
    # badges, future per-portfolio reports); it controls nothing.
    portfolio: str = "PPA"
    show_dashboard: bool = True
    show_daily_report: bool = True
    show_financial: bool = True

    # v77 — client delivery. Non-blank routes this plant into a
    # per-client daily report mailed via the notifier channel of the
    # same name (Recipients rows per channel). Blank = internal only.
    # Independent of show_daily_report, which governs the INTERNAL
    # report: a CAPEX plant is typically show_daily_report=FALSE +
    # client_channel=<client>.
    client_channel: str = ""


@dataclass(frozen=True)
class InverterConfig:
    plant_key: str
    inverter_sn: str
    inverter_label: str
    rated_kw: float
    active: bool

    # Stage 7.1
    mppt_count: Optional[int] = None
    strings_per_mppt: Optional[int] = None
    rated_kw_dc: Optional[float] = None


@dataclass(frozen=True)
class Portfolio:
    plants: Dict[str, PlantConfig] = field(default_factory=dict)
    inverters_by_plant: Dict[str, List[InverterConfig]] = field(default_factory=dict)

    def active_plants(self) -> List[PlantConfig]:
        """MACHINE axis: telemetry, KPI, alerts. Report flags never
        filter here — hiding a plant must not stop its data."""
        return [p for p in self.plants.values() if p.active]

    def dashboard_plants(self) -> List[PlantConfig]:
        return [p for p in self.active_plants() if p.show_dashboard]

    def daily_report_plants(self) -> List[PlantConfig]:
        return [p for p in self.active_plants() if p.show_daily_report]

    def financial_plants(self) -> List[PlantConfig]:
        return [p for p in self.active_plants() if p.show_financial]

    def client_channels(self) -> List[str]:
        """Distinct non-blank client channels among active plants."""
        return sorted({p.client_channel for p in self.active_plants()
                       if p.client_channel})

    def for_client_channel(self, channel: str) -> "Portfolio":
        """A portfolio VIEW for one client: only that channel's active
        plants, with show_daily_report forced True — the internal flag
        hides a plant from ARGIA's report, never from the client's own
        (the whole point of the channel). Alerts scoping then covers
        exactly the client's plants for free."""
        from dataclasses import replace
        view = Portfolio()
        for pk, p in self.plants.items():
            if p.active and p.client_channel == channel:
                view.plants[pk] = replace(p, show_daily_report=True)
                view.inverters_by_plant[pk] = list(
                    self.inverters_by_plant.get(pk, []))
        return view

    def inverters_for(self, plant_key: str) -> List[InverterConfig]:
        return [i for i in self.inverters_by_plant.get(plant_key, []) if i.active]

    def plants_by_brand(self, brand: str) -> List[PlantConfig]:
        target = brand.upper()
        return [p for p in self.active_plants() if p.brand == target]


# ----------------- expected sheet headers -----------------

PLANTS_HEADER_V70 = [
    "plant_key", "customer", "brand", "site_id",
    "kwp_dc", "kwp_ac", "lat", "lon",
    "expected_factor", "pr_target", "installation_date",
    "secret_api_name", "secret_user_name", "secret_pass_name",
    "weather_plant_id", "datalogger_sn", "datalogger_addr",
    "active",
]

PLANTS_HEADER_V71 = PLANTS_HEADER_V70 + [
    "module_count", "module_wp", "string_count",
    "tilt_deg", "azimuth_deg",
    "system_losses_pct", "commissioning_date", "notes",
]

# Stage 7.3 — 2 new columns at end
PLANTS_HEADER = PLANTS_HEADER_V71 + [
    "pr_baseline", "tariff_mxn_per_kwh",
]

INVERTERS_HEADER_V70 = [
    "plant_key", "inverter_sn", "inverter_label", "rated_kw", "active",
]

INVERTERS_HEADER = INVERTERS_HEADER_V70 + [
    "mppt_count", "strings_per_mppt", "rated_kw_dc",
]


def _truthy(value) -> bool:
    s = normalize_text(value).lower()
    return s in ("true", "yes", "y", "1", "x")


KNOWN_PORTFOLIOS = ("PPA", "CAPEX", "PROLOGIS")


def _flag_default_true(value, column: str, plant_key: str) -> bool:
    """Report-visibility flags: BLANK means TRUE (migration is a
    behavioral no-op), an explicit falsy hides, and an unrecognized
    value warns and shows — erring on visibility, never on silent
    hiding."""
    s = normalize_text(value).lower()
    if s == "":
        return True
    if s in ("false", "no", "n", "0"):
        return False
    if s in ("true", "yes", "y", "1", "x"):
        return True
    LOG.warning("Plants.%s for %s: unrecognized value %r — treating as "
                "TRUE (visible)", column, plant_key, value)
    return True


def _client_channel(value, plant_key: str) -> str:
    """Notifier channel token: lowercased, spaces collapsed to '_'.
    Channels live in Report_Outbox/Recipients as simple lowercase
    tokens ('reporting', 'shareholders'), so client channels follow
    the same convention."""
    s = normalize_text(value).lower().replace(" ", "_")
    return s


def _portfolio_label(value, plant_key: str) -> str:
    """Pure label: blank -> PPA; unknown labels are KEPT (a new deal
    category must never disable anything) with a warning."""
    s = normalize_text(value).upper()
    if s == "":
        return "PPA"
    if s not in KNOWN_PORTFOLIOS:
        LOG.warning("Plants.portfolio for %s: unknown label %r (kept — "
                    "known: %s)", plant_key, s,
                    "/".join(KNOWN_PORTFOLIOS))
    return s


def _optional_int(value) -> Optional[int]:
    f = safe_float(value)
    if f is None:
        return None
    try:
        return int(f)
    except (ValueError, TypeError):
        return None


def _optional_float(value) -> Optional[float]:
    return safe_float(value)


# ---------- sanity warnings ----------


def _warn_plant_sanity(plant: PlantConfig, log: logging.Logger) -> None:
    """Best-effort warnings at load time. Pure logging, no raises."""
    if plant.kwp_dc <= 0 and plant.active:
        log.warning(
            "[%s] kwp_dc is 0 or missing on Plants tab — PR will be None",
            plant.plant_key,
        )
    if plant.kwp_ac <= 0 and plant.active:
        log.warning(
            "[%s] kwp_ac is 0 or missing on Plants tab — capacity factor will be None",
            plant.plant_key,
        )
    if plant.kwp_dc > 0 and plant.kwp_ac > 0:
        ratio = plant.kwp_dc / plant.kwp_ac
        if ratio < 1.0:
            log.warning(
                "[%s] kwp_dc (%.1f) < kwp_ac (%.1f). DC nameplate is usually "
                "1.1-1.3× AC nameplate — kwp_dc likely set to single-inverter "
                "rating instead of plant total. Expect PR > 1.0.",
                plant.plant_key, plant.kwp_dc, plant.kwp_ac,
            )
    if (
        plant.module_count is not None and plant.module_wp is not None
        and plant.module_count > 0 and plant.module_wp > 0
        and plant.kwp_dc > 0
    ):
        derived_kwp = (plant.module_count * plant.module_wp) / 1000.0
        if abs(derived_kwp - plant.kwp_dc) / plant.kwp_dc > 0.15:
            log.warning(
                "[%s] kwp_dc=%.1f disagrees with module_count×module_wp=%.1f "
                "by >15%%. One of them is wrong.",
                plant.plant_key, plant.kwp_dc, derived_kwp,
            )


def load_portfolio(sheets: SheetsClient) -> Portfolio:
    """Read Plants + Inverters tabs and return a Portfolio object.

    Stage 7.3: AB column range to fit the 2 new Plants fields. Old
    sheets with fewer columns still load (missing cells → None)."""
    plants_raw = sheets.read_table("Plants", "A1:AZ")  # AZ: headroom — pr_baseline sits at AJ, past the old AB cutoff
    inverters_raw = sheets.read_table("Inverters", "A1:Z")

    plants: Dict[str, PlantConfig] = {}
    for row in plants_raw:
        plant_key = normalize_text(row.get("plant_key"))
        if not plant_key:
            continue

        try:
            cfg = PlantConfig(
                plant_key=plant_key,
                customer=normalize_text(row.get("customer")),
                brand=normalize_text(row.get("brand")).upper(),
                site_id=normalize_text(row.get("site_id")),
                kwp_dc=safe_float(row.get("kwp_dc"), 0.0) or 0.0,
                kwp_ac=safe_float(row.get("kwp_ac"), 0.0) or 0.0,
                lat=safe_float(row.get("lat")),
                lon=safe_float(row.get("lon")),
                expected_factor=safe_float(row.get("expected_factor"), 0.0) or 0.0,
                pr_target=safe_float(row.get("pr_target"), 0.0) or 0.0,
                installation_date=normalize_text(row.get("installation_date")),
                secret_api_name=normalize_text(row.get("secret_api_name")),
                secret_user_name=normalize_text(row.get("secret_user_name")),
                secret_pass_name=normalize_text(row.get("secret_pass_name")),
                weather_plant_id=normalize_text(row.get("weather_plant_id")),
                datalogger_sn=normalize_text(row.get("datalogger_sn")),
                datalogger_addr=int(safe_float(row.get("datalogger_addr"), 0) or 0),
                active=_truthy(row.get("active")),
                # Stage 7.1
                module_count=_optional_int(row.get("module_count")),
                module_wp=_optional_float(row.get("module_wp")),
                string_count=_optional_int(row.get("string_count")),
                tilt_deg=_optional_float(row.get("tilt_deg")),
                azimuth_deg=_optional_float(row.get("azimuth_deg")),
                system_losses_pct=_optional_float(row.get("system_losses_pct")),
                commissioning_date=normalize_text(row.get("commissioning_date")),
                notes=normalize_text(row.get("notes")),
                # Stage 7.3
                pr_baseline=_optional_float(row.get("pr_baseline")),
                tariff_mxn_per_kwh=_optional_float(row.get("tariff_mxn_per_kwh")),
                gamma_pmax=_optional_float(row.get("gamma_pmax")),
                # v61
                om_cost_monthly_mxn=_optional_float(
                    row.get("om_cost_monthly_mxn")),
                # v74
                portfolio=_portfolio_label(row.get("portfolio"),
                                           plant_key),
                show_dashboard=_flag_default_true(
                    row.get("show_dashboard"), "show_dashboard",
                    plant_key),
                show_daily_report=_flag_default_true(
                    row.get("show_daily_report"), "show_daily_report",
                    plant_key),
                show_financial=_flag_default_true(
                    row.get("show_financial"), "show_financial",
                    plant_key),
                # v77
                client_channel=_client_channel(
                    row.get("client_channel"), plant_key),
            )
        except (ValueError, TypeError) as e:
            LOG.warning("Skipping malformed Plants row %s: %s", plant_key, e)
            continue

        if plant_key in plants:
            LOG.warning(
                "Duplicate plant_key '%s' in Plants tab — keeping first", plant_key,
            )
            continue
        plants[plant_key] = cfg
        _warn_plant_sanity(cfg, LOG)

    inverters_by_plant: Dict[str, List[InverterConfig]] = {}
    for row in inverters_raw:
        plant_key = normalize_text(row.get("plant_key"))
        sn = normalize_sn(row.get("inverter_sn"))
        if not plant_key or not sn:
            continue

        if plant_key not in plants:
            LOG.warning(
                "Inverters row references unknown plant_key '%s' — skipping",
                plant_key,
            )
            continue

        inv = InverterConfig(
            plant_key=plant_key,
            inverter_sn=sn,
            inverter_label=normalize_text(row.get("inverter_label")) or sn,
            rated_kw=safe_float(row.get("rated_kw"), 0.0) or 0.0,
            active=_truthy(row.get("active")),
            mppt_count=_optional_int(row.get("mppt_count")),
            strings_per_mppt=_optional_int(row.get("strings_per_mppt")),
            rated_kw_dc=_optional_float(row.get("rated_kw_dc")),
        )
        if inv.rated_kw <= 0 and inv.active:
            LOG.warning(
                "[%s/%s] rated_kw is 0 on Inverters tab — peer ranking "
                "will not work for this inverter",
                plant_key, sn,
            )
        inverters_by_plant.setdefault(plant_key, []).append(inv)

    LOG.info(
        "Loaded portfolio: %d plants (%d active), %d inverters",
        len(plants),
        sum(1 for p in plants.values() if p.active),
        sum(len(v) for v in inverters_by_plant.values()),
    )
    return Portfolio(plants=plants, inverters_by_plant=inverters_by_plant)
