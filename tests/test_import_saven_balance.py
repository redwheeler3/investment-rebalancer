"""Private config selection and encrypted snapshot persistence through the importer."""

import importlib.util
from pathlib import Path
from unittest.mock import patch

from tests.test_saven_tracking import ACCOUNT_A, ACCOUNT_B, cached, export


spec = importlib.util.spec_from_file_location(
    "import_saven_balance", Path(__file__).resolve().parents[1] / "scripts" / "import_saven_balance.py",
)
importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(importer)


def run_import(tmp_path, account_numbers, existing=None, extra_args=()):
    config = tmp_path / "config"
    config.mkdir()
    config.joinpath("settings.yaml").write_text(
        "saven:\n  account_numbers:\n" + "".join(f'    - "{number}"\n' for number in account_numbers),
        encoding="utf-8",
    )
    data = tmp_path / "data"
    data.mkdir()
    destination = data / "saven_balance.json"
    if existing is not None:
        destination.write_text("encrypted-placeholder", encoding="utf-8")
    with (
        patch.object(importer, "get_config_dir", return_value=config),
        patch.object(importer, "get_data_dir", return_value=data),
        patch.object(importer, "read_encrypted_json", return_value=existing),
        patch.object(importer, "write_encrypted_json") as write,
        patch("sys.argv", ["import_saven_balance", "--directory", str(tmp_path), "--save", *extra_args]),
    ):
        code = importer.main()
    return code, write, destination


def test_uses_private_config_and_saves_only_tracked_account(tmp_path, capsys):
    export(tmp_path / "one.csv", ACCOUNT_A, "10.00")
    export(tmp_path / "two.csv", ACCOUNT_B, "20.00")
    code, write, destination = run_import(tmp_path, [ACCOUNT_A])
    assert code == 0
    write.assert_called_once()
    assert write.call_args.args[0] == destination
    assert set(write.call_args.args[1]["accounts"]) == {ACCOUNT_A}
    output = capsys.readouterr().out
    assert ACCOUNT_A not in output
    assert ACCOUNT_B not in output


def test_no_new_export_keeps_cache_and_does_not_rewrite_it(tmp_path):
    path = export(tmp_path / "one.csv")
    existing = cached(path)
    path.unlink()
    code, write, _ = run_import(tmp_path, [ACCOUNT_A], existing)
    assert code == 0
    write.assert_not_called()


def test_refreshing_one_configured_account_preserves_the_other_cache(tmp_path):
    first = export(tmp_path / "one.csv", ACCOUNT_A, "10.00")
    second = export(tmp_path / "two.csv", ACCOUNT_B, "20.00")
    existing = cached(second)
    code, write, _ = run_import(tmp_path, [ACCOUNT_A, ACCOUNT_B], existing, ["--account-number", ACCOUNT_A])
    assert code == 0
    result = write.call_args.args[1]["accounts"]
    assert result[ACCOUNT_A]["balance_cad"] == "10.00"
    assert result[ACCOUNT_B] == existing["accounts"][ACCOUNT_B]
