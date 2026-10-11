import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pandas as pd
import pytest
from moto import mock_aws

import scheduler.loop_handler as loop
from decision_engine.kill_switch import DEFAULT_PARAMETER, ENGAGED, RELEASED
from decision_engine.models import Aggressiveness, Forecast
from scheduler.loop_handler import (
    Clients,
    LoopConfig,
    LoopState,
    ReplaySource,
    append_records,
    parse_dry_run,
    run_cycle,
    run_replay,
    select_services,
)
from tests.fakes import StatefulLambda

ASG = "predictive-scaling-catalog-asg"
FUNCTION = "predictive-scaling-thumbnail-service"
BOTH_DUE = pd.Timestamp("2026-06-01 02:10")  # minute 10: catalog (every 5) and thumbnail (every 1)
THUMBNAIL_ONLY = pd.Timestamp("2026-06-01 02:11")


# --- test doubles ------------------------------------------------------------------------------


class FixedForecaster:
    """Always forecasts the same p90 level; remembers the newest data point it was shown."""

    def __init__(self, service: str, p90_level: float = 150.0) -> None:
        self.service, self.p90_level, self.seen_until = service, p90_level, None

    def forecast(self, series: pd.Series, origin: pd.Timestamp) -> Forecast:
        self.seen_until = series.index.max()
        n, level = 15, self.p90_level
        return Forecast(self.service, origin.to_pydatetime(), tuple([0.0] * n), tuple([level / 2] * n), tuple([level] * n))


class BrokenForecaster:
    def forecast(self, series, origin):
        raise RuntimeError("model blew up")


def make_source() -> ReplaySource:
    index = pd.date_range("2026-06-01", periods=400, freq="min")
    flat = pd.Series(10.0, index=index)
    return ReplaySource({"catalog_service": flat, "thumbnail_service": flat})


def forecasters(catalog: float = 150.0, thumbnail: float = 600.0) -> dict:
    return {"catalog_service": FixedForecaster("catalog_service", catalog), "thumbnail_service": FixedForecaster("thumbnail_service", thumbnail)}


# --- DRY_RUN parsing: only the literal word "false" turns it off ---------------------------------


@pytest.mark.parametrize("value", [None, "", "true", "TRUE", "1", "yes", "on", "flase", "0", "no", "off", " "])
def test_anything_but_the_word_false_stays_dry_run(value) -> None:
    assert parse_dry_run(value) is True


@pytest.mark.parametrize("value", ["false", "FALSE", " False "])
def test_only_false_turns_dry_run_off(value) -> None:
    assert parse_dry_run(value) is False


def test_the_default_configuration_is_dry_run() -> None:
    assert LoopConfig().dry_run is True
    assert LoopConfig.from_env({}).dry_run is True
    assert LoopConfig.from_env({"DRY_RUN": "false"}).dry_run is False


def test_config_reads_names_and_aggressiveness_from_the_environment() -> None:
    config = LoopConfig.from_env({"AGGRESSIVENESS": "latency_optimized", "CATALOG_ASG_NAME": "x-asg"})

    assert config.aggressiveness is Aggressiveness.LATENCY_OPTIMIZED and config.asg_name == "x-asg"


def test_a_bad_aggressiveness_is_a_loud_error() -> None:
    with pytest.raises(ValueError):
        LoopConfig.from_env({"AGGRESSIVENESS": "yolo"})


# --- offline mode: logs decisions, can never actuate --------------------------------------------


def test_offline_cycle_logs_decisions_and_actuates_nothing() -> None:
    records = run_cycle(BOTH_DUE, LoopConfig(), make_source(), forecasters(), None, LoopState())

    assert {r.service for r in records} == {"catalog_service", "thumbnail_service"}
    assert all(r.mode == "offline" and r.actuation_status == "offline" and r.kill_engaged is None for r in records)
    by_service = {r.service: r for r in records}
    assert by_service["catalog_service"].desired == 3  # ceil(150 / 50)
    assert by_service["thumbnail_service"].desired == 2  # ceil(600/60 * 0.1 * 1.5)


def test_real_actuation_without_aws_clients_is_refused_outright() -> None:
    with pytest.raises(ValueError, match="requires AWS clients"):
        run_cycle(BOTH_DUE, LoopConfig(dry_run=False), make_source(), forecasters(), None, LoopState())


