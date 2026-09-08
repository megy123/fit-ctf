from fit_ctf_cli.cli import cli
from tests import CLIData


def _run(cli_data: CLIData, command: str):
    result = cli_data[2].invoke(cli, command.split())
    assert result.exit_code == 0, result.output
    return result


def test_attack_defense_operator_cli(empty_cli_data: CLIData):
    app, _, _ = empty_cli_data
    app.prj_mgr.init_project(name="cli_ad", max_nof_users=10)

    _run(empty_cli_data, "team create -t red --display-name Red")
    _run(empty_cli_data, "team create -t blue --display-name Blue")
    red = _run(empty_cli_data, "ad enroll-team -pn cli_ad -t red")
    blue = _run(empty_cli_data, "ad enroll-team -pn cli_ad -t blue")
    assert "shown once" in red.output
    assert "shown once" in blue.output

    _run(empty_cli_data, "ad init -pn cli_ad --tick-seconds 10")
    _run(
        empty_cli_data,
        "ad service-add -pn cli_ad --key notes --scenario notes --service web",
    )
    _run(empty_cli_data, "ad preflight -pn cli_ad")
    _run(empty_cli_data, "ad start -pn cli_ad")
    _run(empty_cli_data, "ad tick-open -pn cli_ad")
    _run(empty_cli_data, "ad tick-close -pn cli_ad")
    board = _run(empty_cli_data, "ad scoreboard -pn cli_ad")
    assert "Red" in board.output
    assert "Blue" in board.output
