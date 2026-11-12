import builtins
from unittest.mock import patch

from typer.testing import CliRunner

from mcp_email_server.cli import app


def test_missing_optional_ui_has_an_install_hint_and_does_not_break_mail_runtime():
    original = builtins.__import__

    def no_gradio(name, *args, **kwargs):
        if name == "gradio":
            raise ModuleNotFoundError("Synthetic optional dependency missing", name="gradio")
        return original(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=no_gradio):
        result = CliRunner().invoke(app, ["ui"])
    assert result.exit_code == 2
    assert "mcp-email-server[ui]" in result.stdout