def test_offline_state_chains_so_scale_down_is_gradual() -> None:
    config = LoopConfig(damping={})  # damping off: this isolates the policy's own one-step-down rule
    state, source, fc = LoopState(), make_source(), forecasters(catalog=300.0)
    first = run_cycle(BOTH_DUE, config, source, fc, None, state)
    fc["catalog_service"].p90_level = 0.0

    second = run_cycle(BOTH_DUE + pd.Timedelta(minutes=5), config, source, fc, None, state)

    catalog1 = next(r for r in first if r.service == "catalog_service")
    catalog2 = next(r for r in second if r.service == "catalog_service")
    assert catalog1.desired == 6 and catalog2.current == 6 and catalog2.desired == 5  # one step down only


# --- cadence ------------------------------------------------------------------------------------


def test_catalog_runs_every_five_minutes_and_thumbnail_every_minute() -> None:
    off_beat = run_cycle(THUMBNAIL_ONLY, LoopConfig(), make_source(), forecasters(), None, LoopState())

    assert [r.service for r in off_beat] == ["thumbnail_service"]


def test_replay_runs_the_requested_number_of_ticks() -> None:
    records = run_replay(pd.Timestamp("2026-06-01 02:10"), 10, LoopConfig(), make_source(), forecasters(), None)

    assert len([r for r in records if r.service == "thumbnail_service"]) == 10
    assert len([r for r in records if r.service == "catalog_service"]) == 2  # minutes :10 and :15


# --- no peeking at the future -------------------------------------------------------------------


def test_the_forecaster_only_sees_history_up_to_now() -> None:
    fc = forecasters()

    run_cycle(BOTH_DUE, LoopConfig(), make_source(), fc, None, LoopState())

    assert fc["catalog_service"].seen_until == BOTH_DUE
    assert fc["thumbnail_service"].seen_until == BOTH_DUE


# --- damping -----------------------------------------------------------------------------------------


def transitions(records, service: str) -> int:
    """How many times capacity switched between 'none' and 'some' (create / delete cycles)."""
    on = [r.desired > 0 for r in records if r.service == service]
    return sum(1 for a, b in zip(on, on[1:]) if a != b)


class FlippingForecaster:
    """Alternates between a high and a zero forecast every call: the worst case for flapping."""

    def __init__(self, service: str) -> None:
        self.service, self.calls = service, 0

    def forecast(self, series: pd.Series, origin: pd.Timestamp) -> Forecast:
        self.calls += 1
        level = 600.0 if self.calls % 2 else 0.0
        n = 15
        return Forecast(self.service, origin.to_pydatetime(), tuple([0.0] * n), tuple([level / 2] * n), tuple([level] * n))


def test_damping_is_on_by_default_and_can_be_turned_off() -> None:
    assert set(LoopConfig().damping) == {"catalog_service", "thumbnail_service"}
    assert LoopConfig(damping={}).damping == {}


def test_a_release_is_held_and_both_the_proposal_and_the_damped_value_are_logged() -> None:
    state, source, fc = LoopState(), make_source(), forecasters(catalog=300.0)
    run_cycle(BOTH_DUE, LoopConfig(), source, fc, None, state)
    fc["catalog_service"].p90_level = 0.0

    later = run_cycle(BOTH_DUE + pd.Timedelta(minutes=5), LoopConfig(), source, fc, None, state)

    catalog = by_service(later)["catalog_service"]
    assert catalog.proposed == 5 and catalog.desired == 6  # the policy wanted 5; damping held 6
    assert "holding" in catalog.damping and "damping:" in catalog.reason


def test_increases_are_never_delayed_by_damping() -> None:
    state, source, fc = LoopState(), make_source(), forecasters(catalog=100.0)
    run_cycle(BOTH_DUE, LoopConfig(), source, fc, None, state)
    fc["catalog_service"].p90_level = 400.0

    later = run_cycle(BOTH_DUE + pd.Timedelta(minutes=5), LoopConfig(), source, fc, None, state)

    catalog = by_service(later)["catalog_service"]
    assert catalog.desired == catalog.proposed == 8 and not catalog.damping.startswith("holding")


