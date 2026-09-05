from pathlib import Path

from lib_common.env_utils import load_dotenv_file, parse_dotenv_line


def test_parse_dotenv_line_strips_inline_comments() -> None:
    assert parse_dotenv_line("DB_PASSWORD=secret # comment") == ("DB_PASSWORD", "secret")
    assert parse_dotenv_line("HASH=pass#word") == ("HASH", "pass#word")
    assert parse_dotenv_line('QUOTED="value # keep"') == ("QUOTED", "value # keep")
    assert parse_dotenv_line("   # comment only") is None
    assert parse_dotenv_line("") is None


def test_load_dotenv_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DB_USER=trader\nDB_PASSWORD=secret # comment\nQUOTED='value # keep'\n",
        encoding="utf-8",
    )
    values = load_dotenv_file(str(env_file))
    assert values["DB_USER"] == "trader"
    assert values["DB_PASSWORD"] == "secret"
    assert values["QUOTED"] == "value # keep"