def test_damping_cuts_flapping_on_a_flickering_forecast() -> None:
    def run(config: LoopConfig):
        fc = {"catalog_service": FixedForecaster("catalog_service", 100.0), "thumbnail_service": FlippingForecaster("thumbnail_service")}
        return run_replay(pd.Timestamp("2026-06-01 02:00"), 120, config, make_source(), fc, None)

    undamped = transitions(run(LoopConfig(damping={})), "thumbnail_service")
    damped = transitions(run(LoopConfig()), "thumbnail_service")

    assert undamped >= 100  # flips every minute without damping
    assert damped <= undamped / 4


def test_the_damped_value_is_what_reaches_the_aws_actuator(aws) -> None:
    clients, _lam = aws
    config, state, source = LoopConfig(dry_run=False), LoopState(), make_source()
    fc = forecasters(catalog=300.0)
    run_cycle(BOTH_DUE, config, source, fc, clients, state)
    assert asg_desired(clients) == 6
    fc["catalog_service"].p90_level = 0.0

    later = run_cycle(BOTH_DUE + pd.Timedelta(minutes=5), config, source, fc, clients, state)

    catalog = by_service(later)["catalog_service"]
    assert catalog.proposed < catalog.desired == 6
    assert asg_desired(clients) == 6  # the real ASG was not scaled down yet


def test_damping_state_is_kept_per_service() -> None:
    state = LoopState()

    run_cycle(BOTH_DUE, LoopConfig(), make_source(), forecasters(), None, state)

    assert set(state.damping) == {"catalog_service", "thumbnail_service"}
    assert state.damping["catalog_service"] is not state.damping["thumbnail_service"]


def test_the_log_carries_the_damping_fields(tmp_path) -> None:
    log = tmp_path / "decisions.jsonl"

    run_replay(pd.Timestamp("2026-06-01 02:10"), 2, LoopConfig(), make_source(), forecasters(), None, log_path=log)

    row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert {"proposed", "damping", "desired"} <= set(row)


# --- AWS mode (moto) -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _fake_aws():
    patch = pytest.MonkeyPatch()
    patch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        session = boto3.Session(region_name="us-east-1")
        ec2 = session.client("ec2")
        ami = ec2.describe_images()["Images"][0]["ImageId"]
        ec2.create_launch_template(LaunchTemplateName="test-lt", LaunchTemplateData={"ImageId": ami})
        autoscaling = session.client("autoscaling")
        autoscaling.create_auto_scaling_group(
            AutoScalingGroupName=ASG, MinSize=1, MaxSize=10, DesiredCapacity=1,
            LaunchTemplate={"LaunchTemplateName": "test-lt", "Version": "$Latest"}, AvailabilityZones=["us-east-1a"],
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            z.writestr("h.py", "def handler(e, c):\n    return 1\n")
        role = session.client("iam").create_role(RoleName="test-role", AssumeRolePolicyDocument="{}")["Role"]["Arn"]
        lam = session.client("lambda")
        lam.create_function(FunctionName=FUNCTION, Runtime="python3.11", Role=role, Handler="h.handler",
                            Code={"ZipFile": buffer.getvalue()}, Publish=True)
        lam.create_alias(FunctionName=FUNCTION, Name="live", FunctionVersion="1")
        yield {"autoscaling": autoscaling, "lambda": lam, "ssm": session.client("ssm")}
    patch.undo()


@pytest.fixture
def aws(_fake_aws):
    """Fresh state per test: ASG at 1, no provisioned concurrency, kill switch RELEASED."""
    _fake_aws["autoscaling"].update_auto_scaling_group(AutoScalingGroupName=ASG, MinSize=1, MaxSize=10, DesiredCapacity=1)
    _fake_aws["ssm"].put_parameter(Name=DEFAULT_PARAMETER, Value=RELEASED, Type="String", Overwrite=True)
    lam = StatefulLambda(_fake_aws["lambda"])
    clients = Clients(_fake_aws["autoscaling"], lam, _fake_aws["ssm"])
    return clients, lam


def asg_desired(clients: Clients) -> int:
    return clients.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG])["AutoScalingGroups"][0]["DesiredCapacity"]


def by_service(records) -> dict:
    return {r.service: r for r in records}


def test_aws_dry_run_validates_against_real_resources_but_changes_nothing(aws) -> None:
    clients, lam = aws

    records = run_cycle(BOTH_DUE, LoopConfig(dry_run=True), make_source(), forecasters(), clients, LoopState())

    assert {r.actuation_status for r in records} == {"dry_run"}
    assert asg_desired(clients) == 1 and lam.calls == []
    assert all(r.mode == "aws" and r.kill_engaged is False for r in records)


def test_released_kill_switch_plus_real_mode_actually_scales(aws) -> None:
    clients, lam = aws

    records = run_cycle(BOTH_DUE, LoopConfig(dry_run=False), make_source(), forecasters(), clients, LoopState())

    assert {r.actuation_status for r in records} == {"applied"}
    assert asg_desired(clients) == 3
    assert lam.config["RequestedProvisionedConcurrentExecutions"] == 2


def test_an_engaged_kill_switch_blocks_all_real_actuation(aws) -> None:
    clients, lam = aws
    clients.ssm.put_parameter(Name=DEFAULT_PARAMETER, Value=ENGAGED, Type="String", Overwrite=True)

    records = run_cycle(BOTH_DUE, LoopConfig(dry_run=False), make_source(), forecasters(), clients, LoopState())

    assert {r.actuation_status for r in records} == {"blocked_by_kill_switch"}
    assert all(r.kill_engaged is True for r in records)
    assert asg_desired(clients) == 1 and lam.calls == []


def test_a_missing_kill_switch_parameter_blocks_real_actuation(aws) -> None:
    clients, lam = aws
    clients.ssm.delete_parameter(Name=DEFAULT_PARAMETER)

    records = run_cycle(BOTH_DUE, LoopConfig(dry_run=False), make_source(), forecasters(), clients, LoopState())

    assert {r.actuation_status for r in records} == {"blocked_by_kill_switch"}
    assert asg_desired(clients) == 1 and lam.calls == []


def test_an_unreadable_kill_switch_blocks_real_actuation(aws, monkeypatch) -> None:
    clients, lam = aws
    from botocore.exceptions import ClientError

    def denied(**_kw):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetParameter")

    monkeypatch.setattr(clients.ssm, "get_parameter", denied)

    records = run_cycle(BOTH_DUE, LoopConfig(dry_run=False), make_source(), forecasters(), clients, LoopState())

    assert {r.actuation_status for r in records} == {"blocked_by_kill_switch"}
    assert asg_desired(clients) == 1


def test_the_kill_switch_is_read_at_the_start_of_every_cycle(aws, monkeypatch) -> None:
    clients, _lam = aws
    reads = []
    real_get = clients.ssm.get_parameter
    monkeypatch.setattr(clients.ssm, "get_parameter", lambda **kw: (reads.append(kw["Name"]), real_get(**kw))[1])

    run_replay(pd.Timestamp("2026-06-01 02:10"), 4, LoopConfig(), make_source(), forecasters(), clients)

    assert reads == [DEFAULT_PARAMETER] * 4  # one read per cycle, not one per service


def test_flipping_the_switch_between_cycles_takes_effect_immediately(aws) -> None:
    clients, _lam = aws
    state = LoopState()
    first = run_cycle(BOTH_DUE, LoopConfig(dry_run=False), make_source(), forecasters(), clients, state)
    clients.ssm.put_parameter(Name=DEFAULT_PARAMETER, Value=ENGAGED, Type="String", Overwrite=True)

    second = run_cycle(BOTH_DUE + pd.Timedelta(minutes=5), LoopConfig(dry_run=False), make_source(), forecasters(catalog=500.0), clients, state)

    assert by_service(first)["catalog_service"].actuation_status == "applied"
    assert by_service(second)["catalog_service"].actuation_status == "blocked_by_kill_switch"
    assert asg_desired(clients) == 3  # the second, bigger decision (10) was never applied


def test_an_absurd_forecast_cannot_push_real_capacity_past_the_caps(aws) -> None:
    clients, lam = aws

    records = run_cycle(BOTH_DUE, LoopConfig(dry_run=False), make_source(), forecasters(1e12, 1e12), clients, LoopState())

    assert asg_desired(clients) == 10
    assert lam.config["RequestedProvisionedConcurrentExecutions"] == 5  # LambdaPolicyConfig default cap
    assert all(r.cap_applied for r in records)


def test_the_loop_reads_current_capacity_from_aws_not_from_its_own_memory(aws) -> None:
    clients, _lam = aws
    clients.autoscaling.set_desired_capacity(AutoScalingGroupName=ASG, DesiredCapacity=7)

    records = run_cycle(BOTH_DUE, LoopConfig(), make_source(), forecasters(catalog=0.0), clients, LoopState())

    assert by_service(records)["catalog_service"].current == 7


# --- failures are recorded, not hidden -----------------------------------------------------------


def test_one_service_failing_does_not_stop_the_other_and_is_recorded() -> None:
    fc = {"catalog_service": BrokenForecaster(), "thumbnail_service": FixedForecaster("thumbnail_service", 600.0)}

    records = run_cycle(BOTH_DUE, LoopConfig(), make_source(), fc, None, LoopState())

    results = by_service(records)
    assert results["catalog_service"].actuation_status == "error"
    assert "RuntimeError: model blew up" in results["catalog_service"].error
    assert results["thumbnail_service"].actuation_status == "offline"


def test_a_refused_actuation_is_recorded_as_an_error(aws) -> None:
    clients, _lam = aws
    clients.autoscaling.update_auto_scaling_group(AutoScalingGroupName=ASG, MaxSize=2)

    records = run_cycle(BOTH_DUE, LoopConfig(dry_run=False), make_source(), forecasters(catalog=300.0), clients, LoopState())

    catalog = by_service(records)["catalog_service"]
    assert catalog.actuation_status == "error" and "ActuationRefused" in catalog.error
    assert by_service(records)["thumbnail_service"].actuation_status == "applied"  # the other service still ran


# --- decision log ---------------------------------------------------------------------------------


def test_decisions_are_appended_as_json_lines(tmp_path) -> None:
    log = tmp_path / "out" / "decisions.jsonl"
    records = run_cycle(BOTH_DUE, LoopConfig(), make_source(), forecasters(), None, LoopState())

    append_records(log, records)
    append_records(log, records)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 * len(records)
    parsed = json.loads(lines[0])
    assert parsed["service"] in {"catalog_service", "thumbnail_service"} and parsed["dry_run"] is True
    assert parsed["time"].startswith("2026-06-01T02:10")


def test_run_replay_writes_the_log_when_asked(tmp_path) -> None:
    log = tmp_path / "decisions.jsonl"

    records = run_replay(pd.Timestamp("2026-06-01 02:10"), 3, LoopConfig(), make_source(), forecasters(), None, log_path=log)

    assert len(log.read_text(encoding="utf-8").splitlines()) == len(records)


# --- structure -------------------------------------------------------------------------------------


def test_the_loop_module_builds_no_aws_clients_at_import_or_in_the_cycle() -> None:
    source = Path(loop.__file__).read_text(encoding="utf-8")
    before_main = source.split("def main(")[0]

    assert "import boto3" not in before_main and "boto3." not in before_main


def test_importing_the_loop_needs_no_database_or_model_stack() -> None:
    source = Path(loop.__file__).read_text(encoding="utf-8")
    before_main = source.split("def main(")[0]

    assert "forecasting." not in before_main  # those imports are deferred into main()


# --- running one service at a time (for careful real-AWS tests) -----------------------------------------


AVAILABLE = {"catalog_service": object(), "thumbnail_service": object()}


def test_no_selection_means_every_service() -> None:
    assert select_services(AVAILABLE, "") == ["catalog_service", "thumbnail_service"]
    assert select_services(AVAILABLE, "   ") == ["catalog_service", "thumbnail_service"]


def test_a_selection_runs_exactly_the_named_services() -> None:
    assert select_services(AVAILABLE, "catalog_service") == ["catalog_service"]
    assert select_services(AVAILABLE, " thumbnail_service ") == ["thumbnail_service"]
    assert select_services(AVAILABLE, "thumbnail_service,catalog_service") == ["catalog_service", "thumbnail_service"]


@pytest.mark.parametrize("names", ["catalogue_service", "catalog_service,nope", ",", "ec2"])
def test_an_unknown_or_empty_selection_is_a_loud_error(names) -> None:
    with pytest.raises(ValueError, match="unknown service"):
        select_services(AVAILABLE, names)


def test_a_single_service_selection_only_touches_that_service() -> None:
    fc = forecasters()
    only_thumbnail = {name: fc[name] for name in select_services(fc, "thumbnail_service")}

    records = run_cycle(BOTH_DUE, LoopConfig(), make_source(), only_thumbnail, None, LoopState())

    assert {r.service for r in records} == {"thumbnail_service"}
